"""The research entry point.

    python -m signalforge.research_cli [--run-holdout] [--no-dashboard]

Runs the baseline suite over the train and validation windows, records every
run in the experiment registry, and renders the operator dashboard from the
results it just produced rather than from anything cached.

The held-out test window is NOT run unless `--run-holdout` is passed. It can
honestly be looked at once, and the registry refuses a second look, so
consuming it has to be a deliberate act rather than a side effect of running
the command again.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from signalforge.config.models import ConfigBundle, ResearchConfig, load_all
from signalforge.core.errors import SignalForgeError
from signalforge.data.storage import Warehouse
from signalforge.learning.registry import ExperimentRegistry
from signalforge.pipeline import (
    ResearchRun,
    baseline_specs,
    load_index_bundle,
    load_regime_labels,
    run_window,
)

DEFAULT_DB = Path("data") / "warehouse" / "signalforge.duckdb"
DEFAULT_OUT = Path("dashboard") / "build" / "index.html"

HOLDOUT_NOT_CONSUMED = (
    "The held-out test window was NOT run. It is reported as untouched so that "
    "the one honest out-of-sample number this project has stays unspent. Pass "
    "--run-holdout to consume it; the registry then refuses a second look."
)


@dataclass(slots=True)
class ResearchOutput:
    runs: list[ResearchRun] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    experiment_ids: list[str] = field(default_factory=list)

    @property
    def backtests(self) -> list:
        return [o.result for run in self.runs for o in run.outcomes]

    def run_for(self, window: str) -> ResearchRun | None:
        for run in self.runs:
            if run.window == window:
                return run
        return None


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m signalforge.research_cli")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--series", default="NASDAQCOM")
    parser.add_argument(
        "--run-holdout",
        action="store_true",
        help="consume the held-out test window; it can only be evaluated once",
    )
    parser.add_argument("--no-dashboard", dest="dashboard", action="store_false")
    parser.set_defaults(dashboard=True)
    return parser.parse_args(argv)


def run_research(
    warehouse: Warehouse,
    research: ResearchConfig,
    *,
    series_id: str = "NASDAQCOM",
    run_holdout: bool = False,
    registry: ExperimentRegistry | None = None,
) -> ResearchOutput:
    from signalforge.config.models import RiskConfig

    bundle = load_index_bundle(warehouse, series_id)
    config = ConfigBundle(
        research=research,
        risk=RiskConfig(),
        data_sources=_safe_data_sources(),
    )
    registry = registry or ExperimentRegistry(warehouse)
    output = ResearchOutput()
    output.notes.append(bundle.description)

    regime_labels, regime_note = load_regime_labels(warehouse)
    output.notes.append(regime_note)

    windows = ["train", "validation"]
    if run_holdout:
        spent = _holdout_already_spent(registry, bundle, config)
        if spent:
            output.notes.append(
                "The held-out test window was NOT run: it has ALREADY been spent for "
                f"{', '.join(spent)} on this dataset. The figures are deliberately not "
                "recomputed or shown. A held-out period looked at twice is a tuning "
                "set, and a caveat beside the numbers does not undo having seen them. "
                "Change the strategy, the parameters or the dataset to earn a new look."
            )
        else:
            windows.append("test")

    for window in windows:
        run = run_window(bundle, config, window, regime_labels=regime_labels)
        output.runs.append(run)
        output.experiment_ids.extend(_record(registry, run, config))

    if not run_holdout:
        output.notes.append(HOLDOUT_NOT_CONSUMED)
    else:
        output.notes.append(
            "The held-out test window HAS now been consumed for the strategy and "
            "parameter sets in this run. A second evaluation of the same "
            "combination will be refused by the registry."
        )
    return output


def _holdout_already_spent(
    registry: ExperimentRegistry, bundle, config: ConfigBundle
) -> list[str]:
    """Names of the baseline strategies whose hold-out look is already spent.

    Consulted BEFORE the test window runs. Checking afterwards and refusing
    only to *record* the result still hands the researcher the exact
    out-of-sample figures the guard exists to protect.
    """
    splits = config.research.splits
    if splits is None:
        return []
    from signalforge.backtesting.engine import BacktestEngine

    fingerprint = BacktestEngine.fingerprint(
        bundle.history, splits.test_start, splits.test_end
    )
    symbol = bundle.history.symbols[0]
    spent: list[str] = []
    for label, strategy_id, params in baseline_specs(symbol):
        if registry.holdout_spent(
            strategy_name=strategy_id,
            parameters=_stringify(params),
            dataset_fingerprint=fingerprint,
        ):
            spent.append(label)
            # Nothing is re-run, but record that re-opening was asked for.
            registry.log_holdout_attempt(
                strategy_name=strategy_id,
                parameters=_stringify(params),
                dataset_fingerprint=fingerprint,
                experiment_id="blocked-before-run",
            )
    return spent


def _safe_data_sources():
    from signalforge.config.models import DataSourcesConfig, load_data_sources

    try:
        return load_data_sources()
    except SignalForgeError:
        return DataSourcesConfig(providers={})


def _record(
    registry: ExperimentRegistry, run: ResearchRun, config: ConfigBundle
) -> list[str]:
    execution = config.research.execution
    costs = {
        "commission_per_share": str(execution.commission_per_share),
        "min_commission": str(execution.min_commission),
        "slippage_bps": str(execution.slippage_bps),
        "fill_delay_days": execution.fill_delay_days,
        "gap_slippage_multiplier": str(execution.gap_slippage_multiplier),
        "max_participation_of_volume": str(execution.max_participation_of_volume),
    }
    recorded: list[str] = []
    for outcome in run.outcomes:
        try:
            recorded.append(
                registry.record(
                    strategy_name=outcome.strategy_id,
                    strategy_version=outcome.result.engine_version,
                    parameters=_stringify(outcome.result.parameters),
                    dataset_fingerprint=run.dataset_fingerprint,
                    dataset_description=run.dataset_description,
                    window=run.window,
                    splits=config.research.splits,
                    transaction_cost_assumptions=costs,
                    engine_version=outcome.result.engine_version,
                    random_seed=config.research.random_seed,
                    hypothesis=(
                        f"{outcome.label} produces a better risk-adjusted return "
                        f"than buy-and-hold on {run.dataset_description}"
                    ),
                    metrics=outcome.metrics.to_dict(),
                    max_drawdown=outcome.metrics.max_drawdown,
                    trade_count=outcome.metrics.trade_count,
                    evidence_level=outcome.metrics.evidence,
                )
            )
        except SignalForgeError as exc:
            # A refused hold-out re-evaluation is the registry doing its job.
            # It must not take the whole run down with it.
            run.limitations.append(
                f"{outcome.label} was not recorded for the {run.window} window: {exc}"
            )
    return recorded


def _stringify(params: dict) -> dict:
    out = {}
    for key, value in params.items():
        if isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, (tuple, list)):
            out[key] = [str(v) for v in value]
        else:
            out[key] = value
    return out


def write_dashboard_for(
    output: ResearchOutput,
    warehouse: Warehouse,
    config: ConfigBundle,
    out: Path,
) -> Path:
    """Render the dashboard from the results of the run just performed."""
    from signalforge.dashboard.render import write_dashboard

    benchmark_curve = None
    validation = output.run_for("validation") or (output.runs[0] if output.runs else None)
    if validation is not None:
        benchmark = validation.by_id("buy_and_hold")
        if benchmark is not None:
            benchmark_curve = benchmark.result.equity_curve
    from signalforge.learning.overfitting import family_wise_summary
    from signalforge.learning.registry import ExperimentRegistry
    from signalforge.pipeline import load_regime_labels

    regime_labels, _ = load_regime_labels(warehouse)
    try:
        bias = family_wise_summary(ExperimentRegistry(warehouse))
    except Exception:
        bias = None
    return write_dashboard(
        out,
        warehouse=warehouse,
        backtests=output.backtests,
        config=config,
        benchmark_curve=benchmark_curve,
        regime_labels=regime_labels,
        selection_bias=bias,
    )


def format_table(output: ResearchOutput) -> str:
    lines: list[str] = []
    for run in output.runs:
        lines.append("")
        lines.append(
            f"=== {run.window.upper()}  {run.start} .. {run.end}  "
            f"dataset {run.dataset_fingerprint[:12]}"
        )
        lines.append(
            f"{'strategy':<34}{'totRet':>9}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}"
            f"{'trades':>7}{'capDep':>8}  evidence"
        )
        for outcome in run.outcomes:
            m = outcome.metrics
            sharpe = f"{m.sharpe:.2f}" if m.sharpe is not None else "n/a"
            cagr = f"{m.cagr:.2%}" if m.cagr is not None else "n/a"
            cap = (
                f"{m.average_capital_deployed:.1%}"
                if m.average_capital_deployed is not None
                else "n/a"
            )
            lines.append(
                f"{outcome.label:<34}{outcome.total_return:>8.1%}{cagr:>8}{sharpe:>8}"
                f"{m.max_drawdown:>7.1%}{m.trade_count:>7}{cap:>8}  {m.evidence.value}"
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.db.exists():
        print(f"no warehouse at {args.db}; run the archive ingestion first")
        return 2
    config = load_all()
    warehouse = Warehouse(str(args.db))
    try:
        output = run_research(
            warehouse,
            config.research,
            series_id=args.series,
            run_holdout=args.run_holdout,
        )
        print(format_table(output))
        print()
        for note in output.notes:
            print("NOTE:", note)
        print()
        print("LIMITATIONS (these travel with every number above):")
        seen = set()
        for run in output.runs:
            for note in run.limitations:
                if note not in seen:
                    seen.add(note)
                    print("  -", note)
        if args.dashboard:
            written = write_dashboard_for(output, warehouse, config, args.out)
            print()
            print(f"dashboard: {written} ({written.stat().st_size} bytes)")
        print(f"experiments recorded: {len(output.experiment_ids)}")
    finally:
        warehouse.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
