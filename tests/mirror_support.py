"""Fakes shared by the mirror tests: an in-memory sqlite3 stand-in for D1 and small warehouse builders."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from signalforge.data.storage import Warehouse
from signalforge.mirror import codec
from signalforge.mirror.wrangler import CMD_UNSAFE, MAX_COMMAND_CHARS, WranglerError


class FakeD1:
    """Executes the real SQL against in-memory sqlite3; returns list-of-dict rows like Wrangler.query."""

    def __init__(self, init=True):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.queries: list[str] = []
        self.files: list[str] = []
        self.fail_on_file: int | None = None
        self.drop_writes = False
        if init:
            for stmt in codec.schema_statements():
                self.con.execute(stmt)
            self.con.commit()

    def query(self, sql):
        if len(sql) > MAX_COMMAND_CHARS or any(c in sql for c in CMD_UNSAFE):
            raise WranglerError("unsafe --command")
        self.queries.append(sql)
        try:
            rows = [dict(r) for r in self.con.execute(sql).fetchall()]
        except sqlite3.Error as exc:
            raise WranglerError(f"sqlite: {exc}") from exc
        # D1 JSON numbers are JS doubles: integers above 2**53 lose precision.
        return [
            {k: int(float(v)) if isinstance(v, int) and abs(v) > 2**53 else v for k, v in r.items()}
            for r in rows
        ]

    def execute_file(self, path):
        self.files.append(str(path))
        if self.fail_on_file == len(self.files):
            raise WranglerError("injected failure")
        if self.drop_writes:
            return
        text = open(path, encoding="utf-8").read()
        self.con.executescript(text)

    def one(self, sql):
        return self.con.execute(sql).fetchone()[0]

    def as_runner(self, calls=None):
        """A subprocess.run stand-in speaking wrangler's argv and JSON."""

        def run(argv, **kwargs):
            if calls is not None:
                calls.append((argv, kwargs))
            try:
                if "--command" in argv:
                    rows = self.query(argv[argv.index("--command") + 1])
                    out = json.dumps([{"results": rows, "success": True, "meta": {}}])
                else:
                    self.execute_file(argv[argv.index("--file") + 1])
                    out = ""
                return SimpleNamespace(stdout=out, stderr="", returncode=0)
            except WranglerError as exc:
                return SimpleNamespace(stdout="", stderr=str(exc), returncode=1)

        return run


def add_bar(wh, symbol, d, close="10.5", *, source="tiingo", adj="10.4", retrieved_at=None, volume=100):
    wh.con.execute(
        "INSERT INTO bars (symbol, trade_date, open, high, low, close, adj_close, volume, volume_known, "
        "source, retrieved_at, adjustment_method, quality_status, source_reference) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [symbol, d, Decimal(close), Decimal(close), Decimal(close), Decimal(close),
         None if adj is None else Decimal(adj), volume, True, source,
         retrieved_at or datetime(2026, 10, 1, 12, 0, 0), "split_div", "ok", f"ref/{symbol}/{d}"],
    )


def add_series(wh, sid, d, value, source="fred"):
    wh.con.execute(
        "INSERT INTO series VALUES (?,?,?,?,?)",
        [sid, d, Decimal(value), source, datetime(2026, 10, 1, 12, 0, 0)],
    )


def add_action(wh, symbol, d, kind="split", ratio="2", cash=None, source="tiingo"):
    wh.con.execute(
        "INSERT INTO corporate_actions VALUES (?,?,?,?,?,?)",
        [symbol, d, kind, None if ratio is None else Decimal(ratio),
         None if cash is None else Decimal(cash), source],
    )


def make_warehouse(path) -> Warehouse:
    """Three symbols over two years, two series (one negative), eight corporate actions."""
    wh = Warehouse(path)
    for i, sym in enumerate(("AAA", "BBB", "CCC")):
        for j in range(6):
            add_bar(wh, sym, date(2023, 12, 26) + timedelta(days=j), close=f"{10 + i}.{j}5",
                    adj=None if j == 0 else f"{10 + i}.{j}")
        for j in range(6):
            add_bar(wh, sym, date(2024, 1, 1) + timedelta(days=j), close=f"{20 + i}.{j}5")
    for j in range(5):
        add_series(wh, "SERA", date(2024, 2, 1) + timedelta(days=j), f"{j}.250000")
    add_series(wh, "NEG", date(2024, 2, 1), "-0.000001")
    add_series(wh, "NEG", date(2024, 2, 2), "-123.456789")
    for j in range(8):
        add_action(wh, "AAA" if j < 4 else "BBB", date(2020, 1, 1) + timedelta(days=j),
                   kind="split" if j % 2 == 0 else "dividend",
                   ratio="2" if j % 2 == 0 else None, cash=None if j % 2 == 0 else "0.25")
    return wh
