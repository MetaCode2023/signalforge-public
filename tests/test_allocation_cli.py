"""allocation_cli: config guards, the research-only risk profile, and a full run."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from signalforge.allocation_cli import main, run_allocation
from signalforge.config.models import AllocationConfig, RiskConfig, load_all, load_allocation
from signalforge.core.errors import ConfigError
from signalforge.data.storage import Warehouse
from signalforge.reports.events import dataset_kind
from tests.test_ohlcv_research import DAYS, research, seed_universe

D = Decimal
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "wh.duckdb"))
    seed_universe(w)  # AAA, BBB rise; CCC falls; SPY rises
    yield w
    w.close()


def allocation(**over) -> AllocationConfig:
    payload = {
        "name": "alloc",
        "source": "fixture",
        "symbols": ("AAA", "BBB", "CCC"),
        "load_start": DAYS[0],
        "train_start": DAYS[60],
        "lookback": 60,
        "target_weight": D("0.10"),
        "max_single_name_exposure": D("0.10"),
        **over,
    }
    return AllocationConfig(**payload)


def config():
    return load_all().model_copy(update={"research": research()})


@pytest.mark.parametrize(
    "over",
    [
        {"max_single_name_exposure": D("0.30")},
        {"target_weight": D("0.20")},
        {"target_weight": D("0.25"), "max_single_name_exposure": D("0.25"),
         "symbols": ("A", "B", "C", "D", "E")},
        {"symbols": ("AAA", "aaa")},
        {"lookback": 0},
        {"load_start": DAYS[61]},
        {"name": "Not A Slug"},
    ],
)
def test_incoherent_allocations_are_refused(over):
    with pytest.raises(ConfigError):
        allocation(**over)


def test_the_risk_profile_changes_only_the_single_name_cap():
    base = RiskConfig()
    changed = allocation().risk(base)
    assert changed.max_single_name_exposure == D("0.10")
    assert {**changed.model_dump(), "max_single_name_exposure": None} == {
        **base.model_dump(), "max_single_name_exposure": None
    }


def test_the_checked_in_allocation_loads_inside_the_train_window():
    loaded = load_allocation(ROOT / "config" / "allocations" / "assets10.yaml")
    assert loaded.symbols == ("SPY", "IWM", "EFA", "EEM", "IEF", "TLT", "LQD", "HYG", "GLD", "VNQ")
    assert loaded.target_weight * len(loaded.symbols) == 1
    splits = loaded.effective_splits(load_all().research.splits)
    assert splits.train_start == loaded.train_start


def test_a_run_covers_train_and_validation_and_records_both(warehouse):
    output = run_allocation(warehouse, allocation(), config())
    assert [run.window for run in output.runs] == ["train", "validation"]
    for run in output.runs:
        assert [o.strategy_id for o in run.outcomes] == ["buy_and_hold", "tsmom", "tsmom_nobreaker"]
        assert any(n.startswith("ALLOCATION BOOK alloc: 3 ETFs at 0.10") for n in run.limitations)
    validation = output.runs[1]
    trend = validation.by_id("tsmom")
    bought = {f.symbol for f in trend.result.fills if f.side.value == "BUY"}
    assert "AAA" in bought and "CCC" not in bought
    rows = warehouse.con.execute(
        "SELECT dataset_description, evaluated_window, train_start FROM experiments"
    ).fetchall()
    assert len(rows) == len(output.experiment_ids) == 6
    assert {dataset_kind(r[0]) for r in rows} == {"ohlcv-alloc-fixture"}
    assert {r[1] for r in rows} == {"train", "validation"}
    assert {r[2] for r in rows} == {DAYS[60]}


def test_an_entry_is_sized_to_the_allocation_weight_not_the_stock_cap(warehouse):
    output = run_allocation(warehouse, allocation(), config())
    basket = output.runs[1].by_id("buy_and_hold")
    first = {}
    for fill in basket.result.fills:
        first.setdefault(fill.symbol, fill)
    equity = basket.result.starting_cash
    for fill in first.values():
        assert fill.quantity * fill.price > equity * D("0.06")


def test_the_holdout_is_refused(capsys):
    assert main(["--run-holdout"]) == 2
    assert "held-out test window is not run" in capsys.readouterr().out


def test_the_paper_engine_never_names_the_allocation_profile():
    # paper_cli chooses the risk config for an allocation book (decision 20);
    # the runner and broker only ever receive one and cannot pick their own.
    for path in (ROOT / "signalforge" / "paper").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "allocation" not in text.lower(), path
