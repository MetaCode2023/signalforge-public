"""Performance metrics computed from a BacktestResult.

Precision choice: every figure is computed in `Decimal` (28 significant
digits) straight from the Decimal equity values, so `0.1 + 0.2`-style float
drift cannot move an answer. No float is used anywhere in this module. The
single approximation is CAGR's fractional power, which Decimal evaluates at
working precision.

Honesty rules, each pinned by a test:
  * A ratio that cannot be computed is None, never 0 and never inf.
  * Thin evidence is flagged, the figures are still computed, and the note
    says exactly which threshold failed and by how much.
  * There is no confidence score. None is invented or hidden.

Conventions: a period return is `equity[t] / equity[t-1] - 1`; periods whose
prior equity is not positive are skipped. Annualisation uses 252 sessions.
Drawdown is a positive fraction below the running peak. Closed trades are
position reductions, grouped per (symbol, order), costed at average cost with
fees included on both sides; open positions are not trades.
"""

from __future__ import annotations

import bisect
import decimal
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence

from signalforge.config.models import ResearchConfig
from signalforge.core.errors import ValidationError
from signalforge.core.interfaces import BacktestResult, EquityPoint
from signalforge.core.types import Side

ZERO = Decimal("0")
ONE = Decimal("1")
SESSIONS_PER_YEAR = 252
DAYS_PER_YEAR = Decimal("365.25")
ZERO_VARIANCE_TOLERANCE = Decimal("1E-15")
ANNUALISER = Decimal(SESSIONS_PER_YEAR).sqrt()
UNLABELLED = "UNLABELLED"


class EvidenceLevel(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class RegimeMetrics:
    label: str
    observations: int
    total_return: Decimal
    annualised_volatility: Decimal | None
    sharpe: Decimal | None
    max_drawdown: Decimal
    evidence: EvidenceLevel


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    observations: int
    total_return: Decimal | None
    cagr: Decimal | None
    annualised_volatility: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    max_drawdown: Decimal
    max_drawdown_duration_days: int
    max_drawdown_duration_sessions: int
    calmar: Decimal | None
    trade_count: int
    fill_count: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    avg_trade_return: Decimal | None
    turnover: Decimal | None
    exposure: Decimal | None
    """Time in market: the fraction of sessions with any position open.

    This is NOT how much capital was deployed. A strategy holding a 5%
    position every single day scores ~1.0 here while 95% of the book sits in
    cash. Read it with `average_capital_deployed`.
    """
    average_capital_deployed: Decimal | None
    """Mean gross exposure divided by equity, across the curve.

    Added because `exposure` alone let a portfolio-level CAGR and Sharpe be
    read as the strategy's own performance when the position cap meant only a
    small slice of the book was ever at risk. On a one-symbol universe under a
    5% single-name cap this lands near 0.05, which is the number a reader
    needs in order to interpret every other figure here.
    """
    peak_capital_deployed: Decimal | None
    total_transaction_costs: Decimal
    total_slippage: Decimal
    benchmark_relative_return: Decimal | None
    tracking_error: Decimal | None
    information_ratio: Decimal | None
    evidence: EvidenceLevel
    evidence_note: str
    regimes: Mapping[str, RegimeMetrics] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready. Decimals become strings (lossless), enums their value."""
        return _jsonable(asdict(self))


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# --- building blocks -----------------------------------------------------

def drawdown_series(equity_curve: Sequence[EquityPoint]) -> list[Decimal]:
    """Per-point drawdown as a positive fraction below the running peak."""
    out: list[Decimal] = []
    peak = None
    for point in equity_curve:
        e = point.equity
        peak = e if peak is None or e > peak else peak
        out.append((peak - e) / peak if peak > 0 else ZERO)
    return out


def regime_labels_from_drawdown(
    benchmark_curve: Sequence[EquityPoint], threshold: Decimal
) -> dict[date, str]:
    """DRAWDOWN where the benchmark is `threshold` or more below its peak, else NORMAL."""
    threshold = Decimal(threshold)
    if not (ZERO < threshold <= ONE):
        raise ValidationError(f"drawdown threshold must be in (0, 1], got {threshold}")
    return {
        p.valuation_date: ("DRAWDOWN" if dd >= threshold else "NORMAL")
        for p, dd in zip(benchmark_curve, drawdown_series(benchmark_curve))
    }


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / len(values)


def _sample_std(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    m = _mean(values)
    return (sum(((v - m) ** 2 for v in values), ZERO) / (len(values) - 1)).sqrt()


def _returns(curve: Sequence[EquityPoint]) -> list[tuple[date, date, Decimal]]:
    """(previous date, date, simple return) for each period with a positive prior equity."""
    out = []
    for prev, cur in zip(curve, curve[1:]):
        if prev.equity > 0:
            out.append((prev.valuation_date, cur.valuation_date, cur.equity / prev.equity - ONE))
    return out


def _risk_free_daily(series: Mapping[date, Decimal] | None, day: date) -> Decimal:
    """Annual rate in force on or before `day`, as a per-session rate. Zero before the first."""
    if not series:
        return ZERO
    keys = sorted(series)
    i = bisect.bisect_right(keys, day)
    return Decimal(series[keys[i - 1]]) / SESSIONS_PER_YEAR if i else ZERO


def _sharpe(excess: Sequence[Decimal]) -> Decimal | None:
    std = _sample_std(excess)
    if std is None or std < ZERO_VARIANCE_TOLERANCE:
        return None
    return _mean(excess) / std * ANNUALISER


def _sortino(excess: Sequence[Decimal]) -> Decimal | None:
    if len(excess) < 2:
        return None
    downside = (sum((min(x, ZERO) ** 2 for x in excess), ZERO) / len(excess)).sqrt()
    if downside < ZERO_VARIANCE_TOLERANCE:
        return None
    return _mean(excess) / downside * ANNUALISER


def _annualised_vol(returns: Sequence[Decimal]) -> Decimal | None:
    std = _sample_std(returns)
    return None if std is None else std * ANNUALISER


def _max_drawdown_and_durations(curve: Sequence[EquityPoint]) -> tuple[Decimal, int, int]:
    dds = drawdown_series(curve)
    max_dd = max(dds, default=ZERO)
    max_days = max_sessions = 0
    peak_val = peak_idx = None
    dipped = False
    for i, point in enumerate(curve):
        if peak_val is None or point.equity >= peak_val:
            if dipped:
                max_days = max(max_days, (point.valuation_date - curve[peak_idx].valuation_date).days)
                max_sessions = max(max_sessions, i - peak_idx)
            peak_val, peak_idx, dipped = point.equity, i, False
        else:
            dipped = True
    if dipped:
        max_days = max(max_days, (curve[-1].valuation_date - curve[peak_idx].valuation_date).days)
        max_sessions = max(max_sessions, len(curve) - 1 - peak_idx)
    return max_dd, max_days, max_sessions


def _cagr(curve: Sequence[EquityPoint]) -> Decimal | None:
    if len(curve) < 2:
        return None
    start, end = curve[0], curve[-1]
    days = (end.valuation_date - start.valuation_date).days
    if days <= 0 or start.equity <= 0:
        return None
    if end.equity <= 0:
        return -ONE
    try:
        return (end.equity / start.equity) ** (DAYS_PER_YEAR / days) - ONE
    except decimal.DecimalException:
        return None


def closed_trades(fills) -> list[tuple[Decimal, Decimal]]:
    """(cost basis, net proceeds) per closed trade, average-cost, fees included."""
    held: dict[str, list[Decimal]] = {}
    trades: dict[tuple[str, str], list[Decimal]] = {}
    for f in sorted(fills, key=lambda f: f.fill_date):
        pos = held.setdefault(f.symbol, [ZERO, ZERO])
        if f.side is Side.BUY:
            pos[0] += f.quantity
            pos[1] += f.price * f.quantity + f.fees
            continue
        if pos[0] <= 0:
            continue
        qty = min(Decimal(f.quantity), pos[0])
        basis = pos[1] * qty / pos[0]
        pos[0] -= qty
        pos[1] -= basis
        t = trades.setdefault((f.symbol, f.order_id), [ZERO, ZERO])
        t[0] += basis
        t[1] += f.price * qty - f.fees
    return [(b, p) for b, p in trades.values()]


def _evidence(trade_count: int, observations: int, config: ResearchConfig) -> tuple[EvidenceLevel, str]:
    notes = []
    if trade_count < config.min_trades_for_evidence:
        notes.append(
            f"closed trade count {trade_count} is below the minimum of "
            f"{config.min_trades_for_evidence} (short by {config.min_trades_for_evidence - trade_count})"
        )
    if observations < config.min_observations_for_metrics:
        notes.append(
            f"observations {observations} is below the minimum of "
            f"{config.min_observations_for_metrics} "
            f"(short by {config.min_observations_for_metrics - observations})"
        )
    if notes:
        return EvidenceLevel.INSUFFICIENT_EVIDENCE, "; ".join(notes)
    return EvidenceLevel.SUFFICIENT, ""


def _benchmark_relative(
    curve: Sequence[EquityPoint], benchmark: Sequence[EquityPoint]
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    bench = {p.valuation_date: p.equity for p in benchmark}
    common = [p for p in curve if p.valuation_date in bench]
    if len(common) < 2 or common[0].equity <= 0 or bench[common[0].valuation_date] <= 0:
        return None, None, None
    first, last = common[0], common[-1]
    relative = (last.equity / first.equity - ONE) - (
        bench[last.valuation_date] / bench[first.valuation_date] - ONE
    )
    active: list[Decimal] = []
    for prev, cur in zip(common, common[1:]):
        b_prev, b_cur = bench[prev.valuation_date], bench[cur.valuation_date]
        if prev.equity > 0 and b_prev > 0:
            active.append((cur.equity / prev.equity - ONE) - (b_cur / b_prev - ONE))
    std = _sample_std(active)
    if std is None:
        return relative, None, None
    te = std * ANNUALISER
    ir = None if std < ZERO_VARIANCE_TOLERANCE else _mean(active) * SESSIONS_PER_YEAR / te
    return relative, te, ir


def _regime_metrics(
    returns: Sequence[tuple[date, date, Decimal]],
    labels: Mapping[date, str],
    config: ResearchConfig,
) -> dict[str, RegimeMetrics]:
    """Each period is labelled by the regime known at the START of the period,
    so a label derived from the period's own close cannot leak into it."""
    buckets: dict[str, list[Decimal]] = {}
    for prev_date, _, r in returns:
        buckets.setdefault(labels.get(prev_date, UNLABELLED), []).append(r)
    out: dict[str, RegimeMetrics] = {}
    for label in sorted(buckets):
        rs = buckets[label]
        index, peak, worst = ONE, ONE, ZERO
        for r in rs:
            index *= ONE + r
            peak = max(peak, index)
            worst = max(worst, (peak - index) / peak) if peak > 0 else worst
        out[label] = RegimeMetrics(
            label=label,
            observations=len(rs),
            total_return=index - ONE,
            annualised_volatility=_annualised_vol(rs),
            sharpe=_sharpe(rs),
            max_drawdown=worst,
            evidence=(
                EvidenceLevel.SUFFICIENT
                if len(rs) >= config.min_observations_for_metrics
                else EvidenceLevel.INSUFFICIENT_EVIDENCE
            ),
        )
    return out


def _capital_deployed_ratios(curve) -> list[Decimal]:
    """gross_exposure / equity per session, skipping sessions with no equity."""
    return [
        p.gross_exposure / p.equity for p in curve if p.equity > 0
    ]


def _average_capital_deployed(curve) -> Decimal | None:
    ratios = _capital_deployed_ratios(curve)
    if not ratios:
        return None
    return sum(ratios) / Decimal(len(ratios))


def _peak_capital_deployed(curve) -> Decimal | None:
    ratios = _capital_deployed_ratios(curve)
    return max(ratios) if ratios else None


def compute_metrics(
    result: BacktestResult,
    benchmark_curve: Sequence[EquityPoint] | None = None,
    risk_free_series: Mapping[date, Decimal] | None = None,
    config: ResearchConfig | None = None,
    regime_labels: Mapping[date, str] | None = None,
) -> PerformanceMetrics:
    """`risk_free_series` maps date -> annualised risk-free rate (0.05 = 5%),
    forward-filled; `regime_labels` maps date -> label (see
    `regime_labels_from_drawdown`)."""
    config = config or ResearchConfig()
    curve = result.equity_curve
    returns = _returns(curve)
    excess = [r - _risk_free_daily(risk_free_series, prev) for prev, _, r in returns]
    plain = [r for _, _, r in returns]

    max_dd, dd_days, dd_sessions = _max_drawdown_and_durations(curve)
    cagr = _cagr(curve)
    total_return = (
        curve[-1].equity / curve[0].equity - ONE if len(curve) >= 2 and curve[0].equity > 0 else None
    )

    trades = closed_trades(result.fills)
    pnls = [p - b for b, p in trades if b > 0]
    gross_win = sum((x for x in pnls if x > 0), ZERO)
    gross_loss = -sum((x for x in pnls if x < 0), ZERO)
    trade_returns = [(p - b) / b for b, p in trades if b > 0]

    mean_equity = _mean([p.equity for p in curve]) if curve else ZERO
    traded = sum((f.price * f.quantity for f in result.fills), ZERO)

    relative = te = ir = None
    if benchmark_curve:
        relative, te, ir = _benchmark_relative(curve, benchmark_curve)

    evidence, note = _evidence(len(trades), len(curve), config)

    return PerformanceMetrics(
        observations=len(curve),
        total_return=total_return,
        cagr=cagr,
        annualised_volatility=_annualised_vol(plain),
        sharpe=_sharpe(excess),
        sortino=_sortino(excess),
        max_drawdown=max_dd,
        max_drawdown_duration_days=dd_days,
        max_drawdown_duration_sessions=dd_sessions,
        calmar=cagr / max_dd if cagr is not None and max_dd > 0 else None,
        trade_count=len(trades),
        fill_count=len(result.fills),
        win_rate=(Decimal(sum(1 for x in pnls if x > 0)) / len(pnls)) if pnls else None,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        avg_trade_return=_mean(trade_returns) if trade_returns else None,
        turnover=(traded / 2 / mean_equity) if mean_equity > 0 else None,
        exposure=(Decimal(sum(1 for p in curve if p.position_count > 0)) / len(curve)) if curve else None,
        average_capital_deployed=_average_capital_deployed(curve),
        peak_capital_deployed=_peak_capital_deployed(curve),
        total_transaction_costs=sum((f.fees for f in result.fills), ZERO),
        total_slippage=sum((f.slippage_cost for f in result.fills), ZERO),
        benchmark_relative_return=relative,
        tracking_error=te,
        information_ratio=ir,
        evidence=evidence,
        evidence_note=note,
        regimes=_regime_metrics(returns, regime_labels, config) if regime_labels else {},
    )


# Public alias kept because learning/overfitting.py imported the private name.
# A rename there would have broken it silently, which is the kind of coupling
# that only shows up at runtime.
_closed_trades = closed_trades
