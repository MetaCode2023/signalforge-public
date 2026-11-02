"""The research entry point.

    python -m signalforge.research_cli [--run-holdout] [--no-dashboard]
        [--series ID | --symbols A,B | --universe | --universe-file PATH]
        [--overlays] [--benchmark SPY] [--source tiingo]

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
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from signalforge.config.models import (
    ConfigBundle,
    ResearchConfig,
    UniverseConfig,
    load_all,
    load_universe,
)
from signalforge.core.errors import ConfigError, SignalForgeError, ValidationError
from signalforge.data.storage import Warehouse
from signalforge.learning.registry import ExperimentRegistry
from signalforge.pipeline import (
    EXPOSURE_MISMATCH_NOTE,
    SURVIVORSHIP_NOTE,
    HistoryBundle,
    ResearchRun,
    baseline_specs,
    broad_specs,
    coverage_notes,
    load_index_bundle,
    load_regime_labels,
    load_riskoff_gates,
    ohlcv_history,
    overlay_specs,
    run_window,
)

DEFAULT_DB = Path("data") / "warehouse" / "signalforge.duckdb"
DEFAULT_OUT = Path("dashboard") / "build" / "index.html"

HOLDOUT_NOT_CONSUMED = (
    "The held-out test window was NOT run. It is reported as untouched so that "
    "the one honest out-of-sample number this project has stays unspent. Pass "
    "--run-holdout to consume it; the registry then refuses a second look."
)

OHLCV_HOLDOUT_REFUSED = (
    "--run-holdout is refused for OHLCV runs: the hold-out is keyed to the dataset "
    "fingerprint, and the warehouse bars and their adjustment factors are revised "
    "by the provider, so there is no revision-stable dataset ID to key a single "
    "look against. Nothing was run and the registry is unchanged."
)

OVERLAYS_REFUSED_HOLDOUT = (
    "--run-holdout is refused with --overlays and with --universe-file: those runs "
    "have no revision-stable dataset ID to key a single look against, and overlay "
    "variants multiply the number of looks. Nothing was run and the registry is unchanged."
)

# Calendar days of history loaded before the validation window, so every
# lookback (252-session eligibility, 200-day MA, 147-day momentum) is warm on
# its first day and the results match a load of the whole history.
WINDOW_WARMUP_DAYS = 1095

BROAD_UNIVERSE_NOTE = (
    "BROAD UNIVERSE {name}: {count} committed symbols, bars for this window loaded "
    "{load_start} .. {load_end} only; bars after the validation window are never loaded. The "
    "train window here is {train_start} .. {train_end} ({years} years), shorter than "
    "the research config train window. The symbol list is the set of names "
    "that have bars in the warehouse today, so it is not a list anyone could have "
    "written at the time."
)

ELIGIBILITY_NOTE = (
    "ELIGIBILITY: each day a name is only tradeable if it has at least "
    "{min_history_sessions} visible sessions, a latest bar no older than {max_stale_days} "
    "days (the risk engine staleness limit), and a median close x volume over its last "
    "{dollar_volume_window} sessions of at least {min_median_dollar_volume}. Ineligible "
    "names get an explicit exit. The screen uses only data visible that day, but the "
    "list it screens is still today's survivors, so it narrows survivorship bias "
    "without removing it. Breadth makes survivorship worse, not better: more names "
    "that survived to today."
)

REGIME_GATE_NOTE = (
    "REGIME GATE {name} ({series} {rule} {threshold}, {lag} session lag) {window}: "
    "risk-off {off}, risk-on {on}, unknown {unknown} sessions. Unknown (no observation "
    "within 10 days) is treated as risk-on. Values are the latest published figures, "
    "not vintage-dated (no ALFRED), so a revised value is used as if it were always known."
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--series", default="NASDAQCOM")
    mode.add_argument(
        "--symbols",
        type=lambda v: [s.strip().upper() for s in v.split(",") if s.strip()],
        default=None,
        help="comma-separated symbols to trade from the warehouse OHLCV bars",
    )
    mode.add_argument(
        "--universe",
        action="store_true",
        help="trade the research.yaml universe from the warehouse OHLCV bars",
    )
    mode.add_argument(
        "--universe-file",
        type=Path,
        default=None,
        help="trade the explicit symbol list in a universe YAML (see config/universes/)",
    )
    parser.add_argument(
        "--overlays",
        action="store_true",
        help="also run the trend and momentum strategies behind each risk-off gate",
    )
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--source", default="tiingo")
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
    symbols: list[str] | None = None,
    benchmark: str = "SPY",
    source: str | None = None,
    run_holdout: bool = False,
    registry: ExperimentRegistry | None = None,
    universe: UniverseConfig | None = None,
    overlays: bool = False,
) -> ResearchOutput:
    from signalforge.config.models import RiskConfig

    if universe is not None:
        symbols, benchmark, source = list(universe.symbols), universe.benchmark, universe.source
    if overlays and symbols is None:
        raise ValidationError("--overlays needs an OHLCV run (--symbols, --universe or --universe-file)")
    if (overlays or universe is not None) and run_holdout:
        raise ValidationError(OVERLAYS_REFUSED_HOLDOUT)
    if symbols is not None:
        if not symbols:
            raise ValidationError(
                "symbols is empty: refusing to fall back to the series path"
            )
        if run_holdout:
            raise ValidationError(OHLCV_HOLDOUT_REFUSED)
        if source is None:
            raise ValidationError(
                "an OHLCV run needs an explicit source: the warehouse can hold the "
                "same symbol from several datasets and mixing them is refused"
            )
        if universe is not None:
            bundle, specs, research = _universe_bundle(
                warehouse, universe, research, RiskConfig().stale_data_max_age_days, "train"
            )
        else:
            bundle, specs = _ohlcv_bundle(warehouse, symbols, benchmark, source)
    else:
        bundle = load_index_bundle(warehouse, series_id)
        specs = baseline_specs(bundle.history.symbols[0])
    config = ConfigBundle(
        research=research,
        risk=RiskConfig(),
        data_sources=_safe_data_sources(),
    )
    gates = []
    if overlays:
        gates = load_riskoff_gates(warehouse)
        specs = specs + overlay_specs(specs, gates)
    registry = registry or ExperimentRegistry(warehouse)
    output = ResearchOutput()
    output.notes.append(bundle.description)

    regime_labels, regime_note = load_regime_labels(warehouse)
    output.notes.append(regime_note)

    windows = ["train", "validation"]
    if run_holdout:
        spent = _holdout_already_spent(registry, bundle, config, specs)
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
        if universe is not None and window != "train":
            bundle = None  # release the train bars before loading the next window
            bundle, _, _ = _universe_bundle(
                warehouse, universe, research, RiskConfig().stale_data_max_age_days, window
            )
            output.notes.append(bundle.description)
        # The strategy-only rows exist for train and validation. The test window
        # is a single look keyed by the as-traded specs, so it gets no second row.
        run = run_window(
            bundle, config, window, specs=specs, regime_labels=regime_labels,
            strategy_only=window != "test",
        )
        if symbols is not None:
            run.limitations.extend(
                n
                for n in coverage_notes(
                    bundle.history, config.research.splits, benchmark, universe=tuple(symbols),
                    summarise=universe is not None,
                )
                if n.split()[1].lower() == window
            )
        for gate in gates:
            counts = gate.count_states(bundle.history.trading_days, run.start, run.end)
            run.limitations.append(
                REGIME_GATE_NOTE.format(
                    name=gate.name, series=gate.series_id, rule=gate.rule,
                    threshold=gate.threshold, lag=gate.lag_sessions, window=window,
                    off=counts["risk_off"], on=counts["risk_on"], unknown=counts["unknown"],
                )
            )
        output.runs.append(run)
    # Recorded only once every window has loaded and run: a universe run loads
    # validation after train has run, and a refusal there must leave no rows.
    for run in output.runs:
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


def _ohlcv_bundle(
    warehouse: Warehouse,
    symbols: list[str],
    benchmark: str,
    source: str,
) -> tuple[HistoryBundle, list[tuple[str, str, dict]]]:
    from signalforge.pipeline import ohlcv_history

    wanted = sorted(set(symbols) | {benchmark})
    bundle = ohlcv_history(warehouse, wanted, sources=[source], price_basis="adjusted")
    if benchmark not in bundle.history.symbols:
        raise ValidationError(
            f"benchmark {benchmark} has no {source} bars in the warehouse; "
            "the benchmark must be ingested before an OHLCV run can be compared to it"
        )
    bundle = HistoryBundle(
        history=bundle.history,
        limitations=(*bundle.limitations, SURVIVORSHIP_NOTE, EXPOSURE_MISMATCH_NOTE),
        description=bundle.description,
    )
    traded = tuple(s for s in symbols if s != benchmark)
    return bundle, baseline_specs(
        bundle.history.symbols[0], universe=traded, benchmark=benchmark
    )


def _refuse_gaps_across_windows(
    warehouse: Warehouse, symbols: list[str], source: str, start, end
) -> None:
    """The concatenated-datasets gap check over the whole run, in SQL.

    Each window loads its own bars, so a gap that starts before the validation
    warmup and ends after train_end is invisible to both loads.
    """
    from signalforge.pipeline import GAP_REFUSE_DAYS

    row = warehouse.con.execute(
        "SELECT symbol, prev, trade_date FROM ("
        " SELECT symbol, trade_date,"
        "  lag(trade_date) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev"
        " FROM bars WHERE source = ? AND trade_date BETWEEN ? AND ?"
        f" AND symbol IN ({', '.join('?' for _ in symbols)}))"
        " WHERE date_diff('day', prev, trade_date) >= ? ORDER BY symbol, trade_date LIMIT 1",
        [source, start, end, *symbols, GAP_REFUSE_DAYS],
    ).fetchone()
    if row is not None:
        symbol, prev, current = row
        raise ValidationError(
            f"{symbol} has a {(current - prev).days}-day gap between {prev} and {current} "
            f"(source {source}). That is not a holiday: these are almost certainly two "
            "unrelated datasets concatenated. Fix or exclude the symbol."
        )


def _refuse_manual_splits_train_missed(
    warehouse: Warehouse, symbols: list[str], notes, train_end, load_end
) -> None:
    """Refuse a manual split the validation load applied but the train load could not see.

    A manual split is applied only when the bar on its date shows the jump. The
    train load ends at train_end, so for a split dated in the validation window
    it never sees that bar and leaves the earlier bars unscaled.
    """
    for action in warehouse.load_corporate_actions(symbols=symbols, source="manual"):
        if action.action_type != "split" or not train_end < action.effective_date <= load_end:
            continue
        label = f"{action.symbol} {action.effective_date} ({action.numerator}:{action.denominator})"
        if not any(n.startswith(f"MANUAL SPLIT SKIPPED: {label}:") for n in notes):
            raise ValidationError(
                f"manual split {label} applies in the validation window, but the train "
                "window loads bars only to train_end and cannot apply it, so its prices "
                "would be unscaled. Nothing was recorded."
            )


def _universe_bundle(
    warehouse: Warehouse,
    universe: UniverseConfig,
    research: ResearchConfig,
    max_stale_days: int,
    window: str,
) -> tuple[HistoryBundle, list[tuple[str, str, dict]], ResearchConfig]:
    """History, specs and research config for one window of a universe-file run.

    Each window loads only its own bars plus a warmup, so only one window's
    history is in memory at a time, and the test window is never read. The
    returned config carries the universe train_start in its splits, so the
    registry records the windows that were really run.
    """
    effective = universe.effective_splits(research.splits)
    research = research.model_copy(update={"splits": effective})
    wanted = sorted(set(universe.symbols) | {universe.benchmark})
    if window == "train":
        _refuse_gaps_across_windows(
            warehouse, wanted, universe.source, universe.load_start, effective.validation_end
        )
        load_start, load_end = universe.load_start, effective.train_end
    else:
        load_start = max(
            universe.load_start,
            effective.validation_start - timedelta(days=WINDOW_WARMUP_DAYS),
        )
        load_end = effective.validation_end
    loaded = ohlcv_history(
        warehouse, wanted, sources=[universe.source], price_basis="adjusted",
        start=load_start, end=load_end,
    )
    if window != "train":
        _refuse_manual_splits_train_missed(
            warehouse, wanted, loaded.limitations, effective.train_end, load_end
        )
    if universe.benchmark not in loaded.history.symbols:
        raise ValidationError(
            f"benchmark {universe.benchmark} has no {universe.source} bars in the warehouse; "
            "the benchmark must be ingested before an OHLCV run can be compared to it"
        )
    years = round((effective.train_end - effective.train_start).days / 365.25)
    notes = (
        SURVIVORSHIP_NOTE,
        EXPOSURE_MISMATCH_NOTE,
        BROAD_UNIVERSE_NOTE.format(
            name=universe.name, count=len(universe.symbols), load_start=load_start,
            load_end=load_end, train_start=effective.train_start,
            train_end=effective.train_end, years=years,
        ),
        ELIGIBILITY_NOTE.format(
            **universe.eligibility.model_dump(), max_stale_days=max_stale_days
        ),
    )
    bundle = HistoryBundle(
        history=loaded.history,
        limitations=(*loaded.limitations, *notes),
        description=f"{loaded.description}, universe {universe.name}",
    )
    specs = broad_specs(
        tuple(universe.symbols), universe.benchmark, universe.eligibility, universe.max_names,
        max_stale_days,
    )
    return bundle, specs, research


def _holdout_already_spent(
    registry: ExperimentRegistry, bundle, config: ConfigBundle, specs
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
    spent: list[str] = []
    for label, strategy_id, params in specs:
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
    width = max([34] + [len(o.label) + 2 for run in output.runs for o in run.outcomes])
    for run in output.runs:
        lines.append("")
        lines.append(
            f"=== {run.window.upper()}  {run.start} .. {run.end}  "
            f"dataset {run.dataset_fingerprint[:12]}"
        )
        lines.append(
            f"{'strategy':<{width}}{'totRet':>9}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}"
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
                f"{outcome.label:<{width}}{outcome.total_return:>8.1%}{cagr:>8}{sharpe:>8}"
                f"{m.max_drawdown:>7.1%}{m.trade_count:>7}{cap:>8}  {m.evidence.value}"
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    ohlcv = args.symbols is not None or args.universe or args.universe_file is not None
    if (ohlcv or args.overlays) and args.run_holdout:
        print(OHLCV_HOLDOUT_REFUSED if args.symbols is not None or args.universe else OVERLAYS_REFUSED_HOLDOUT)
        return 2
    if args.overlays and not ohlcv:
        print("refused: --overlays needs an OHLCV run (--symbols, --universe or --universe-file)")
        return 2
    if not args.db.exists():
        print(f"no warehouse at {args.db}; run the archive ingestion first")
        return 2
    config = load_all()
    universe = None
    if args.universe_file is not None:
        try:
            universe = load_universe(args.universe_file)
            splits = universe.effective_splits(config.research.splits)
        except ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        config = config.model_copy(
            update={"research": config.research.model_copy(update={"splits": splits})}
        )
        symbols = list(universe.symbols)
    else:
        symbols = list(config.research.universe) if args.universe else args.symbols
    if ohlcv and not symbols:
        print("refused: no symbols to trade (--symbols is empty or research.universe is empty)")
        return 2
    warehouse = Warehouse(str(args.db))
    try:
        try:
            output = run_research(
                warehouse,
                config.research,
                series_id=args.series,
                symbols=symbols,
                benchmark=args.benchmark,
                source=args.source,
                run_holdout=args.run_holdout,
                universe=universe,
                overlays=args.overlays,
            )
        except ValidationError as exc:
            if not ohlcv:
                raise
            print(f"refused: {exc}")
            return 2
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
