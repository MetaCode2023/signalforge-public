from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

from signalforge.core.errors import ValidationError
from signalforge.core.types import Bar, QualityStatus

DUPLICATE_SYMBOL_DATE = "duplicate_symbol_date"
NEGATIVE_OR_ZERO_PRICE = "negative_or_zero_price"
IMPOSSIBLE_OHLC = "impossible_ohlc"
NEGATIVE_VOLUME = "negative_volume"
ZERO_VOLUME = "zero_volume"
SUSPICIOUS_PRICE_JUMP = "suspicious_price_jump"
STALE_PRICE = "stale_price"
MISSING_OBSERVATION = "missing_observation"
NON_TRADING_DAY = "non_trading_day"
FUTURE_DATED = "future_dated"
INCONSISTENT_ADJUSTMENT = "inconsistent_adjustment"
MALFORMED_RECORD = "malformed_record"

QUARANTINE_RULES = (
    DUPLICATE_SYMBOL_DATE, NEGATIVE_OR_ZERO_PRICE, IMPOSSIBLE_OHLC, NEGATIVE_VOLUME,
    NON_TRADING_DAY, FUTURE_DATED, MALFORMED_RECORD,
)
SUSPECT_RULES = (ZERO_VOLUME, SUSPICIOUS_PRICE_JUMP, STALE_PRICE, INCONSISTENT_ADJUSTMENT)

Key = tuple[str, date, str]


@dataclass(frozen=True)
class QuarantinedRecord:
    record: dict
    rule: str
    rules: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class Flag:
    symbol: str
    trade_date: date
    source: str
    rule: str
    detail: str


@dataclass
class ValidationReport:
    accepted: list = field(default_factory=list)
    quarantined: list[QuarantinedRecord] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    missing: dict[str, int] = field(default_factory=dict)
    meta: dict[Key, dict] = field(default_factory=dict)

    def status_of(self, bar: Bar) -> QualityStatus:
        return self.meta[(bar.symbol, bar.trade_date, bar.source)]["quality_status"]

    def rules_fired(self) -> Counter:
        counts: Counter = Counter()
        for q in self.quarantined:
            counts.update(q.rules)
        for f in self.flags:
            counts[f.rule] += 1
        if self.missing:
            counts[MISSING_OBSERVATION] += sum(self.missing.values())
        return counts


def _to_decimal(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("not numeric")
    try:
        out = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"not numeric: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"not finite: {value!r}")
    return out


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _to_volume(value) -> int:
    d = _to_decimal(value)
    if d != d.to_integral_value():
        raise ValueError(f"volume not integral: {value!r}")
    return int(d)


def _parse(record: dict) -> dict:
    symbol = str(record.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("missing symbol")
    parsed = {"symbol": symbol, "trade_date": _to_date(record.get("trade_date"))}
    for name in ("open", "high", "low", "close"):
        parsed[name] = _to_decimal(record.get(name))
    parsed["volume"] = _to_volume(record.get("volume"))
    adj = record.get("adj_close")
    parsed["adj_close"] = None if adj is None or str(adj).strip() == "" else _to_decimal(adj)
    parsed["source"] = str(record.get("source") or "unknown")
    return parsed


def _hard_failures(p: dict, as_of: date) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if p["trade_date"] > as_of:
        out.append((FUTURE_DATED, f"trade_date {p['trade_date']} is after ingest date {as_of}"))
    if p["trade_date"].weekday() >= 5:
        out.append((NON_TRADING_DAY, f"{p['trade_date']} is a weekend"))
    prices = [p[n] for n in ("open", "high", "low", "close")]
    if p["adj_close"] is not None:
        prices.append(p["adj_close"])
    if any(x <= 0 for x in prices):
        out.append((NEGATIVE_OR_ZERO_PRICE, f"non-positive price in {[str(x) for x in prices]}"))
    else:
        o, h, lo, c = p["open"], p["high"], p["low"], p["close"]
        if h < lo or not (lo <= o <= h) or not (lo <= c <= h):
            out.append((IMPOSSIBLE_OHLC, f"open={o} high={h} low={lo} close={c}"))
    if p["volume"] < 0:
        out.append((NEGATIVE_VOLUME, f"volume {p['volume']}"))
    return out


def _weekdays_between(a: date, b: date) -> int:
    n, d = 0, a
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _quarantine(record: dict, failures: list[tuple[str, str]]) -> QuarantinedRecord:
    return QuarantinedRecord(
        record,
        failures[0][0],
        tuple(r for r, _ in failures),
        "; ".join(f"{r}: {d}" for r, d in failures),
    )


def validate_bar_records(
    records: Iterable[dict],
    *,
    as_of: date | None = None,
    jump_threshold: float = 0.50,
    stale_run: int = 5,
    adjustment_threshold: float = 0.01,
    corporate_action_dates: set[tuple[str, date]] | None = None,
    prior_bars: Mapping[str, Bar] | None = None,
    prior_stale_runs: Mapping[str, int] | None = None,
) -> ValidationReport:
    """Validate a batch of bar records.

    `prior_bars` is the last ALREADY-STORED bar per symbol, and
    `prior_stale_runs` the length of the identical-close run ending at it.
    Without them the jump, stale-price and adjustment rules cannot see across
    a batch boundary: they compare each bar with the previous bar in the same
    batch and skip the first one, so on a daily incremental pull a
    split-sized overnight jump lands exactly on the unchecked bar. Supply
    both from `Warehouse.latest_bars()` and `Warehouse.stale_runs()`.

    A prior bar is used for comparison only. It never appears in `accepted`,
    and a prior bar dated at or after the batch's first bar is ignored rather
    than treated as a predecessor.
    """
    as_of = as_of or date.today()
    actions = corporate_action_dates or set()
    report = ValidationReport()
    seen: set[tuple[str, date]] = set()
    bars: list[Bar] = []

    for record in records:
        try:
            p = _parse(record)
        except (ValueError, TypeError) as exc:
            report.quarantined.append(_quarantine(record, [(MALFORMED_RECORD, str(exc))]))
            continue
        failures = _hard_failures(p, as_of)
        key = (p["symbol"], p["trade_date"])
        if key in seen:
            failures.append((DUPLICATE_SYMBOL_DATE, f"{p['symbol']} {p['trade_date']} already seen"))
        if failures:
            # Deliberately NOT claiming the key here. A record that already
            # failed must not reserve its (symbol, date): a corrected record
            # arriving later for the same key was being quarantined as a
            # duplicate of the broken one it was meant to replace.
            report.quarantined.append(_quarantine(record, failures))
            continue
        seen.add(key)
        try:
            bar = Bar(**p)
        except ValidationError as exc:
            report.quarantined.append(_quarantine(record, [(MALFORMED_RECORD, str(exc))]))
            continue
        bars.append(bar)
        report.meta[(bar.symbol, bar.trade_date, bar.source)] = {
            "retrieved_at": record.get("retrieved_at"),
            "source_reference": record.get("source_reference"),
            "adjustment_method": record.get("adjustment_method"),
            "quality_status": QualityStatus.OK,
        }

    report.accepted = bars
    by_symbol: dict[str, list[Bar]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)

    def flag(bar: Bar, rule: str, detail: str) -> None:
        report.flags.append(Flag(bar.symbol, bar.trade_date, bar.source, rule, detail))
        report.meta[(bar.symbol, bar.trade_date, bar.source)]["quality_status"] = (
            QualityStatus.SUSPECT
        )

    priors = prior_bars or {}
    carried_runs = prior_stale_runs or {}

    for symbol, series in by_symbol.items():
        series.sort(key=lambda b: b.trade_date)

        # The last stored bar, if it genuinely precedes this batch. A prior
        # dated at or after the batch start is a stale cache, not a
        # predecessor, so it is ignored rather than compared against.
        prior = priors.get(symbol)
        if prior is not None and prior.trade_date >= series[0].trade_date:
            prior = None

        present = {b.trade_date for b in series}
        span_start = prior.trade_date if prior is not None else series[0].trade_date
        gaps = _weekdays_between(span_start, series[-1].trade_date) - sum(
            1 for d in present if d.weekday() < 5
        )
        if prior is not None and prior.trade_date.weekday() < 5:
            gaps -= 1
        if gaps > 0:
            report.missing[symbol] = gaps

        run = carried_runs.get(symbol, 1) if prior is not None else 1
        for i, bar in enumerate(series):
            if bar.volume == 0:
                flag(bar, ZERO_VOLUME, "volume is 0; a halt is possible, kept but flagged")
            prev = series[i - 1] if i > 0 else prior
            if prev is None:
                continue
            move = abs(math.log(float(bar.close) / float(prev.close)))
            if move > jump_threshold:
                flag(
                    bar,
                    SUSPICIOUS_PRICE_JUMP,
                    f"close {prev.close} -> {bar.close} (|log return| {move:.3f}); "
                    f"prior close {prev.close} on {prev.trade_date}",
                )
            run = run + 1 if bar.close == prev.close else 1
            if run >= stale_run:
                flag(bar, STALE_PRICE, f"{run} identical closes at {bar.close}")
            if (
                bar.adjustment_ratio is not None
                and prev.adjustment_ratio is not None
                and (symbol, bar.trade_date) not in actions
            ):
                change = abs(float(bar.adjustment_ratio / prev.adjustment_ratio) - 1.0)
                if change > adjustment_threshold:
                    flag(
                        bar,
                        INCONSISTENT_ADJUSTMENT,
                        f"adj/close ratio {prev.adjustment_ratio:.6f} -> "
                        f"{bar.adjustment_ratio:.6f} with no recorded corporate action",
                    )
    report.flags.sort(key=lambda f: (f.symbol, f.trade_date, f.rule))
    return report


@dataclass
class SeriesReport:
    accepted: list[dict] = field(default_factory=list)
    quarantined: list[QuarantinedRecord] = field(default_factory=list)


def validate_series_records(records: Iterable[dict], *, as_of: date | None = None) -> SeriesReport:
    as_of = as_of or date.today()
    report = SeriesReport()
    seen: set[tuple[str, date]] = set()
    for record in records:
        try:
            sid = str(record.get("series_id") or "").strip()
            if not sid:
                raise ValueError("missing series_id")
            day = _to_date(record.get("observation_date"))
            value = _to_decimal(record.get("value"))
        except (ValueError, TypeError) as exc:
            report.quarantined.append(_quarantine(record, [(MALFORMED_RECORD, str(exc))]))
            continue
        failures = []
        if day > as_of:
            failures.append((FUTURE_DATED, f"observation_date {day} is after ingest date {as_of}"))
        if (sid, day) in seen:
            failures.append((DUPLICATE_SYMBOL_DATE, f"{sid} {day} already seen"))
        seen.add((sid, day))
        if failures:
            report.quarantined.append(_quarantine(record, failures))
            continue
        report.accepted.append({**record, "series_id": sid, "observation_date": day, "value": value})
    return report
