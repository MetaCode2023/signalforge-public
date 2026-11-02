"""paper_cli --allocation: its own book, its own approval, parameters from the file, the file's cap."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
import yaml

from signalforge import paper_cli
from signalforge.core.types import Bar
from signalforge.data.storage import Warehouse
from tests.conftest import business_days

D = Decimal
DAYS = business_days(date(2020, 1, 1), 120)
START = next(d for d in DAYS if d.month == 4)  # the first session of April


def rising(symbol, slope):
    out = []
    for i, day in enumerate(DAYS):
        price = D(100) + D(slope) * D(i)
        out.append(Bar(symbol=symbol, trade_date=day, open=price, high=price, low=price, close=price,
                       volume=5_000_000, adj_close=price, source="tiingo"))
    return out


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "paper.duckdb"
    w = Warehouse(str(path))
    w.upsert_bars(rising("SPY", "0.5") + rising("AAA", "1") + rising("BBB", "0.2"))
    w.close()
    return path


@pytest.fixture
def alloc_file(tmp_path):
    path = tmp_path / "alloc.yaml"
    path.write_text(yaml.safe_dump({
        "name": "alloc", "source": "tiingo", "symbols": ["SPY", "AAA", "BBB"],
        "load_start": "2020-01-01", "train_start": "2020-01-01", "lookback": 20,
        "target_weight": "0.20", "max_single_name_exposure": "0.20",
    }), encoding="utf-8")
    return path


def run(db, *extra):
    return paper_cli.main(["--db", str(db), *extra])


def approve(db, alloc_file, strategy="tsmom"):
    return run(db, "--allocation", str(alloc_file), "--approve", strategy,
               "--approved-by", "austin", "--reason", "approved for the allocation book")


def test_allocation_is_its_own_mode():
    with pytest.raises(SystemExit):
        paper_cli.parse_args(["--allocation", "--universe"])
    assert paper_cli.parse_args(["--allocation"]).allocation == paper_cli.DEFAULT_ALLOCATION


def test_approval_records_the_files_parameters_under_its_own_book(db, alloc_file, capsys):
    assert approve(db, alloc_file) == 0
    w = Warehouse(str(db))
    try:
        rows = w.con.execute("SELECT book_id, strategy_id, parameters, approved_by FROM active_strategy").fetchall()
    finally:
        w.close()
    assert len(rows) == 1
    book, strategy, params, who = rows[0]
    assert (book, strategy, who) == ("allocation:alloc", "tsmom", "austin")
    assert json.loads(params) == {"symbols": ["AAA", "BBB", "SPY"], "lookback": 20, "target_weight": "0.20"}


def test_only_the_two_allocation_strategies_can_be_approved(db, alloc_file, capsys):
    assert approve(db, alloc_file, "ma_trend") == 2
    assert "known: ['buy_and_hold', 'tsmom']" in capsys.readouterr().out


def test_another_books_approval_does_not_authorise_the_allocation_book(db, alloc_file, capsys):
    assert run(db, "--symbols", "AAA,BBB", "--approve", "buy_and_hold",
               "--approved-by", "austin", "--reason", "other book") == 0
    capsys.readouterr()
    assert run(db, "--allocation", str(alloc_file), "--start", str(START)) == 3
    assert "REFUSED TO RUN" in capsys.readouterr().out


def test_the_approved_book_trades_at_the_files_weight(db, alloc_file, capsys):
    assert approve(db, alloc_file) == 0
    capsys.readouterr()
    assert run(db, "--allocation", str(alloc_file), "--start", str(START)) == 0
    out = capsys.readouterr().out
    assert "strategy        tsmom" in out and "approval        promotion" in out
    w = Warehouse(str(db))
    try:
        rows = w.con.execute(
            "SELECT equity, positions FROM paper_sessions WHERE book_id = 'allocation:alloc' "
            "ORDER BY session_date DESC LIMIT 1"
        ).fetchall()
    finally:
        w.close()
    equity, positions = D(str(rows[0][0])), json.loads(rows[0][1])
    held = {p["symbol"]: D(p["market_value"]) / equity for p in positions}
    assert set(held) == {"AAA", "BBB", "SPY"}
    # Above the stock book's 5% cap: the allocation's 20% is what sized them.
    assert all(share > D("0.10") for share in held.values())
