"""Turning the warehouse into runnable histories, and running the baselines.

Two kinds of history come out of this module and they are NOT interchangeable:

* `series_history` builds bars from a macro or index series. Those bars carry
  one number, the close, repeated into open/high/low, and `volume_known` is
  False. Backtests on them are PROXY STUDIES: you cannot buy an index, there
  is no intraday range, and no traded size constrains a fill.
* `ohlcv_history` builds bars from real per-symbol OHLCV with volume.

Every history returned carries its own limitation notes, which travel into
`BacktestResult.data_limitations` and from there into the reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from signalforge.backtesting.engine import BacktestEngine
from signalforge.backtesting.metrics import PerformanceMetrics, compute_metrics
from signalforge.config.models import ConfigBundle, EligibilityConfig, ResearchConfig, RiskConfig
from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory, summarise_symbols
from signalforge.core.interfaces import BacktestResult, RiskEvent
from signalforge.core.types import Bar
from signalforge.data.storage import Warehouse
from signalforge.strategies.regime import RiskOffGate
from signalforge.strategies.registry import build

ZERO = Decimal("0")

PROXY_NOTE = (
    "PROXY STUDY, NOT A TRADEABLE RESULT. This run uses the {series} index "
    "level as if it were a tradeable instrument. It is not: an index cannot be "
    "bought. A real implementation would trade an ETF and would pay its "
    "tracking error, expense ratio, spread and creation/redemption friction, "
    "none of which are modelled here."
)

CLOSE_ONLY_NOTE = (
    "Close-only series: open, high and low are all set to the published close, "
    "so there is no intraday range. Gap modelling degrades to close-to-close "
    "and limit orders cannot be evaluated against a real high or low."
)

SURVIVORSHIP_NOTE = (
    "SURVIVORSHIP BIAS: this universe is today's surviving large caps, chosen "
    "in hindsight. Companies that failed, were delisted or were acquired over "
    "the same period are absent by construction, so every figure here is "
    "flattered relative to what could have been selected at the time."
)

ADJUSTED_BASIS_NOTE = (
    "ADJUSTED PRICE BASIS: orders are sized, filled and marked on split- and "
    "dividend-adjusted prices so a held position survives corporate actions. "
    "Early adjusted price levels and per-share commissions are not historical: "
    "a 2005 fill is priced at today's adjusted level, so the per-share "
    "commission and the minimum commission are charged against prices and "
    "share counts that never traded. This note supersedes the engine's generic "
    "warning that a holding carried through a split would be mis-valued: that "
    "applies to raw prices only, and on this basis a split is already folded "
    "into the price history."
)

EXPOSURE_MISMATCH_NOTE = (
    "EXPOSURE MISMATCH: the per-name strategies hold up to 5% PER SYMBOL across "
    "the universe (up to roughly 100% gross), while the benchmark holds 5% in a "
    "single ETF. Benchmark-relative figures therefore partly reflect how much "
    "capital was deployed, not skill. Read the capital-deployed column beside "
    "every return."
)

PRICE_BASES = ("raw", "adjusted")
PRICE_QUANT = Decimal("0.000001")


VOLATILITY_REGIMES = ("low_vol", "mid_vol", "high_vol")
REGIME_WARMUP_SESSIONS = 252


def volatility_regime_labels(
    observations: list[tuple[date, Decimal]],
    warmup: int = REGIME_WARMUP_SESSIONS,
) -> dict[date, str]:
    """Label each date low/mid/high volatility from a volatility series.

    Classification is POINT IN TIME: each date is placed against the terciles
    of the observations up to and including it, never against the terciles of
    the whole history. A boundary computed over the full series would move
    every historical label as new data arrived — in 1995 nobody knew the
    1995-2026 VIX distribution, and using it is look-ahead bias dressed up as
    a regime definition.

    The first `warmup` observations are left UNLABELLED rather than guessed.
    A date absent from the result means "not classifiable", which the metrics
    layer reports as unlabelled instead of folding into a regime.
    """
    if warmup < 0:
        raise ValidationError(f"warmup must not be negative, got {warmup}")
    usable = [(d, v) for d, v in sorted(observations) if v is not None and v > ZERO]
    if len(usable) <= warmup:
        return {}

    labels: dict[date, str] = {}
    history: list[Decimal] = [v for _, v in usable[:warmup]]
    for day, value in usable[warmup:]:
        history.append(value)
        ranked = sorted(history)
        lower = ranked[len(ranked) // 3]
        upper = ranked[(2 * len(ranked)) // 3]
        if value <= lower:
            labels[day] = "low_vol"
        elif value >= upper:
            labels[day] = "high_vol"
        else:
            labels[day] = "mid_vol"
    return labels


@dataclass(frozen=True, slots=True)
class HistoryBundle:
    history: PriceHistory
    limitations: tuple[str, ...]
    description: str


@dataclass(slots=True)
class StrategyOutcome:
    strategy_id: str
    label: str
    window: str
    result: BacktestResult
    metrics: PerformanceMetrics

    @property
    def total_return(self) -> Decimal:
        if not self.result.equity_curve:
            return ZERO
        return (self.result.final_equity / self.result.starting_cash) - Decimal(1)


@dataclass(slots=True)
class ResearchRun:
    window: str
    start: date
    end: date
    dataset_description: str
    dataset_fingerprint: str
    limitations: list[str] = field(default_factory=list)
    outcomes: list[StrategyOutcome] = field(default_factory=list)

    def by_id(self, strategy_id: str) -> StrategyOutcome | None:
        for outcome in self.outcomes:
            if outcome.strategy_id == strategy_id:
                return outcome
        return None


def series_history(
    observations: list[tuple[date, Decimal]], symbol: str, source: str = "fred"
) -> HistoryBundle:
    """Build a close-only history from a series of (date, value) pairs.

    Non-positive observations are dropped rather than coerced: a zero or
    negative index level is not a price, and the alternative is inventing one.
    """
    if not observations:
        raise ValidationError(f"no observations supplied for {symbol}")
    bars: list[Bar] = []
    dropped = 0
    for day, value in sorted(observations):
        if value is None or value <= ZERO:
            dropped += 1
            continue
        bars.append(
            Bar(
                symbol=symbol,
                trade_date=day,
                open=value,
                high=value,
                low=value,
                close=value,
                volume=0,
                adj_close=value,
                source=source,
                volume_known=False,
            )
        )
    if not bars:
        raise ValidationError(f"every observation for {symbol} was non-positive")
    notes = [PROXY_NOTE.format(series=symbol), CLOSE_ONLY_NOTE]
    if dropped:
        notes.append(
            f"{dropped} {symbol} observations were non-positive and were dropped "
            "rather than coerced into a price."
        )
    notes.append(
        f"Dividends are not reflected: {symbol} is a price index, so total "
        "return is understated for buy-and-hold and for any strategy holding it."
    )
    return HistoryBundle(
        history=PriceHistory.from_bars(bars),
        limitations=tuple(notes),
        description=(
            f"{symbol} daily close from {source}, {len(bars)} observations "
            f"{bars[0].trade_date.isoformat()} to {bars[-1].trade_date.isoformat()}"
        ),
    )


# A gap this long inside one symbol's history is not a holiday. Above the
# refusal threshold it is almost certainly two unrelated datasets glued
# together; between the two it loads but must be declared.
GAP_DECLARE_DAYS = 60
GAP_REFUSE_DAYS = 365


def _adjusted(bar: Bar) -> Bar:
    ratio = bar.adjustment_ratio
    if ratio is None:
        return bar
    o, h, low, c = (
        (x * ratio).quantize(PRICE_QUANT)
        for x in (bar.open, bar.high, bar.low, bar.close)
    )
    return Bar(
        symbol=bar.symbol,
        trade_date=bar.trade_date,
        open=o,
        high=max(h, o, c),
        low=min(low, o, c),
        close=c,
        volume=int((Decimal(bar.volume) / ratio).to_integral_value()),
        adj_close=c,
        source=bar.source,
        volume_known=bar.volume_known,
    )


def _manual_scaled(bar: Bar, numerator: int, denominator: int) -> Bar:
    o, h, low, c = (
        (x * denominator / numerator).quantize(PRICE_QUANT)
        for x in (bar.open, bar.high, bar.low, bar.close)
    )
    return Bar(
        symbol=bar.symbol,
        trade_date=bar.trade_date,
        open=o,
        high=max(h, o, c),
        low=min(low, o, c),
        close=c,
        volume=int((Decimal(bar.volume * numerator) / denominator).to_integral_value()),
        adj_close=c,
        source=bar.source,
        volume_known=bar.volume_known,
    )


MANUAL_JUMP_TOLERANCE = Decimal("0.10")


def _vendor_shows_jump(bars: list[Bar], action) -> bool:
    """Whether the vendor-adjusted series still jumps across the action's date.

    A manual action exists because the vendor's adj_close did not fold it in.
    If the adjusted close already moves by about the manual ratio there, the
    vendor has since fixed it and applying the action again would double it.
    """
    on = next((b for b in bars if b.trade_date == action.effective_date), None)
    prior = [b for b in bars if b.trade_date < action.effective_date]
    if on is None or not prior or on.close <= ZERO or prior[-1].close <= ZERO:
        return False
    ratio = Decimal(action.numerator) / Decimal(action.denominator)
    seen = prior[-1].close / on.close
    return abs(seen / ratio - 1) <= MANUAL_JUMP_TOLERANCE


def _apply_manual_splits(
    warehouse: Warehouse, bars: list[Bar]
) -> tuple[list[Bar], list[str]]:
    """Fold source='manual' splits into already-adjusted bars.

    Bars dated before a split's effective date are scaled by denominator /
    numerator, cumulatively over every later manual split. Vendor splits are
    not touched: they are already inside adj_close. A manual action is applied
    only while the vendor-adjusted series still jumps across its date; one the
    vendor has since folded in, or one with no bar to check against, is skipped
    and named in the notes.
    """
    symbols = sorted({b.symbol for b in bars})
    manual = [
        a
        for a in warehouse.load_corporate_actions(symbols=symbols, source="manual")
        if a.action_type == "split"
    ]
    if not manual:
        return bars, []
    series: dict[str, list[Bar]] = {}
    for bar in bars:
        series.setdefault(bar.symbol, []).append(bar)
    by_symbol: dict[str, list] = {}
    notes: list[str] = []
    for action in manual:
        label = f"{action.symbol} {action.effective_date} ({action.numerator}:{action.denominator})"
        if _vendor_shows_jump(series.get(action.symbol, []), action):
            by_symbol.setdefault(action.symbol, []).append(action)
        else:
            notes.append(
                f"MANUAL SPLIT SKIPPED: {label}: the vendor-adjusted series shows no "
                "matching jump across that date (already folded in, or no bar to check), "
                "so it was not applied again."
            )
    out: list[Bar] = []
    for bar in bars:
        later = [a for a in by_symbol.get(bar.symbol, ()) if bar.trade_date < a.effective_date]
        if not later:
            out.append(bar)
            continue
        numerator = denominator = 1
        for action in later:
            numerator *= action.numerator
            denominator *= action.denominator
        out.append(_manual_scaled(bar, numerator, denominator))
    notes.extend(
        f"MANUAL SPLIT ADJUSTMENT: {symbol} bars before "
        + ", ".join(
            f"{a.effective_date} ({a.numerator}:{a.denominator})"
            for a in sorted(actions, key=lambda a: a.effective_date)
        )
        + " are scaled by the recorded manual split(s) because the vendor's adjusted "
        "close did not fold them in."
        for symbol, actions in sorted(by_symbol.items())
    )
    return out, notes


def ohlcv_history(
    warehouse: Warehouse,
    symbols: list[str],
    sources: list[str] | None = None,
    *,
    price_basis: str = "raw",
    start: date | None = None,
    end: date | None = None,
) -> HistoryBundle:
    """Build a history from real per-symbol OHLCV stored in the warehouse.

    Refuses a symbol whose bars contain a multi-year hole. The warehouse can
    hold the same symbol from two unrelated datasets (a 2015-2017 sample and a
    2026 four-day snapshot, in this project's case). Concatenating them yields
    a history with a 9.5-year hole, and a moving average or z-score computed
    across that hole is meaningless while looking exactly like a real number.

    Pass `sources` to select one dataset deliberately.

    `price_basis="adjusted"` scales open/high/low/close by each bar's
    adj_close/close ratio (volume by its inverse), so a split is not a crash.
    Bars with no adj_close stay raw and are named in a limitation note.
    """
    if price_basis not in PRICE_BASES:
        raise ValidationError(
            f"unknown price_basis {price_basis!r}; expected one of {list(PRICE_BASES)}"
        )
    bars = warehouse.load_bars(symbols=symbols, start=start, end=end)
    if sources is not None:
        allowed = set(sources)
        bars = [b for b in bars if b.source in allowed]
    if not bars:
        where = f" from sources {sorted(sources)}" if sources else ""
        raise ValidationError(
            f"no bars in the warehouse for {summarise_symbols(symbols)}{where}"
        )

    notes: list[str] = []
    grouped: dict[str, list[Bar]] = {}
    for b in bars:
        grouped.setdefault(b.symbol, []).append(b)
    for symbol in sorted(grouped):
        symbol_bars = sorted(grouped[symbol], key=lambda b: b.trade_date)
        for previous, current in zip(symbol_bars, symbol_bars[1:]):
            gap = (current.trade_date - previous.trade_date).days
            if gap < GAP_DECLARE_DAYS:
                continue
            detail = (
                f"{symbol} has a {gap}-day gap between {previous.trade_date} "
                f"(source {previous.source}) and {current.trade_date} "
                f"(source {current.source})"
            )
            if gap >= GAP_REFUSE_DAYS:
                raise ValidationError(
                    detail
                    + ". That is not a holiday: these are almost certainly two "
                    "unrelated datasets concatenated. Any trailing-window "
                    "calculation would span the gap and produce a meaningless "
                    "number that looks real. Pass `sources=[...]` to choose one."
                )
            notes.append(
                "DISCONTINUOUS HISTORY: "
                + detail
                + ". Any trailing window spanning it mixes two regimes."
            )

    days = sorted({b.trade_date for b in bars})
    if len(days) < 252:
        notes.append(
            f"Only {len(days)} distinct trading days available for "
            f"{summarise_symbols(symbols)}; below the 252-session floor for "
            "annualised statistics."
        )
    sources = sorted({b.source for b in bars})
    if any(b.adj_close is None for b in bars):
        notes.append(
            "Some bars carry no adjusted close, so dividends are not reflected "
            "for those symbols."
        )
    basis = ""
    if price_basis == "adjusted":
        unadjusted = sorted({b.symbol for b in bars if b.adj_close is None})
        partial = sorted(
            s for s in unadjusted
            if any(b.symbol == s and b.adj_close is not None for b in bars)
        )
        if partial:
            raise ValidationError(
                f"{summarise_symbols(partial)} carry adj_close on some bars but not "
                "others; scaling only part of a symbol's history would put a "
                "price discontinuity inside it. Fix or exclude the symbol."
            )
        if unadjusted:
            notes.append(
                f"{summarise_symbols(unadjusted)} carry no adj_close on some bars; "
                "those bars stay on RAW prices inside an adjusted-basis run."
            )
        bars = [_adjusted(b) for b in bars]
        bars, manual_notes = _apply_manual_splits(warehouse, bars)
        notes.append(ADJUSTED_BASIS_NOTE)
        notes.extend(manual_notes)
        basis = ", adjusted price basis"
    return HistoryBundle(
        history=PriceHistory.from_bars(bars),
        limitations=tuple(notes),
        description=(
            f"{len(bars)} OHLCV bars for {len(set(b.symbol for b in bars))} symbols "
            f"from {', '.join(sources)}, {days[0].isoformat()} to {days[-1].isoformat()}"
            f"{basis}"
        ),
    )


def load_index_bundle(warehouse: Warehouse, series_id: str) -> HistoryBundle:
    observations = warehouse.load_series(series_id)
    return series_history(observations, symbol=series_id)


# ----------------------------------------------------------------------
# The baseline suite
# ----------------------------------------------------------------------
def baseline_specs(
    symbol: str,
    *,
    universe: tuple[str, ...] | None = None,
    benchmark: str | None = None,
) -> list[tuple[str, str, dict]]:
    """The four baselines plus the benchmark, as (label, strategy_id, params).

    With `universe`, the symbol-taking strategies trade that universe and the
    buy-and-hold benchmark holds `benchmark` instead.

    Parameters are deliberately conventional textbook values, chosen before
    looking at any result, so that the first pass is not already a fitted one.
    """
    traded = (symbol,) if universe is None else tuple(universe)
    momentum_scope = {} if universe is None else {"symbols": traded}
    held = (symbol,) if universe is None else (benchmark,)
    return [
        (
            "MA trend 50/200",
            "ma_trend",
            {
                "fast_window": 50,
                "slow_window": 200,
                "target_weight": Decimal("0.05"),
                "symbols": traded,
            },
        ),
        (
            "Cross-sectional momentum 126/21",
            "xs_momentum",
            {
                "lookback_days": 126,
                "skip_days": 21,
                "top_n": 1,
                "target_weight_total": Decimal("0.05"),
                "min_history_days": 150,
                **momentum_scope,
            },
        ),
        (
            "Mean reversion z=2 / 20d",
            "zscore_mean_reversion",
            {
                "window": 20,
                # entry_z is a positive magnitude; the strategy enters when the
                # z-score falls BELOW -entry_z and exits above -exit_z.
                "entry_z": Decimal("2"),
                "exit_z": Decimal("0"),
                "target_weight": Decimal("0.05"),
                "max_holding_days": 20,
                "symbols": traded,
            },
        ),
        (
            "Vol-targeted trend 10% ann.",
            "vol_targeted_trend",
            {
                "fast_window": 50,
                "slow_window": 200,
                "target_volatility": Decimal("0.10"),
                "vol_window": 20,
                "max_weight": Decimal("0.05"),
                "annualisation_factor": 252,
                "symbols": traded,
            },
        ),
        (
            "Buy and hold (benchmark)",
            "buy_and_hold",
            {"symbols": held, "target_weight": Decimal("0.05")},
        ),
    ]


# name, series, rule, threshold, lag in sessions. Decided a priori, not tuned.
RISKOFF_GATES = (
    ("vix30", "VIXCLS", ">", Decimal("30"), 1),
    ("curve0", "T10Y2Y", "<", Decimal("0"), 2),
)


def broad_specs(
    traded: tuple[str, ...],
    benchmark: str,
    eligibility: EligibilityConfig,
    max_names: int,
    max_stale_days: int,
) -> list[tuple[str, str, dict]]:
    """The strategies a broad run can afford: the two that scale to hundreds of names.

    Both run behind the point-in-time eligibility screen and hold at most
    `max_names` positions, so the 20-position risk cap is rarely what picks the
    book. It can still bite: max_positions counts unfilled orders, so a
    rotation (an exit and an entry on the same signal day) fills one session
    late and the cap can refuse an ENTER on the signal day. Mean reversion is left out (quadratic in history) and the
    vol-targeted trend is left out (the 5% cap binds before vol scaling, so it
    duplicates ma_trend).
    """
    screen = {
        "min_history_sessions": eligibility.min_history_sessions,
        "min_median_dollar_volume": eligibility.min_median_dollar_volume,
        "dollar_volume_window": eligibility.dollar_volume_window,
        "max_stale_days": max_stale_days,
    }
    return [
        (
            f"MA trend 50/200 (top {max_names})",
            "ma_trend",
            {
                "fast_window": 50,
                "slow_window": 200,
                "target_weight": Decimal("0.05"),
                "symbols": traded,
                "max_names": max_names,
                "eligibility": dict(screen),
            },
        ),
        (
            f"Cross-sectional momentum 126/21 (top {max_names})",
            "xs_momentum",
            {
                "lookback_days": 126,
                "skip_days": 21,
                "top_n": max_names,
                "target_weight_total": Decimal("1.0"),
                "min_history_days": 150,
                "symbols": traded,
                "eligibility": dict(screen),
            },
        ),
        (
            "Buy and hold (benchmark)",
            "buy_and_hold",
            {"symbols": (benchmark,), "target_weight": Decimal("0.05")},
        ),
    ]


def overlay_specs(
    specs: list[tuple[str, str, dict]], gates: list[RiskOffGate]
) -> list[tuple[str, str, dict]]:
    """Each trend/momentum spec behind each risk-off gate."""
    return [
        (f"{label} + riskoff {gate.name}", strategy_id, {**params, "riskoff_gate": gate})
        for gate in gates
        for label, strategy_id, params in specs
        if strategy_id in ("ma_trend", "xs_momentum")
    ]


def load_gate(
    warehouse: Warehouse,
    name: str,
    series_id: str,
    rule: str,
    threshold: Decimal,
    lag_sessions: int,
) -> RiskOffGate:
    observations = warehouse.load_series(series_id)
    if not observations:
        raise ValidationError(
            f"risk-off gate {name} needs series {series_id}, which has no observations "
            "in the warehouse; ingest it before running with --overlays"
        )
    return RiskOffGate(name, series_id, rule, threshold, lag_sessions, observations)


def load_riskoff_gates(warehouse: Warehouse) -> list[RiskOffGate]:
    return [load_gate(warehouse, *gate) for gate in RISKOFF_GATES]


def strategy_only_risk(risk: RiskConfig) -> RiskConfig:
    """The research-only risk config for a "strategy only" row.

    Identical to `risk` except the portfolio drawdown breaker, which is set to
    100% (it only fires if equity is gone). RiskConfig has no off switch and
    gets none: this is built here, handed to a research engine, and never
    reaches the paper runner or any default. Position cap, single-name cap,
    daily loss, staleness, order caps and participation are untouched.
    """
    return RiskConfig.model_validate(
        {**risk.model_dump(), "max_portfolio_drawdown": Decimal("1")}
    )


class StrategyOnly:
    """Labels a strategy as run with the drawdown breaker off. Signals pass through."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.strategy_id = f"{inner.strategy_id}_nobreaker"
        self.warmup_days = inner.warmup_days

    def parameters(self) -> dict[str, object]:
        return {**self.inner.parameters(), "risk_drawdown_breaker": "off"}

    def generate_signals(self, as_of, view):
        return self.inner.generate_signals(as_of, view)


def breaker_lockout_note(risk_events: list[RiskEvent]) -> str | None:
    """Say how long the drawdown breaker kept a book out of new positions.

    The breaker measures drawdown from the all-time peak and never resets, so
    after a deep enough loss every later entry is refused. That is the engine
    working as configured, but it makes the rest of the window cash.
    """
    days = sorted({e.event_date for e in risk_events if e.rule == "breaker_drawdown"})
    if not days:
        return None
    return (
        f"BREAKER LOCKOUT: the drawdown breaker refused risk-increasing orders on "
        f"{len(days)} session(s), first on {days[0].isoformat()}, last on "
        f"{days[-1].isoformat()}. Drawdown is measured from the run's peak equity and "
        "does not reset, so sessions after the first refusal may be held in cash "
        "by the breaker rather than by the strategy."
    )


def coverage_notes(
    history: PriceHistory,
    splits,
    benchmark: str,
    *,
    universe: tuple[str, ...] | None = None,
    summarise: bool = False,
) -> list[str]:
    """Per-window coverage: who has no bars, who lists mid-window, and when
    the benchmark starts. Late listings are a silent no-op in the engine, so
    they have to be said out loud here."""
    windows = (
        ("TRAIN", splits.train_start, splits.train_end,
         " (indicative only: most names are absent for much of it)"),
        ("VALIDATION", splits.validation_start, splits.validation_end, ""),
    )
    absent = sorted(set(universe or ()) - set(history.symbols))
    notes: list[str] = []
    for label, start, end, caveat in windows:
        empty: list[str] = list(absent)
        late: list[str] = []
        for symbol in history.symbols:
            if symbol == benchmark:
                continue
            in_window = [
                b.trade_date for b in history.bars_for(symbol) if start <= b.trade_date <= end
            ]
            if not in_window:
                empty.append(symbol)
            elif history.bars_for(symbol)[0].trade_date > start:
                late.append(f"{symbol} from {in_window[0].isoformat()}")
        bench_bars = history.bars_for(benchmark)
        parts = [f"COVERAGE {label} {start} .. {end}{caveat}."]
        if empty:
            names = summarise_symbols(sorted(empty)) if summarise else ", ".join(sorted(empty))
            parts.append(f"No bars in this window: {names}.")
        if late:
            names = summarise_symbols(late) if summarise else ", ".join(late)
            parts.append(f"Listing mid-window: {names}.")
        if bench_bars:
            parts.append(f"Benchmark {benchmark} from {bench_bars[0].trade_date.isoformat()}.")
        notes.append(" ".join(parts))
    return notes


def load_regime_labels(
    warehouse: Warehouse, series_id: str = "VIXCLS"
) -> tuple[dict[date, str], str]:
    """Point-in-time volatility regime labels, plus a note on their provenance.

    Returns an empty mapping and a reason when the series is unavailable, so a
    caller can say "not classified, because X" rather than rendering an empty
    regime table that reads as "no regimes found".
    """
    try:
        observations = warehouse.load_series(series_id)
    except Exception as exc:  # the series simply may not be ingested
        return {}, f"no regime labels: {series_id} could not be read ({exc})"
    if not observations:
        return {}, f"no regime labels: {series_id} has no observations in the warehouse"
    labels = volatility_regime_labels(observations)
    if not labels:
        return {}, (
            f"no regime labels: {series_id} has only {len(observations)} observations, "
            f"fewer than the {REGIME_WARMUP_SESSIONS}-session warmup needed to "
            "classify a regime without looking ahead"
        )
    days = sorted(labels)
    return labels, (
        f"Regimes from {series_id} terciles, classified point-in-time with a "
        f"{REGIME_WARMUP_SESSIONS}-session expanding warmup: {len(labels)} labelled "
        f"sessions from {days[0]} to {days[-1]}."
    )


def run_window(
    bundle: HistoryBundle,
    config: ConfigBundle,
    window: str,
    *,
    specs: list[tuple[str, str, dict]] | None = None,
    regime_labels: dict[date, str] | None = None,
    strategy_only: bool = False,
) -> ResearchRun:
    """Run the baseline suite over one named evaluation window.

    With `strategy_only`, every strategy except the buy-and-hold benchmark is
    also run on an engine with the drawdown breaker off, as a second row
    directly after its as-traded row.
    """
    splits = config.research.splits
    if splits is None:
        raise ValidationError("research config defines no splits; cannot run a named window")
    bounds = {
        "train": (splits.train_start, splits.train_end),
        "validation": (splits.validation_start, splits.validation_end),
        "test": (splits.test_start, splits.test_end),
    }
    if window not in bounds:
        raise ValidationError(f"unknown window {window!r}; expected one of {sorted(bounds)}")
    start, end = bounds[window]

    symbol = bundle.history.symbols[0]
    specs = specs or baseline_specs(symbol)
    engine = BacktestEngine(config.research, config.risk)
    free_engine = BacktestEngine(config.research, strategy_only_risk(config.risk))

    run = ResearchRun(
        window=window,
        start=start,
        end=end,
        dataset_description=bundle.description,
        dataset_fingerprint=BacktestEngine.fingerprint(bundle.history, start, end),
        limitations=list(bundle.limitations),
    )

    benchmark_curve = None
    for spec_label, strategy_id, params in specs:
        variants = [(spec_label, engine, False)]
        if strategy_only and strategy_id != "buy_and_hold":
            variants.append((f"{spec_label} (strategy only)", free_engine, True))
        for label, run_engine, free in variants:
            strategy = build(strategy_id, **params)
            if free:
                strategy = StrategyOnly(strategy)
            result = run_engine.run(
                strategy,
                bundle.history,
                start=start,
                end=end,
                run_id=f"{strategy.strategy_id}:{window}",
                data_limitations=list(bundle.limitations),
            )
            lockout = breaker_lockout_note(result.risk_events)
            if lockout:
                run.limitations.append(f"{label}: {lockout}")
            metrics = compute_metrics(
                result,
                benchmark_curve=benchmark_curve,
                config=config.research,
                regime_labels=regime_labels,
            )
            if strategy_id == "buy_and_hold":
                benchmark_curve = result.equity_curve
            run.outcomes.append(
                StrategyOutcome(
                    strategy_id=strategy.strategy_id,
                    label=label,
                    window=window,
                    result=result,
                    metrics=metrics,
                )
            )

    # Re-score everything against the benchmark now that it exists, so the
    # benchmark-relative figures are populated for every strategy.
    if benchmark_curve is not None:
        for outcome in run.outcomes:
            if outcome.strategy_id == "buy_and_hold":
                continue
            outcome.metrics = compute_metrics(
                outcome.result,
                benchmark_curve=benchmark_curve,
                config=config.research,
                regime_labels=regime_labels,
            )
    return run
