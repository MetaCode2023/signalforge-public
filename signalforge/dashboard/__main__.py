"""python -m signalforge.dashboard --db <warehouse> --out <html> [--serve]"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from signalforge.config.models import load_all
from signalforge.dashboard.render import open_readonly, write_dashboard
from signalforge.dashboard.serve import serve_forever

DEFAULT_DB = Path("data") / "warehouse" / "signalforge.duckdb"
DEFAULT_OUT = Path("dashboard") / "build" / "index.html"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m signalforge.dashboard")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    warehouse, note = None, " ".join(("no warehouse file found at", str(args.db)))
    if args.db.exists():
        try:
            warehouse, note = open_readonly(args.db), None
        except duckdb.Error as exc:
            note = " ".join(("no warehouse could be opened at", str(args.db), "because", str(exc)))
    out = write_dashboard(args.out, warehouse=warehouse, config=load_all(), warehouse_note=note)
    print("wrote", out, out.stat().st_size, "bytes")
    if args.serve:
        print("serving", out.parent, "on", args.host, "port", args.port)
        serve_forever(out.parent, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
