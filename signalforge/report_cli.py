"""Send paper-research results to the reporting API.

    python -m signalforge.report_cli smoke
    python -m signalforge.report_cli push [--db PATH] [--dry-run]

`smoke` sends ONE heartbeat under an e2e-test run id (the server marks it as
test data from that prefix). `push` builds every event from the warehouse
(opened read-only), validates all of them locally, and only then sends. With
--dry-run it prints the events and needs neither credentials nor network.
Exit 0 only if every event was accepted (202) or already delivered.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from signalforge.data.storage import DEFAULT_PATH, Warehouse
from signalforge.reports import events as ev
from signalforge.reports import sender


def _session():
    import requests

    return requests.Session()


def _result(outcome) -> str:
    if outcome.duplicate:
        return "duplicate (already delivered)"
    if outcome.ok:
        return "accepted"
    return f"FAILED {outcome.error_code or ''}".strip()


def _print_outcomes(outcomes) -> None:
    print(f"{'event_type':<14} {'event_id':<36} {'status':>6}  result")
    for o in outcomes:
        print(f"{o.event_type:<14} {o.event_id:<36} {o.status_code:>6}  {_result(o)}")
    accepted = sum(1 for o in outcomes if o.ok and not o.duplicate)
    duplicate = sum(1 for o in outcomes if o.duplicate)
    failed = sum(1 for o in outcomes if not o.ok)
    print(f"total {len(outcomes)}: accepted {accepted}, duplicate {duplicate}, failed {failed}")


def _smoke(args, session, now, env_path) -> int:
    try:
        credentials = sender.load_credentials(env_path)
    except sender.MissingCredentials as exc:
        print(str(exc), file=sys.stderr)
        return 2
    event = ev.build_heartbeat(now, ev.smoke_run_id(now))
    outcome = sender.send(event, session or _session(), credentials)
    _print_outcomes([outcome])
    return 0 if outcome.ok else 1


def _push(args, session, now, env_path) -> int:
    credentials = None
    if not args.dry_run:
        try:
            credentials = sender.load_credentials(env_path)
        except sender.MissingCredentials as exc:
            print(str(exc), file=sys.stderr)
            return 2
    db = Path(args.db)
    if not db.exists():
        print(f"warehouse not found: {db}", file=sys.stderr)
        return 2
    wh = Warehouse(db, read_only=True)
    try:
        events, warnings = ev.build_all(wh, now)
    finally:
        wh.close()
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    problems = []
    for index, event in enumerate(events):
        try:
            ev.validate_event(event)
        except ev.EventInvalid as exc:
            kind = event.get("event_type") if isinstance(event, dict) else "?"
            problems.append(f"event {index} ({kind}): {exc}")
    if problems:
        print("invalid events, nothing sent:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2
    if args.dry_run:
        for event in events:
            print(ev.serialize(event).decode("utf-8"))
        print(f"dry run: {len(events)} events valid, nothing sent")
        return 0
    live = session or _session()
    outcomes = [sender.send(event, live, credentials) for event in events]
    _print_outcomes(outcomes)
    return 0 if all(o.ok for o in outcomes) else 1


def main(argv=None, *, session=None, now=None, env_path=None) -> int:
    parser = argparse.ArgumentParser(prog="signalforge.report_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke", help="send one test heartbeat")
    push = sub.add_parser("push", help="build, validate and send all events")
    push.add_argument("--db", default=str(DEFAULT_PATH))
    push.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    handler = _smoke if args.command == "smoke" else _push
    return handler(args, session, now, env_path)


if __name__ == "__main__":
    sys.exit(main())
