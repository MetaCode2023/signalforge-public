"""Assay: test whether copying company insiders' disclosed purchases beats the market.

    python -m signalforge.insider_cli download              # SEC quarterly zips, 2006q1..2014q4
    python -m signalforge.insider_cli build                 # parse them into data/insiders/
    python -m signalforge.insider_cli trial --window validation --source tiingo

`download` and `build` touch only public SEC data. `trial` reads the price
warehouse read-only, runs on the train or validation window only (the
held-out test window is spent once, through research_cli --run-holdout),
writes reports/insider_trials/ and the dashboard's Assay tab.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

from signalforge.config.models import load_all, load_data_sources
from signalforge.core.errors import ConfigError, SignalForgeError, ValidationError
from signalforge.data import sec_insiders as sec
from signalforge.data.storage import Warehouse
from signalforge.insiders.events import load_purchases, resolve_symbols, select
from signalforge.insiders.report import record, render_markdown
from signalforge.insiders.study import Params, load_panel, run
from signalforge.learning.registry import current_git_commit

DEFAULT_DB = Path("data") / "warehouse" / "signalforge.duckdb"
DEFAULT_OUT = Path("reports") / "insider_trials"
DEFAULT_PAGE = Path("dashboard") / "build" / "assay.html"


def cmd_download(args) -> int:
    import requests

    quarters = sec.quarter_range(args.start, args.end)
    agent = args.user_agent or load_data_sources().user_agent
    print(f"downloading {len(quarters)} quarterly data sets to {args.raw_dir} (polite pacing; a few minutes)")
    outcomes = sec.download(Path(args.raw_dir), quarters, requests.Session(), agent)
    for o in outcomes:
        size = f"{o.bytes / 1e6:6.1f} MB" if o.bytes else "        "
        print(f"  {o.quarter}  {o.status:<10} {size}  {o.detail or ''}")
    bad = [o for o in outcomes if o.status in ("failed", "missing")]
    print(f"{len(outcomes) - len(bad)} of {len(outcomes)} quarters on disk")
    return 1 if bad else 0


def cmd_build(args) -> int:
    quarters = sec.quarter_range(args.start, args.end) if args.start else None
    t0 = time.monotonic()
    manifest = sec.build(Path(args.raw_dir), Path(args.out_dir), quarters)
    print(f"parsed {len(manifest['quarters'])} quarters in {time.monotonic() - t0:.0f}s: "
          f"{manifest['transaction_rows']:,} transaction rows -> {manifest['purchases']:,} purchases "
          f"({manifest['first_filing']} to {manifest['last_filing']})")
    for reason, n in list(manifest["rejected"].items())[:8]:
        print(f"  dropped {n:>9,}  {reason}")
    return 0


def trial_window(args, splits):
    if args.window == "validation":
        start, end = splits.validation_start, splits.validation_end
    elif args.window == "train":
        start, end = splits.train_start, splits.train_end
    else:
        raise ConfigError(
            "insider trials run on 'train' or 'validation' only. The test window is spent once, "
            "through research_cli --run-holdout."
        )
    if end >= splits.test_start:
        raise ConfigError("the trial window reaches the held-out test window")
    return start, end


def cmd_trial(args) -> int:
    bundle = load_all(Path(args.config_dir) if args.config_dir else None)
    start, end = trial_window(args, bundle.research.splits)
    db = Path(args.db)
    if not db.is_file():
        raise ConfigError(f"no warehouse at {db}. Build it first (README, Quick start) or pass --db.")
    purchases_path = Path(args.purchases)
    raw = load_purchases(purchases_path)
    manifest_path = purchases_path.parent / sec.MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    known_until = [x for x in raw if x.filing_date <= end]
    if not known_until or min(x.filing_date for x in known_until) > end - timedelta(days=365):
        raise ValidationError(
            f"the purchases file has too little history before {end}; the SEC data starts in 2006, "
            "so only the validation window can be tested"
        )
    wh = Warehouse(db, read_only=True)
    try:
        sql = "SELECT DISTINCT symbol FROM bars" + (" WHERE source = ?" if args.source else "")
        known = [r[0] for r in wh.con.execute(sql, [args.source] if args.source else []).fetchall()]
        matched, dropped_map = resolve_symbols(known_until, known)
        chosen, dropped_sel = select(matched, min_value=args.min_value, include_ten_pct=args.include_ten_pct)
        if args.benchmark not in known:
            raise ValidationError(f"benchmark {args.benchmark} is not in the warehouse")
        first = min(x.filing_date for x in chosen) - timedelta(days=10)
        panel = load_panel(wh, {x.symbol for x in chosen} | {args.benchmark}, first, end, args.source)
        try:
            acknowledged = frozenset(wh.price_move_acknowledgements())
        except Exception:
            acknowledged = frozenset()
    finally:
        wh.close()
    horizons = tuple(int(h) for h in args.horizons.split(","))
    params = Params(
        window_start=start, window_end=end, horizons=horizons, primary=args.primary,
        cost_bps=args.cost_bps, min_history=args.min_history, min_hit=args.min_hit,
        draws=args.draws, seed=args.seed, benchmark=args.benchmark,
    )
    t0 = time.monotonic()
    result = run(chosen, panel, params, acknowledged)
    result.funnel = [
        ("Form 4 transaction rows read", manifest.get("transaction_rows")),
        ("Open-market purchases of common stock", manifest.get("purchases") or len(raw)),
        (f"Filed by {end}", len(known_until)),
        ("Company in the price warehouse", len(matched)),
        (f"Officers and directors, ${args.min_value:,.0f}+", len(chosen)),
        ("Placed on the calendar, checks passed", result.scored),
        ("Copied trades in the window", result.sizes["all"]),
    ]
    result.funnel = [(s, n) for s, n in result.funnel if n is not None]
    rec = record(result, provenance={
        "git_commit": current_git_commit(),
        "purchases_file": str(purchases_path),
        "sec_source": manifest.get("source_url", "https://www.sec.gov/dera/data/form-345"),
        "dropped_mapping": dict(dropped_map), "dropped_selection": dict(dropped_sel),
        "prices": f"{len(panel.symbols)} warehouse symbols, adjusted closes"
                  + (f" from {args.source}" if args.source else ""),
    })
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"assay-{start}-{end}"
    (out / f"{stem}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    table = render_markdown(rec)
    (out / f"{stem}.md").write_text(table, encoding="utf-8")
    print(f"study ran in {time.monotonic() - t0:.1f}s")
    print(table)
    if not args.no_dashboard:
        from signalforge.dashboard.assay_page import write_assay_page

        print("wrote", write_assay_page(args.dashboard_out, rec))
        from signalforge.dashboard.scoreboard_page import refresh_scoreboard

        board = refresh_scoreboard(Path(args.dashboard_out).parent / "scoreboard.html", args.db, out.parent)
        if board is not None:
            print("wrote", board)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signalforge.insider_cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download", help="fetch SEC insider data sets (public data)")
    d.add_argument("--start", default="2006q1")
    d.add_argument("--end", default="2014q4")
    d.add_argument("--raw-dir", default=str(sec.DEFAULT_RAW_DIR))
    d.add_argument("--user-agent", default=None, help="default: user_agent in config/data_sources.yaml")
    b = sub.add_parser("build", help="parse downloaded zips into data/insiders/")
    b.add_argument("--raw-dir", default=str(sec.DEFAULT_RAW_DIR))
    b.add_argument("--out-dir", default=str(sec.DEFAULT_OUT_DIR))
    b.add_argument("--start", default=None)
    b.add_argument("--end", default=None)
    t = sub.add_parser("trial", help="run the Assay study on the validation window")
    t.add_argument("--window", default="validation")
    t.add_argument("--db", default=str(DEFAULT_DB))
    t.add_argument("--config-dir", default=None)
    t.add_argument("--source", default=None, help="restrict bars to one source, e.g. tiingo")
    t.add_argument("--purchases", default=str(sec.DEFAULT_OUT_DIR / sec.PURCHASES_FILE))
    t.add_argument("--benchmark", default="SPY")
    t.add_argument("--horizons", default="5,21,63")
    t.add_argument("--primary", type=int, default=21)
    t.add_argument("--min-value", type=float, default=25_000.0)
    t.add_argument("--min-history", type=int, default=3)
    t.add_argument("--min-hit", type=float, default=0.6)
    t.add_argument("--include-ten-pct", action="store_true")
    t.add_argument("--cost-bps", type=float, default=5.0)
    t.add_argument("--draws", type=int, default=500)
    t.add_argument("--seed", type=int, default=20261101)
    t.add_argument("--out", default=str(DEFAULT_OUT))
    t.add_argument("--dashboard-out", default=str(DEFAULT_PAGE))
    t.add_argument("--no-dashboard", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "build" and bool(args.start) != bool(args.end):
        parser.error("--start and --end go together")
    try:
        return {"download": cmd_download, "build": cmd_build, "trial": cmd_trial}[args.cmd](args)
    except SignalForgeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
