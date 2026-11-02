"""Research a multi-asset allocation book.

    python -m signalforge.allocation_cli [--allocation-file PATH] [--db PATH]

Runs the equal-weight basket and the time-series momentum rule over the train
and validation windows, both as traded and strategy only, and records every
run in the experiment registry. The allocation's single-name cap applies to
this research run only; `risk.yaml`, the defaults and the paper book are
untouched.

The held-out test window is refused here, as it is for universe-file runs:
spending it is a separate, deliberate decision.
"""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

from signalforge.config.models import AllocationConfig, ConfigBundle, load_all, load_allocation
from signalforge.core.errors import ConfigError, ValidationError
from signalforge.data.storage import Warehouse
from signalforge.learning.registry import ExperimentRegistry
from signalforge.pipeline import (
    ALLOCATION_NOTE,
    HistoryBundle,
    allocation_specs,
    load_regime_labels,
    ohlcv_history,
    run_window,
)
from signalforge.research_cli import (
    DEFAULT_DB,
    HOLDOUT_NOT_CONSUMED,
    ResearchOutput,
    _record,
    _refuse_gaps_across_windows,
    _refuse_manual_splits_train_missed,
    _safe_data_sources,
    format_table,
)

DEFAULT_ALLOCATION = Path("config") / "allocations" / "assets10.yaml"
DEFAULT_STATE_OUT = Path("dashboard") / "build" / "allocation.html"

# Calendar days loaded before the validation window: enough for a 252-session
# lookback plus the month before the first decision, with room for holidays.
WARMUP_CALENDAR_DAYS_PER_SESSION = 1.5
WARMUP_EXTRA_DAYS = 60

ALLOCATION_HOLDOUT_REFUSED = (
    "refused: the held-out test window is not run from allocation_cli. Spending the one "
    "honest look is a separate decision, recorded in docs/DECISIONS.md before it is made."
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m signalforge.allocation_cli")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--allocation-file", type=Path, default=DEFAULT_ALLOCATION)
    parser.add_argument(
        "--state",
        action="store_true",
        help="show where the rule stands today and write the Allocation tab; runs no research",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_STATE_OUT)
    parser.add_argument("--run-holdout", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def format_state(state) -> str:
    from signalforge.allocation_state import STATE_TEXT

    def pct(v):
        return "n/a" if v is None else f"{float(v) * 100:+.1f}%"

    lines = [
        f"allocation {state.name}: data through {state.as_of}, decided on the close of {state.reference}",
        f"invested {float(state.invested) * 100:.0f}%, next decision on the first session of "
        f"{state.next_decision_month:%B %Y}",
        f"{'fund':<6}{'this month':<20}{'12m @ decision':>16}{'12m today':>11}{'below 1y high':>15}",
    ]
    for a in state.assets:
        flag = "  <- leaning the other way" if a.flip_watch else ""
        stale = "  STALE" if a.stale else ""
        lines.append(
            f"{a.symbol:<6}{STATE_TEXT[a.state]:<20}{pct(a.decided_return):>16}{pct(a.current_return):>11}"
            f"{pct(a.below_high):>15}{flag}{stale}"
        )
    return "\n".join(lines)


def run_state(warehouse: Warehouse, allocation: AllocationConfig, out: Path) -> int:
    from signalforge.allocation_state import market_state
    from signalforge.dashboard.allocation_page import write_allocation_page

    state = market_state(warehouse, allocation)
    print(format_state(state))
    written = write_allocation_page(out, state)
    print(f"wrote {written}")
    return 0


def allocation_bundle(
    warehouse: Warehouse, allocation: AllocationConfig, config: ConfigBundle, window: str
) -> HistoryBundle:
    """One window's bars plus warmup. The test window is never read."""
    splits = config.research.splits
    symbols = list(allocation.symbols)
    if window == "train":
        _refuse_gaps_across_windows(
            warehouse, symbols, allocation.source, allocation.load_start, splits.validation_end
        )
        start, end = allocation.load_start, splits.train_end
    elif window == "validation":
        warmup = int(allocation.lookback * WARMUP_CALENDAR_DAYS_PER_SESSION) + WARMUP_EXTRA_DAYS
        start = max(allocation.load_start, splits.validation_start - timedelta(days=warmup))
        end = splits.validation_end
    else:
        raise ValidationError(f"allocation runs cover train and validation only, not {window!r}")
    loaded = ohlcv_history(
        warehouse, symbols, sources=[allocation.source], price_basis="adjusted", start=start, end=end
    )
    if window == "validation":
        _refuse_manual_splits_train_missed(
            warehouse, symbols, loaded.limitations, splits.train_end, end
        )
    note = ALLOCATION_NOTE.format(
        name=allocation.name, count=len(symbols), weight=allocation.target_weight,
        cap=allocation.max_single_name_exposure, lookback=allocation.lookback,
    )
    return HistoryBundle(
        history=loaded.history,
        limitations=(*loaded.limitations, note),
        description=f"{loaded.description}, universe {allocation.name}",
    )


def run_allocation(
    warehouse: Warehouse,
    allocation: AllocationConfig,
    config: ConfigBundle,
    registry: ExperimentRegistry | None = None,
) -> ResearchOutput:
    research = config.research.model_copy(
        update={"splits": allocation.effective_splits(config.research.splits)}
    )
    config = ConfigBundle(
        research=research,
        risk=allocation.risk(config.risk),
        data_sources=config.data_sources,
    )
    specs = allocation_specs(allocation.symbols, allocation.lookback, allocation.target_weight)
    registry = registry or ExperimentRegistry(warehouse)
    output = ResearchOutput()
    regime_labels, regime_note = load_regime_labels(warehouse)
    output.notes.append(regime_note)
    for window in ("train", "validation"):
        bundle = allocation_bundle(warehouse, allocation, config, window)
        output.notes.append(bundle.description)
        output.runs.append(
            run_window(bundle, config, window, specs=specs, regime_labels=regime_labels,
                       strategy_only=True)
        )
    # Recorded only after both windows ran, so a refusal leaves no rows.
    for run in output.runs:
        output.experiment_ids.extend(_record(registry, run, config))
    output.notes.append(HOLDOUT_NOT_CONSUMED)
    return output


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.run_holdout:
        print(ALLOCATION_HOLDOUT_REFUSED)
        return 2
    if not args.db.exists():
        print(f"no warehouse at {args.db}; run the archive ingestion first")
        return 2
    config = load_all()
    try:
        allocation = load_allocation(args.allocation_file)
        if not args.state:  # the state runs no window, so the train window does not bind it
            allocation.effective_splits(config.research.splits)
    except ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    config = config.model_copy(update={"data_sources": _safe_data_sources()})
    warehouse = Warehouse(str(args.db))
    try:
        if args.state:
            try:
                return run_state(warehouse, allocation, args.out)
            except (ValidationError, ValueError) as exc:
                print(f"refused: {exc}")
                return 2
        try:
            output = run_allocation(warehouse, allocation, config)
        except ValidationError as exc:
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
        print(f"experiments recorded: {len(output.experiment_ids)}")
    finally:
        warehouse.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
