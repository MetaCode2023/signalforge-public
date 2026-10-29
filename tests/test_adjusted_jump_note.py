"""The adjusted basis names large one-day moves nobody has explained.

The paper runner refuses a raw move beyond 1.4x with no recorded split or
acknowledgement. Research on the adjusted basis used to load the same bad
bar silently: two splits the vendor missed on CVX once flipped a 34-year
result. It now declares every such move in the run's limitations.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.types import Bar
from signalforge.data.storage import Warehouse
from signalforge.pipeline import ADJUSTED_JUMPS_LISTED, ohlcv_history

from tests.conftest import business_days

D = Decimal
DAYS = business_days(date(2020, 1, 1), 40)
DROP_DAY = DAYS[20]


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "jumps.duckdb"))
    yield w
    w.close()


def seed(warehouse, symbol="CVX", factor=D(2), drops=(20,)):
    """Raw and adj_close both fall by `factor` at each index in `drops`."""
    bars, price = [], D(100)
    for i, day in enumerate(DAYS):
        if i in drops:
            price = price / factor
        bars.append(Bar(symbol=symbol, trade_date=day, open=price, high=price, low=price,
                        close=price, volume=1000, adj_close=price, source="tiingo"))
    warehouse.upsert_bars(bars)


def jump_notes(bundle):
    return [n for n in bundle.limitations if n.startswith("UNEXPLAINED ADJUSTED-PRICE JUMPS")]


def test_a_missed_split_is_declared_with_symbol_date_and_size(warehouse):
    seed(warehouse)
    (note,) = jump_notes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted"))
    assert f"CVX {DROP_DAY}" in note
    assert "2.00x" in note
    assert note.startswith("UNEXPLAINED ADJUSTED-PRICE JUMPS: 1 one-day move(s)")


def test_a_recorded_manual_split_clears_its_jump(warehouse):
    seed(warehouse)
    warehouse.record_manual_split("CVX", DROP_DAY, D(2))
    assert jump_notes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted")) == []


def test_an_acknowledged_real_move_is_not_listed(warehouse):
    seed(warehouse)
    warehouse.acknowledge_price_move("CVX", DROP_DAY, "inspected: genuine crash")
    assert jump_notes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted")) == []


def test_a_move_below_the_threshold_is_not_listed(warehouse):
    seed(warehouse, factor=D("1.3"))
    assert jump_notes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted")) == []


def test_raw_basis_is_untouched(warehouse):
    # On raw prices a vendor split is a legitimate jump; the paper runner owns that check.
    seed(warehouse)
    assert jump_notes(ohlcv_history(warehouse, ["CVX"], price_basis="raw")) == []


def test_a_long_list_is_capped_and_states_the_true_total(warehouse):
    drops = tuple(range(2, 38, 3))
    seed(warehouse, drops=drops)
    (note,) = jump_notes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted"))
    assert note.startswith(f"UNEXPLAINED ADJUSTED-PRICE JUMPS: {len(drops)} one-day move(s)")
    assert note.count("CVX ") == ADJUSTED_JUMPS_LISTED
    assert f"and {len(drops) - ADJUSTED_JUMPS_LISTED} more" in note
