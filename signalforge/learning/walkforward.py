"""Walk-forward analysis.

Windows are cut from the sessions the history actually contains, never from a
calendar guess, so a holiday or a data gap cannot make a "100 day" window
quietly 96 sessions long. Within a fold the test window starts on the session
after the train window ends; the two cannot touch.

Each fold runs the strategy with fixed parameters on its train window and on
its out-of-sample window. The strategy is handed the whole history up to each
day by the engine, so a test fold gets its indicator warm-up from the sessions
before it, exactly as it would live, and nothing after.

Stitching: every fold starts a fresh book. The out-of-sample curve rescales
each fold so it begins where the previous one ended. The one-session move
between folds is not measured, and positions do not carry across a fold
boundary. When test windows overlap (step < test) each session is counted once,
from the earliest fold that covers it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from signalforge.backtesting.metrics import (
    EvidenceLevel,
    PerformanceMetrics,
    compute_metrics,
    drawdown_series,
)
from signalforge.core.errors import ValidationError
from signalforge.core.interfaces import BacktestResult, EquityPoint
from signalforge.learning.registry import StrategySpec

MIN_FOLDS = 3


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    train_sessions: int
    test_sessions: int
    train_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics

    @property
    def oos_return(self) -> Decimal | None:
        return self.test_metrics.total_return


@dataclass(frozen=True)
class SkippedFold:
    index: int
    test_start: date | None
    reason: str


@dataclass(frozen=True)
class Consistency:
    """Fraction of folds with a positive out-of-sample return, with the counts behind it."""

    fraction: Decimal | None
    positive_folds: int
    fold_count: int
    evidence: EvidenceLevel
    note: str


@dataclass
class WalkForwardReport:
    mode: str
    folds: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    stitched_curve: list = field(default_factory=list)
    aggregate_metrics: PerformanceMetrics | None = None
    consistency: Consistency | None = None
    evidence: EvidenceLevel = EvidenceLevel.INSUFFICIENT_EVIDENCE
    notes: list = field(default_factory=list)


def _positive_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{name} must be a positive integer, got {value!r}")
    return value


def _consistency(folds: list) -> Consistency:
    n = len(folds)
    positive = sum(1 for f in folds if f.oos_return is not None and f.oos_return > 0)
    fraction = Decimal(positive) / Decimal(n) if n else None
    if n < MIN_FOLDS:
        return Consistency(
            fraction, positive, n, EvidenceLevel.INSUFFICIENT_EVIDENCE,
            f"{n} fold(s) completed, below the minimum of {MIN_FOLDS}: a consistency "
            "fraction over so few folds is an anecdote, not a rate",
        )
    return Consistency(fraction, positive, n, EvidenceLevel.SUFFICIENT, "")


def _stitch(folds_curves: list[list[EquityPoint]], starting_cash: Decimal) -> list[EquityPoint]:
    stitched: list[EquityPoint] = []
    last_date: date | None = None
    for curve in folds_curves:
        if not curve:
            continue
        if last_date is None:
            factor = Decimal(1)
        else:
            anchor = next((p for p in curve if p.valuation_date == last_date), curve[0])
            factor = stitched[-1].equity / anchor.equity if anchor.equity > 0 else Decimal(1)
        for p in curve:
            if last_date is not None and p.valuation_date <= last_date:
                continue
            stitched.append(replace(
                p,
                equity=p.equity * factor, cash=p.cash * factor,
                gross_exposure=p.gross_exposure * factor,
                realized_pnl=p.realized_pnl * factor, unrealized_pnl=p.unrealized_pnl * factor,
            ))
        last_date = stitched[-1].valuation_date
    peak = None
    dds = drawdown_series(stitched)
    for i, p in enumerate(stitched):
        peak = p.equity if peak is None or p.equity > peak else peak
        stitched[i] = replace(p, drawdown=dds[i], peak_equity=peak)
    return stitched


def walk_forward(
    engine, strategy_spec: StrategySpec, history, config, *,
    train_sessions: int, test_sessions: int, step_sessions: int, anchored: bool = False,
    start: date | None = None, end: date | None = None,
) -> WalkForwardReport:
    _positive_int("train_sessions", train_sessions)
    _positive_int("test_sessions", test_sessions)
    _positive_int("step_sessions", step_sessions)
    days = [d for d in history.trading_days if (start is None or d >= start) and (end is None or d <= end)]
    n = len(days)
    report = WalkForwardReport(mode="anchored" if anchored else "rolling")

    if n <= train_sessions:
        report.skipped.append(SkippedFold(
            0, None,
            f"history has {n} session(s), not more than one train window of {train_sessions}; "
            "no out-of-sample session exists",
        ))
        report.consistency = _consistency([])
        return report

    fold_curves: list[list[EquityPoint]] = []
    fills = []
    limitations: list[str] = []
    test_start_idx = train_sessions
    index = 0
    while test_start_idx < n:
        test_end_idx = test_start_idx + test_sessions - 1
        if test_end_idx >= n:
            report.skipped.append(SkippedFold(
                index, days[test_start_idx],
                f"too few sessions: {n - test_start_idx} remain from {days[test_start_idx]} "
                f"but a test window needs {test_sessions}",
            ))
        else:
            train_start_idx = 0 if anchored else test_start_idx - train_sessions
            train_end_idx = test_start_idx - 1
            assert train_end_idx < test_start_idx
            train_result = engine.run(
                strategy_spec.build(), history, start=days[train_start_idx], end=days[train_end_idx]
            )
            test_result = engine.run(
                strategy_spec.build(), history, start=days[test_start_idx], end=days[test_end_idx]
            )
            report.folds.append(Fold(
                index=index,
                train_start=days[train_start_idx], train_end=days[train_end_idx],
                test_start=days[test_start_idx], test_end=days[test_end_idx],
                train_sessions=train_end_idx - train_start_idx + 1,
                test_sessions=test_end_idx - test_start_idx + 1,
                train_metrics=compute_metrics(train_result, config=config),
                test_metrics=compute_metrics(test_result, config=config),
            ))
            fold_curves.append(test_result.equity_curve)
            fills.append(test_result.fills)
            limitations.extend(x for x in test_result.data_limitations if x not in limitations)
        index += 1
        test_start_idx += step_sessions

    report.consistency = _consistency(report.folds)
    report.evidence = report.consistency.evidence
    if not report.folds:
        return report

    report.stitched_curve = _stitch(fold_curves, config.starting_cash)
    kept_fills = []
    covered_until = None
    for fold, fold_fills in zip(report.folds, fills):
        kept_fills.extend(f for f in fold_fills if covered_until is None or f.fill_date > covered_until)
        covered_until = fold.test_end
    aggregate = BacktestResult(
        strategy_id=strategy_spec.strategy_id,
        run_id=f"walkforward:{strategy_spec.strategy_id}:{report.mode}",
        start_date=report.stitched_curve[0].valuation_date,
        end_date=report.stitched_curve[-1].valuation_date,
        starting_cash=config.starting_cash,
        equity_curve=report.stitched_curve,
        fills=kept_fills,
        parameters=dict(strategy_spec.params),
        data_limitations=limitations,
        symbols=history.symbols,
        engine_version=getattr(engine, "ENGINE_VERSION", "0.1.0"),
        dataset_fingerprint=engine.fingerprint(history, days[0], days[-1]),
    )
    report.aggregate_metrics = compute_metrics(aggregate, config=config)
    report.notes.append(
        "Out-of-sample curve is stitched from independent fold books: positions do not "
        "carry across a fold boundary and the move between folds is not measured."
    )
    return report
