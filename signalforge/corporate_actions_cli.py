"""Triage raw price jumps that no corporate action explains.

    python -m signalforge.corporate_actions_cli draft --db PATH --source tiingo \
        [--symbols A,B | --universe] [--out FILE]
    python -m signalforge.corporate_actions_cli apply --db PATH --file FILE

`draft` only reads the warehouse. It lists every close-to-close move beyond the
runner's jump threshold that has no recorded split/adjustment on that date (any
source) and no acknowledgement, classifies it, and proposes what to record.
Nothing is recorded until a human sets `approved: true` on an entry and runs
`apply`, which touches only approved entries.

Categories (checked in this order):
  missing_split  the vendor's adjusted close is smooth under a raw jump; the
                 proposed factor is raw/adjusted, a clean split when it lands
                 within 1% of one, otherwise an exact manual adjustment
  real_move      raw and adjusted moved together: the vendor says it is real
  review         a near-integer split the vendor did not adjust for, a real
                 move too close to the end of the data to judge, or anything
                 that fits neither; a proposal here is a hint, never a verdict
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import duckdb

from signalforge.config.models import load_all
from signalforge.core.types import CorporateAction, ratio_to_fraction
from signalforge.data.storage import Warehouse
from signalforge.paper.runner import UNRECORDED_JUMP_RATIO, PaperRunner

DEFAULT_DB = Path("data") / "warehouse" / "signalforge.duckdb"
DEFAULT_OUT = Path("corporate_action_drafts.json")

SMOOTH_ADJUSTED = Decimal("0.05")
VENDOR_AGREES = Decimal("0.01")
CLEAN_SNAP_TOLERANCE = Decimal("0.01")
INTEGER_SNAP_TOLERANCE = Decimal("0.015")
HOLD_BARS = 20
HOLD_BAND = Decimal("0.25")
RATIO_QUANT = Decimal("0.000001")
CATEGORIES = ("missing_split", "real_move", "review")

CLEAN_SPLIT_FRACTIONS = tuple(
    sorted(
        {Fraction(n, 1) for n in range(2, 21)}
        | {Fraction(1, n) for n in range(2, 21)}
        | {Fraction(3, 2), Fraction(2, 3), Fraction(5, 4), Fraction(4, 5),
           Fraction(5, 2), Fraction(2, 5)}
    )
)


INTEGER_SPLITS = tuple(
    sorted({Fraction(n, 1) for n in (2, 3, 4, 5, 10)} | {Fraction(1, n) for n in (2, 3, 4, 5, 10)})
)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m signalforge.corporate_actions_cli")
    sub = p.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft", help="classify unrecorded raw jumps (read-only)")
    draft.add_argument("--db", type=Path, default=DEFAULT_DB)
    draft.add_argument("--source", default="tiingo")
    scope = draft.add_mutually_exclusive_group()
    scope.add_argument(
        "--symbols",
        type=lambda v: [s.strip().upper() for s in v.split(",") if s.strip()],
        default=None,
    )
    scope.add_argument("--universe", action="store_true")
    draft.add_argument("--out", type=Path, default=DEFAULT_OUT)
    draft.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    apply = sub.add_parser("apply", help="record the approved entries of a draft file")
    apply.add_argument("--db", type=Path, default=DEFAULT_DB)
    apply.add_argument("--file", type=Path, required=True)
    return p.parse_args(argv)


def _snap(value: Decimal, candidates, tolerance: Decimal) -> Decimal | None:
    """The nearest candidate within `tolerance` of the observed value, as a
    6dp ratio that ratio_to_fraction still calls a clean "split", or None."""
    exact = Fraction(value)
    nearest = min(candidates, key=lambda f: abs(f / exact - 1))
    if abs(nearest / exact - 1) > Fraction(tolerance):
        return None
    snapped = (Decimal(nearest.numerator) / Decimal(nearest.denominator)).quantize(RATIO_QUANT)
    return snapped if ratio_to_fraction(snapped)[2] == "split" else None


def _holds(bars, index: int) -> bool:
    """The next HOLD_BARS closes all stay within HOLD_BAND of the jump-day close.

    Fewer than HOLD_BARS following bars cannot show it held, so it did not.
    """
    following = bars[index + 1:index + 1 + HOLD_BARS]
    if len(following) < HOLD_BARS:
        return False
    level = bars[index].close
    return all(abs(b.close / level - 1) <= HOLD_BAND for b in following)


def _classify(bars, index: int, raw_ratio: Decimal, adj_ratio: Decimal | None):
    """(category, proposed, decision) for one unexcused jump."""
    if adj_ratio is None:
        return "review", None, "review"
    if abs(adj_ratio - 1) <= SMOOTH_ADJUSTED:
        factor = raw_ratio / adj_ratio
        clean = _snap(factor, CLEAN_SPLIT_FRACTIONS, CLEAN_SNAP_TOLERANCE)
        if clean is not None:
            return "missing_split", {"action": "manual_split", "ratio": str(clean)}, "propose"
        exact = factor.quantize(RATIO_QUANT)
        return "missing_split", {"action": "manual_adjustment", "ratio": str(exact)}, "propose"
    if abs(adj_ratio / raw_ratio - 1) <= VENDOR_AGREES:
        integer = _snap(raw_ratio, INTEGER_SPLITS, INTEGER_SNAP_TOLERANCE)
        if integer is not None and _holds(bars, index):
            return "review", {"action": "manual_split", "ratio": str(integer)}, "review"
        acknowledge = {
            "action": "acknowledge",
            "reason": f"vendor-adjusted price moved too (adj {adj_ratio:.4f}); real move",
        }
        if len(bars) - index - 1 < HOLD_BARS:
            return "review", acknowledge, "review"
        return "real_move", acknowledge, "propose"
    return "review", None, "review"


def draft_entries(warehouse, source: str, symbols=None) -> list[dict]:
    if symbols is None:
        symbols = [
            r[0]
            for r in warehouse.con.execute(
                "SELECT DISTINCT symbol FROM bars WHERE source = ? ORDER BY symbol", [source]
            ).fetchall()
        ]
    symbols = sorted(set(symbols))
    excused = {
        (a.symbol, a.effective_date)
        for a in PaperRunner._split_like(
            warehouse.load_corporate_actions(symbols=symbols, source=(source, "manual"))
        )
    } | warehouse.price_move_acknowledgements()
    entries: list[dict] = []
    for symbol in symbols:
        bars = warehouse.load_bars(symbols=[symbol], source=source)
        for i in range(1, len(bars)):
            previous, current = bars[i - 1], bars[i]
            if previous.close <= 0 or current.close <= 0:
                continue
            raw_ratio = previous.close / current.close
            if max(raw_ratio, 1 / raw_ratio) <= UNRECORDED_JUMP_RATIO:
                continue
            if (symbol, current.trade_date) in excused:
                continue
            adj_ratio = None
            if (
                previous.adj_close is not None
                and current.adj_close is not None
                and previous.adj_close > 0
                and current.adj_close > 0
            ):
                adj_ratio = previous.adj_close / current.adj_close
            category, proposed, decision = _classify(bars, i, raw_ratio, adj_ratio)
            entries.append({
                "symbol": symbol,
                "date": current.trade_date.isoformat(),
                "raw_ratio": str(raw_ratio.quantize(RATIO_QUANT)),
                "adj_ratio": None if adj_ratio is None else str(adj_ratio.quantize(RATIO_QUANT)),
                "prior_close": str(previous.close),
                "close": str(current.close),
                "category": category,
                "decision": decision,
                "proposed": proposed,
                "approved": False,
            })
    return entries


SPLIT_ACTIONS = ("manual_split", "manual_adjustment")
NOTHING_RECORDED = "nothing was recorded"


def _validate(entry: dict):
    """(symbol, day, proposed) for one approved entry, or ValueError."""
    symbol, raw_day, proposed = entry.get("symbol"), entry.get("date"), entry.get("proposed")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"{symbol!r} {raw_day}: symbol must be a non-empty string")
    try:
        day = date.fromisoformat(raw_day)
    except (TypeError, ValueError):
        raise ValueError(f"{symbol} {raw_day!r}: date must be YYYY-MM-DD") from None
    where = f"{symbol} {day}"
    if not isinstance(proposed, dict):
        raise ValueError(f"{where}: approved with no proposal")
    action = proposed.get("action")
    if action in SPLIT_ACTIONS:
        ratio = proposed.get("ratio")
        if not isinstance(ratio, str):
            raise ValueError(
                f"{where}: ratio must be a string in the file, got {type(ratio).__name__}"
            )
        try:
            CorporateAction.split(symbol, day, Decimal(ratio), "manual")
        except Exception as exc:
            raise ValueError(f"{where}: invalid ratio {ratio!r} ({exc})") from None
    elif action == "acknowledge":
        reason = proposed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{where}: acknowledging a price move requires a reason")
    else:
        raise ValueError(f"{where}: unknown action {action!r}")
    return symbol, day, proposed


def apply_entries(warehouse, entries: list[dict]) -> list[str]:
    """Record the approved entries, all or none; returns one line per entry.

    Every approved entry is validated before anything is written, and the
    writes run in one transaction that rolls back on any failure.
    """
    approved = [e for e in entries if e.get("approved") is True]
    problems: list[str] = []
    checked = []
    for e in approved:
        try:
            checked.append(_validate(e))
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise ValueError("; ".join(problems) + f"; {NOTHING_RECORDED}")
    acknowledged = warehouse.price_move_acknowledgements()
    manual = {
        (a.symbol, a.effective_date)
        for a in warehouse.load_corporate_actions(source="manual")
    }
    lines: list[str] = []

    def write() -> None:
        for symbol, day, proposed in checked:
            if proposed["action"] in SPLIT_ACTIONS:
                seen = (symbol, day) in manual
                warehouse.record_manual_split(symbol, day, Decimal(proposed["ratio"]))
                what = f"{proposed['action'].replace('_', ' ')} {proposed['ratio']}"
            else:
                seen = (symbol, day) in acknowledged
                warehouse.acknowledge_price_move(symbol, day, proposed["reason"])
                what = f"acknowledgement ({proposed['reason']})"
            lines.append(f"{symbol} {day}: {'already recorded, ' if seen else 'recorded '}{what}")

    try:
        warehouse.transaction(write)
    except Exception as exc:
        raise ValueError(f"{exc}; {NOTHING_RECORDED}") from exc
    return lines


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.db.exists():
        print(f"no warehouse at {args.db}")
        return 2
    if args.command == "draft":
        if args.out.exists() and not args.force:
            print(f"{args.out} already exists; it may hold approvals. Pass --force to overwrite it")
            return 2
        symbols = args.symbols
        if args.universe:
            symbols = list(load_all().research.universe)
        warehouse = Warehouse(str(args.db), read_only=True)
        try:
            entries = draft_entries(warehouse, args.source, symbols)
        except duckdb.CatalogException as exc:
            print(
                f"the warehouse at {args.db} is missing a table the draft reads ({exc}); "
                "it predates Phase 2. Open it once read-write (run the collector) to create "
                "the tables, then draft again"
            )
            return 2
        finally:
            warehouse.close()
        args.out.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        print(f"{'category':<16}{'count':>6}")
        for category in CATEGORIES:
            count = sum(1 for e in entries if e["category"] == category)
            print(f"{category:<16}{count:>6}")
        print(f"{'total':<16}{len(entries):>6}")
        print(f"wrote {args.out}; set approved to true on entries you accept, then run apply")
        return 0

    try:
        entries = json.loads(args.file.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, ValueError) as exc:
        print(f"cannot read {args.file}: {exc}")
        return 2
    warehouse = Warehouse(str(args.db))
    try:
        try:
            lines = apply_entries(warehouse, entries)
        except ValueError as exc:
            print(f"refused: {exc}")
            return 2
    finally:
        warehouse.close()
    for line in lines:
        print(line)
    print(f"{len(lines)} approved entries processed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
