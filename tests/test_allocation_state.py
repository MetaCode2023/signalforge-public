"""The Allocation tab: today's state of the trend book, and the page that shows it."""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from signalforge.allocation_cli import main
from signalforge.allocation_state import OFF, ON, YOUNG, market_state
from signalforge.config.models import AllocationConfig
from signalforge.dashboard.allocation_page import render_allocation_page
from signalforge.dashboard.assay_page import render_assay_page
from signalforge.dashboard.forecast_page import ForecastPageData, render_forecast_page
from signalforge.dashboard.render import render_dashboard
from signalforge.data.storage import Warehouse
from tests.conftest import make_bar
from tests.test_ohlcv_research import DAYS

D = Decimal


def bars(symbol, prices, days):
    return [make_bar(symbol, d, D(str(p)), adj_close=D(str(p)), volume=5_000_000) for d, p in zip(days, prices)]


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "wh.duckdb"))
    n = len(DAYS)
    month_start = next(i for i in range(n - 1, 0, -1) if DAYS[i].month != DAYS[i - 1].month)
    peak = 100 + month_start
    turn = [100 + i for i in range(month_start)] + [
        peak * (1 - 0.6 * min(1, (i + 1) / 5)) for i in range(n - month_start)
    ]
    w.upsert_bars(
        bars("UPP", [100 + i for i in range(n)], DAYS)
        + bars("DWN", [1000 - i for i in range(n)], DAYS)
        + bars("TRN", turn, DAYS)              # rose to the month's start, collapsing since
        + bars("NEW", [50 + i for i in range(30)], DAYS[-30:])
        + bars("OLD", [100 + i for i in range(n - 10)], DAYS[:-10])
    )
    yield w
    w.close()


def allocation(**over):
    payload = {
        "name": "alloc", "source": "fixture", "symbols": ("UPP", "DWN", "TRN", "NEW", "OLD"),
        "load_start": DAYS[0], "train_start": DAYS[0], "lookback": 60,
        "target_weight": D("0.10"), "max_single_name_exposure": D("0.10"),
        "classes": {"UPP": "Rising <asset>"}, **over,
    }
    return AllocationConfig(**payload)


def by_symbol(state):
    return {a.symbol: a for a in state.assets}


def test_each_asset_gets_the_rules_state(warehouse):
    state = market_state(warehouse, allocation())
    got = by_symbol(state)
    assert state.as_of == DAYS[-1]
    assert got["UPP"].state == ON and got["DWN"].state == OFF and got["NEW"].state == YOUNG
    assert got["UPP"].below_high == 0
    assert got["DWN"].below_high < 0
    assert got["UPP"].asset_class == "Rising <asset>"


def test_a_mid_month_collapse_is_flagged_but_not_acted_on(warehouse):
    trn = by_symbol(market_state(warehouse, allocation()))["TRN"]
    assert trn.state == ON
    assert trn.current_return < 0
    assert trn.flip_watch


def test_invested_counts_only_held_assets_and_stale_is_marked(warehouse):
    state = market_state(warehouse, allocation())
    held = [a.symbol for a in state.assets if a.state == ON]
    assert state.invested == D("0.10") * len(held)
    assert by_symbol(state)["OLD"].stale and not by_symbol(state)["UPP"].stale


def test_next_decision_is_the_following_month(warehouse):
    state = market_state(warehouse, allocation())
    nxt = state.next_decision_month
    assert nxt.day == 1 and nxt > state.as_of and (nxt.year * 12 + nxt.month) - (state.as_of.year * 12 + state.as_of.month) == 1


def test_the_page_shows_the_book_and_escapes_everything(warehouse):
    html = render_allocation_page(market_state(warehouse, allocation()))
    assert "HELD" in html and "CASH" in html and "WAITING" in html
    assert "Rising &lt;asset&gt;" in html and "<asset>" not in html
    assert "<script" not in html.lower()
    assert "Leaning for next month" in html and "TRN" in html


def test_the_empty_page_says_how_to_fill_it():
    html = render_allocation_page(None)
    assert "DATA UNAVAILABLE" in html and "allocation_cli --state" in html


def test_all_four_pages_carry_all_four_tabs():
    pages = {
        "index.html": render_dashboard(),
        "forecast.html": render_forecast_page(ForecastPageData()),
        "assay.html": render_assay_page(None),
        "allocation.html": render_allocation_page(None),
    }
    for here, html in pages.items():
        for href in pages:
            assert f'href="{href}"' in html
        assert f'<a href="{here}" aria-current="page">' in html


def test_the_cli_writes_the_tab_and_records_nothing(tmp_path, warehouse, capsys):
    path = tmp_path / "alloc.yaml"
    payload = allocation().model_dump(mode="json")
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    db = warehouse.path if hasattr(warehouse, "path") else tmp_path / "wh.duckdb"
    warehouse.close()
    out = tmp_path / "allocation.html"
    assert main(["--state", "--db", str(db), "--allocation-file", str(path), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "leaning the other way" in printed and "STALE" in printed
    assert "HELD" in out.read_text(encoding="utf-8")
    reopened = Warehouse(str(db))
    try:
        tables = {r[0] for r in reopened.con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        assert "experiments" not in tables or reopened.con.execute("SELECT count(*) FROM experiments").fetchone()[0] == 0
    finally:
        reopened.close()
