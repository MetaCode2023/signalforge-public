"""Drive paper trading from the command line.

    python -m signalforge.paper_cli --through 2026-10-23
    python -m signalforge.paper_cli --through 2026-10-23 \
        --override-strategy ma_trend --override-reason "operator dry run"
    python -m signalforge.paper_cli --approve ma_trend --approved-by austin \
        --reason "approved for paper trading"

With no approved strategy and no override, it refuses and says what would
unblock it. That is deliberate: a loop that picks its own strategy is the
same failure as a strategy promoting itself.

Everything it reports is SIMULATED. There is no live-order path.
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

from signalforge.config.models import load_all
from signalforge.core.errors import SignalForgeError
from signalforge.data.storage import Warehouse
from signalforge.paper.runner import PaperRunner, RunnerRefusal, record_active_strategy
from signalforge.pipeline import load_index_bundle

DEFAULT_DB = Path("data") / "warehouse" / "signalforge.duckdb"

# The only parameter set wired to the CLI, chosen a priori. Anything else
# should go through the registry with a named approver rather than a flag.
DEFAULT_PARAMS = {
    "ma_trend": {
        "fast_window": 50,
        "slow_window": 200,
        "target_weight": "0.05",
        "symbols": ["NASDAQCOM"],
    },
    "buy_and_hold": {"symbols": ["NASDAQCOM"], "target_weight": "0.05"},
}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m signalforge.paper_cli")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--series", default="NASDAQCOM")
    p.add_argument("--through", type=str, default=None, help="YYYY-MM-DD")
    p.add_argument("--override-strategy", default=None)
    p.add_argument("--override-reason", default="")
    p.add_argument("--approve", default=None, help="record an approved strategy and exit")
    p.add_argument("--approved-by", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--pause", default=None, help="pause before running, with a reason")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.db.exists():
        print(f"no warehouse at {args.db}; run the archive ingestion first")
        return 2

    warehouse = Warehouse(str(args.db))
    try:
        if args.approve:
            params = DEFAULT_PARAMS.get(args.approve)
            if params is None:
                print(
                    f"no default parameters for {args.approve!r}; known: "
                    f"{sorted(DEFAULT_PARAMS)}"
                )
                return 2
            record_active_strategy(
                warehouse,
                strategy_id=args.approve,
                parameters=params,
                approved_by=args.approved_by,
                reason=args.reason,
            )
            print(f"recorded {args.approve} as the active paper strategy")
            print(f"  approved by: {args.approved_by}")
            print(f"  reason: {args.reason}")
            return 0

        config = load_all()
        bundle = load_index_bundle(warehouse, args.series)
        through = (
            date.fromisoformat(args.through)
            if args.through
            else bundle.history.trading_days[-1]
        )

        override = None
        if args.override_strategy:
            params = DEFAULT_PARAMS.get(args.override_strategy)
            if params is None:
                print(f"no default parameters for {args.override_strategy!r}")
                return 2
            override = (args.override_strategy, params)

        runner = PaperRunner(
            warehouse=warehouse,
            research=config.research,
            risk=config.risk,
            override_strategy=override,
            override_reason=args.override_reason,
        )
        if args.pause:
            runner.pause(args.pause)

        try:
            result = runner.run(bundle.history, through=through)
        except RunnerRefusal as exc:
            print("REFUSED TO RUN")
            print()
            print(str(exc))
            return 3

        print(f"strategy        {result.strategy_id}")
        print(f"approval        {result.approval_source}")
        print(f"sessions run    {result.sessions}")
        if result.sessions:
            print(f"window          {result.first_session} .. {result.last_session}")
        for note in result.notes:
            print(f"NOTE: {note}")
        print()
        status = runner.broker.status()
        print("SIMULATED BOOK (no real capital, no real orders)")
        print(f"  equity          {status['equity']:,.2f}")
        print(f"  cash            {status['cash']:,.2f}")
        print(f"  positions       {status['position_count']}")
        print(f"  pending orders  {status['pending_count']}")
        print(f"  drawdown        {status['drawdown']:.2%}")
        print(f"  paused          {status['paused']}")
        print(f"  ledger intact   {status['ledger_intact']}")
        if runner.broker.statements:
            print()
            print(runner.broker.statements[-1].render())
        for note in bundle.limitations:
            print(f"LIMITATION: {note}")
    except SignalForgeError as exc:
        print(f"error: {exc}")
        return 1
    finally:
        warehouse.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
