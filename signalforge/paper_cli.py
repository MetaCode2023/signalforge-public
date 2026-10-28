"""Drive paper trading from the command line.

    python -m signalforge.paper_cli --through 2026-10-23
    python -m signalforge.paper_cli --through 2026-10-23 \
        --override-strategy ma_trend --override-reason "operator dry run"
    python -m signalforge.paper_cli --approve ma_trend --approved-by austin \
        --reason "approved for paper trading"
    python -m signalforge.paper_cli --symbols AAPL,MSFT --source tiingo
    python -m signalforge.paper_cli --universe --approve buy_and_hold --approved-by austin --reason "approved for the universe book"

With no approved strategy and no override, it refuses and says what would
unblock it. That is deliberate: a loop that picks its own strategy is the
same failure as a strategy promoting itself.

--symbols/--universe run a book of real OHLCV on RAW prices with corporate
actions booked. That book has its own approval: the NASDAQCOM one never
authorises it. Before it trades, the benchmark's bar dates are used as the
calendar and the run is refused (exit 3) if any symbol is stale or has holes.

Everything it reports is SIMULATED. There is no live-order path.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from signalforge.config.models import load_all
from signalforge.core.errors import SignalForgeError
from signalforge.data.storage import Warehouse
from signalforge.paper.calendar import check_freshness
from signalforge.paper.runner import (
    DEFAULT_BOOK_ID,
    PaperRunner,
    RunnerRefusal,
    record_active_strategy,
)
from signalforge.pipeline import SURVIVORSHIP_NOTE, HistoryBundle, load_index_bundle, ohlcv_history

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
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--series", default="NASDAQCOM")
    mode.add_argument(
        "--symbols",
        type=lambda v: [s.strip().upper() for s in v.split(",") if s.strip()],
        default=None,
        help="comma-separated symbols to paper-trade from the warehouse OHLCV bars",
    )
    mode.add_argument(
        "--universe",
        action="store_true",
        help="paper-trade the research.yaml universe from the warehouse OHLCV bars",
    )
    p.add_argument("--source", default="tiingo")
    p.add_argument("--benchmark", default="SPY", help="its bar dates are the trading calendar")
    p.add_argument("--through", type=str, default=None, help="YYYY-MM-DD")
    p.add_argument("--override-strategy", default=None)
    p.add_argument("--override-reason", default="")
    p.add_argument("--approve", default=None, help="record an approved strategy and exit")
    p.add_argument("--approved-by", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--pause", default=None, help="pause before running, with a reason")
    return p.parse_args(argv)


def _book_params(params: dict, symbols: list[str] | None) -> dict:
    return params if symbols is None else {**params, "symbols": list(symbols)}


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.db.exists():
        print(f"no warehouse at {args.db}; run the archive ingestion first")
        return 2

    ohlcv = args.symbols is not None or args.universe
    symbols = None
    book_id = DEFAULT_BOOK_ID
    if ohlcv:
        symbols = sorted(set(load_all().research.universe if args.universe else args.symbols))
        if not symbols:
            print("refused: no symbols to trade (--symbols is empty or research.universe is empty)")
            return 2
        book_id = f"ohlcv:{'universe' if args.universe else ','.join(symbols)}"

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
                parameters=_book_params(params, symbols),
                approved_by=args.approved_by,
                reason=args.reason,
                book_id=book_id,
            )
            print(f"recorded {args.approve} as the active paper strategy")
            print(f"  approved by: {args.approved_by}")
            print(f"  reason: {args.reason}")
            return 0

        config = load_all()
        if ohlcv:
            raw = ohlcv_history(warehouse, symbols, sources=[args.source], price_basis="raw")
            bundle = HistoryBundle(
                history=raw.history,
                limitations=(*raw.limitations, SURVIVORSHIP_NOTE),
                description=raw.description,
            )
        else:
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
            override = (args.override_strategy, _book_params(params, symbols))

        runner = PaperRunner(
            warehouse=warehouse,
            research=config.research,
            risk=config.risk,
            override_strategy=override,
            override_reason=args.override_reason,
            **(
                {"book_id": book_id, "raw_basis": True, "corporate_action_source": args.source}
                if ohlcv
                else {}
            ),
        )
        if args.pause:
            runner.pause(args.pause)

        if ohlcv:
            approved = warehouse.con.execute(
                "SELECT parameters FROM active_strategy "
                "WHERE coalesce(book_id, ?) = ? ORDER BY seq DESC LIMIT 1",
                [DEFAULT_BOOK_ID, book_id],
            ).fetchall()
            approved_symbols = json.loads(approved[0][0]).get("symbols") if approved else None
            if approved_symbols is not None and sorted(approved_symbols) != symbols:
                added = sorted(set(symbols) - set(approved_symbols))
                removed = sorted(set(approved_symbols) - set(symbols))
                print("REFUSED TO RUN")
                print()
                print(
                    f"the symbols approved for {book_id} differ from the book's current "
                    f"symbols (added: {added or 'none'}, removed: {removed or 'none'}). "
                    "re-approve it with --approve for the current set before trading"
                )
                return 2
            calendar = [
                b.trade_date
                for b in warehouse.load_bars(symbols=[args.benchmark], source=args.source)
            ]
            done = runner.last_completed_session(bundle.history)
            problems, warnings = check_freshness(
                calendar,
                {s: [b.trade_date for b in bundle.history.bars_for(s)] for s in symbols},
                through,
                None if done is None else done + timedelta(days=1),
            )
            if problems:
                print("REFUSED TO RUN")
                print()
                print(
                    f"freshness gate against the {args.benchmark} calendar "
                    f"({args.source} bars):"
                )
                for problem in problems:
                    print(f"  {problem}")
                return 3
            if warnings:
                print(
                    "WARNING: older gaps inside the trading window (reported, not refused; "
                    "only the most recent sessions are gated):"
                )
                for warning in warnings:
                    print(f"  {warning}")
                print()

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
        if ohlcv:
            booked = dict(
                warehouse.con.execute(
                    "SELECT kind, count(*) FROM paper_corporate_actions WHERE book_id = ? "
                    "GROUP BY kind",
                    [book_id],
                ).fetchall()
            )
            print(
                "corporate actions booked  "
                + ", ".join(f"{k} {booked.get(k, 0)}" for k in ("split", "adjustment", "dividend"))
                + "  (this book, to date)"
            )
            print(
                "NOTE: adjustment actions are spinoff/special factors, booked as "
                "value-preserving parent shares."
            )
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
