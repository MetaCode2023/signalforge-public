"""Mirror the local warehouse's market-data tables into the private D1 database.

    python -m signalforge.mirror_cli init
    python -m signalforge.mirror_cli sync [--db PATH] [--dry-run] [--max-files N]
                                          [--max-deletes N] [--rows-per-file N]
    python -m signalforge.mirror_cli load        (alias of sync)
    python -m signalforge.mirror_cli verify [--db PATH] [--sample N]

The warehouse is opened read-only. Exit 0 on success, 1 on a failed
postcondition, verify mismatch or wrangler failure, 2 on a refusal (guard,
missing or locked warehouse, version mismatch), 3 when --max-files stopped early. See docs/D1_MARKET_MIRROR.md.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import duckdb

from signalforge.data.storage import DEFAULT_PATH, Warehouse
from signalforge.mirror import codec, sync
from signalforge.mirror.wrangler import Wrangler, WranglerError

SHOWN = 20


def build_d1(runner, environ):
    return Wrangler(run=runner or subprocess.run, environ=environ)


def _open(db: str):
    path = Path(db)
    if not path.exists():
        print(f"warehouse not found: {path}", file=sys.stderr)
        return None
    try:
        return Warehouse(path, read_only=True)
    except duckdb.Error as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"could not open warehouse {path} read-only (is a writer running?): {first}", file=sys.stderr)
        return None


def _print_mismatches(mismatches: list[str]) -> None:
    print(f"{len(mismatches)} mismatch(es); first {min(SHOWN, len(mismatches))}:")
    for line in mismatches[:SHOWN]:
        print(f"  {line}")


def _sync(args, wh, d1, out_dir, now) -> int:
    result = sync.run_sync(
        wh, d1, dry_run=args.dry_run, max_files=args.max_files, max_deletes=args.max_deletes,
        rows_per_file=args.rows_per_file, out_dir=out_dir, now=now,
    )
    if result.mismatches:
        _print_mismatches(result.mismatches)
        return 1
    if not args.dry_run and not result.complete:
        print("incomplete: rerun sync to continue")
        return 3
    return 0


def _verify(args, wh, d1, out_dir, now) -> int:
    report = sync.run_verify(wh, d1, sample=args.sample)
    if report.mismatches:
        _print_mismatches(report.mismatches)
        return 1
    print("verify ok: zero mismatches")
    return 0


def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signalforge.mirror_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the D1 schema and version rows")
    for name in ("sync", "load"):
        p = sub.add_parser(name, help="two-level diff sync" if name == "sync" else "alias of sync")
        p.add_argument("--db", default=str(DEFAULT_PATH))
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--max-files", type=_positive, default=None)
        p.add_argument("--max-deletes", type=_non_negative, default=100)
        p.add_argument("--rows-per-file", type=_positive, default=codec.ROWS_PER_FILE)
    v = sub.add_parser("verify", help="compare the warehouse with D1")
    v.add_argument("--db", default=str(DEFAULT_PATH))
    v.add_argument("--sample", type=int, default=200)
    return parser


def main(argv=None, *, runner=None, d1=None, environ=None, out_dir=None, now=None) -> int:
    args = _parser().parse_args(argv)
    d1 = d1 or build_d1(runner, environ)
    out_dir = out_dir or sync.DEFAULT_OUT_DIR
    wh = None
    try:
        if args.command == "init":
            sync.run_init(d1, out_dir)
            return 0
        wh = _open(args.db)
        if wh is None:
            return 2
        handler = _verify if args.command == "verify" else _sync
        return handler(args, wh, d1, out_dir, now)
    except sync.MirrorError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except WranglerError as exc:
        print(f"wrangler failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if wh is not None:
            wh.close()


if __name__ == "__main__":
    sys.exit(main())
