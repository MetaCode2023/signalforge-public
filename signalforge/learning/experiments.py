"""Running an experiment: one strategy, one window, one registry row.

The window is named, never a pair of dates the caller picks, so the splits in
config are the only definition of "train", "validation" and "test". Tuning is
allowed on train and nowhere else: a parameter chosen by looking at validation
or test is fitted to it, and the number it then reports is not out-of-sample.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from signalforge.backtesting.metrics import PerformanceMetrics, compute_metrics
from signalforge.core.errors import ValidationError
from signalforge.core.interfaces import BacktestResult
from signalforge.learning.registry import WINDOWS, ExperimentRegistry, StrategySpec


@dataclass(frozen=True)
class ExperimentRun:
    experiment_id: str
    window: str
    start: date
    end: date
    result: BacktestResult
    metrics: PerformanceMetrics


@dataclass(frozen=True)
class Trial:
    parameters: dict
    experiment_id: str
    metrics: PerformanceMetrics


@dataclass(frozen=True)
class TuneResult:
    window: str
    trials: list
    best: Trial | None


def window_bounds(config, window: str) -> tuple[date, date]:
    """(start, end) of a named window, from config.splits and nowhere else."""
    if window not in WINDOWS:
        raise ValidationError(f"window must be one of {WINDOWS}, got {window!r}")
    if config.splits is None:
        raise ValidationError(
            f"config defines no train/validation/test splits, so the {window!r} "
            "window does not exist; refusing to invent one"
        )
    return getattr(config.splits, f"{window}_start"), getattr(config.splits, f"{window}_end")


def cost_assumptions(config) -> dict:
    ex = config.execution
    return {
        "commission_per_share": str(ex.commission_per_share),
        "min_commission": str(ex.min_commission),
        "slippage_bps": str(ex.slippage_bps),
        "fill_delay_days": ex.fill_delay_days,
        "max_participation_of_volume": str(ex.max_participation_of_volume),
        "allow_partial_fills": ex.allow_partial_fills,
        "gap_slippage_multiplier": str(ex.gap_slippage_multiplier),
    }


def run_experiment(
    engine, strategy_spec: StrategySpec, history, config, registry: ExperimentRegistry, *,
    hypothesis: str, window: str, data_limitations: list[str] | None = None,
) -> ExperimentRun:
    try:
        return _run_experiment(engine, strategy_spec, history, config, registry,
                               hypothesis=hypothesis, window=window, data_limitations=data_limitations)
    except Exception as exc:
        try:
            start, end = window_bounds(config, window)
            fingerprint = engine.fingerprint(history, start, end)
        except Exception:
            fingerprint = None
        try:
            registry.record_failure(strategy_spec=strategy_spec, window=window,
                                    fingerprint=fingerprint, seed=config.random_seed,
                                    exception_type=type(exc).__name__)
        except Exception as audit_error:
            exc.add_note(f"Failure audit could not be persisted ({type(audit_error).__name__})")
        raise


def _run_experiment(
    engine, strategy_spec: StrategySpec, history, config, registry: ExperimentRegistry, *,
    hypothesis: str, window: str, data_limitations: list[str] | None = None,
) -> ExperimentRun:
    start, end = window_bounds(config, window)
    days = [d for d in history.trading_days if start <= d <= end]
    if not days:
        raise ValidationError(
            f"history has no trading days inside the {window} window {start}..{end}"
        )
    costs = cost_assumptions(config)

    if window == "test":
        fingerprint = engine.fingerprint(history, days[0], days[-1])
        registry.guard_test_window(
            strategy_name=strategy_spec.strategy_id,
            parameters=strategy_spec.params,
            dataset_fingerprint=fingerprint,
            experiment_id=registry.experiment_id_for(
                strategy_name=strategy_spec.strategy_id,
                strategy_version=strategy_spec.version,
                parameters=strategy_spec.params,
                dataset_fingerprint=fingerprint,
                window=window, splits=config.splits, transaction_cost_assumptions=costs,
            ),
        )

    result = engine.run(
        strategy_spec.build(), history, start=start, end=end,
        run_id=f"{strategy_spec.strategy_id}:{window}:{start}:{end}",
        data_limitations=data_limitations,
    )
    metrics = compute_metrics(result, config=config)
    description = (
        f"{','.join(result.symbols) or ','.join(history.symbols)} {result.start_date}..{result.end_date}, "
        f"{result.bar_count} bars; limitations: {' | '.join(result.data_limitations) or 'none recorded'}"
    )
    experiment_id = registry.record(
        strategy_name=strategy_spec.strategy_id,
        strategy_version=strategy_spec.version,
        parameters=strategy_spec.params,
        dataset_fingerprint=result.dataset_fingerprint,
        dataset_description=description,
        window=window,
        splits=config.splits,
        transaction_cost_assumptions=costs,
        engine_version=result.engine_version,
        random_seed=config.random_seed,
        hypothesis=hypothesis,
        metrics=metrics.to_dict(),
        max_drawdown=metrics.max_drawdown,
        trade_count=metrics.trade_count,
        evidence_level=metrics.evidence,
    )
    return ExperimentRun(experiment_id, window, result.start_date, result.end_date, result, metrics)


def _objective(metrics: PerformanceMetrics):
    sharpe = float(metrics.sharpe) if metrics.sharpe is not None else float("-inf")
    ret = float(metrics.total_return) if metrics.total_return is not None else float("-inf")
    return (sharpe, ret)


def tune(
    engine, strategy_spec: StrategySpec, history, config, registry: ExperimentRegistry, *,
    grid: dict, hypothesis: str, window: str = "train",
) -> TuneResult:
    """Sweep a parameter grid on the train window. Anything else is refused."""
    if window != "train":
        raise ValidationError(
            f"tuning is only allowed on the 'train' window, not {window!r}: choosing "
            "parameters by their score on validation or test fits them to that data"
        )
    if not grid or any(len(v) == 0 for v in grid.values()):
        raise ValidationError("tune needs a non-empty grid with at least one value per parameter")
    names = sorted(grid)
    trials = []
    for combo in itertools.product(*(grid[n] for n in names)):
        spec = strategy_spec.with_params(**dict(zip(names, combo)))
        run = run_experiment(engine, spec, history, config, registry,
                             hypothesis=hypothesis, window="train")
        trials.append(Trial(dict(spec.params), run.experiment_id, run.metrics))
    best = max(trials, key=lambda t: _objective(t.metrics))
    return TuneResult("train", trials, best)
