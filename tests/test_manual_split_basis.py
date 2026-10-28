"""The adjusted basis honours manual splits.

A vendor that missed a split leaves its adj_close discontinuous at that date.
A split recorded with `record_manual_split` (source 'manual') must be folded
into the adjusted basis for bars BEFORE its effective date. Vendor splits are
never re-applied: they are already inside adj_close.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.types import Bar
from signalforge.data.storage import Warehouse
from signalforge.pipeline import ohlcv_history

from tests.conftest import business_days

D = Decimal
DAYS = business_days(date(2020, 1, 1), 40)
SPLIT_DAY = DAYS[20]


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "manual_basis.duckdb"))
    yield w
    w.close()


def seed(warehouse, symbol="CVX", drop_at=20, factor=2, volume=1000):
    """Raw and adj_close both halve at `drop_at`: the vendor missed the split."""
    bars = []
    for i, day in enumerate(DAYS):
        price = D(100) if i < drop_at else D(100) / factor
        bars.append(
            Bar(symbol=symbol, trade_date=day, open=price, high=price, low=price,
                close=price, volume=volume, adj_close=price, source="tiingo")
        )
    warehouse.upsert_bars(bars)


def closes(bundle, symbol="CVX"):
    return [b.close for b in bundle.history.bars_for(symbol)]


class TestManualSplitsOnAdjustedBasis:
    def test_a_missed_split_leaves_a_discontinuity_without_the_manual_record(self, warehouse):
        seed(warehouse)
        series = closes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted"))
        assert series[19] == D(100) and series[20] == D(50)

    def test_a_manual_split_makes_the_adjusted_series_continuous(self, warehouse):
        seed(warehouse)
        warehouse.record_manual_split("CVX", SPLIT_DAY, D("2"))
        bundle = ohlcv_history(warehouse, ["CVX"], price_basis="adjusted")
        assert set(closes(bundle)) == {D(50)}

    def test_every_price_field_and_adj_close_are_scaled_before_the_split_only(self, warehouse):
        seed(warehouse)
        warehouse.record_manual_split("CVX", SPLIT_DAY, D("2"))
        bars = ohlcv_history(warehouse, ["CVX"], price_basis="adjusted").history.bars_for("CVX")
        before, on = bars[19], bars[20]
        assert (before.open, before.high, before.low, before.close, before.adj_close) == (D(50),) * 5
        assert on.close == D(50) and on.volume == 1000

    def test_volume_before_the_split_scales_the_other_way(self, warehouse):
        seed(warehouse)
        warehouse.record_manual_split("CVX", SPLIT_DAY, D("2"))
        bars = ohlcv_history(warehouse, ["CVX"], price_basis="adjusted").history.bars_for("CVX")
        assert bars[0].volume == 2000 and bars[20].volume == 1000

    def test_two_manual_splits_compound(self, warehouse):
        bars = []
        for i, day in enumerate(DAYS):
            price = D(120) if i < 10 else (D(60) if i < 20 else D(20))
            bars.append(Bar(symbol="CVX", trade_date=day, open=price, high=price, low=price,
                            close=price, volume=100, adj_close=price, source="tiingo"))
        warehouse.upsert_bars(bars)
        warehouse.record_manual_split("CVX", DAYS[10], D("2"))
        warehouse.record_manual_split("CVX", DAYS[20], D("3"))
        assert set(closes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted"))) == {D(20)}

    def test_a_reverse_manual_split_scales_up(self, warehouse):
        seed(warehouse, factor=D("0.5"))
        warehouse.record_manual_split("CVX", SPLIT_DAY, D("0.5"))
        assert set(closes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted"))) == {D(200)}

    def test_a_manual_split_for_another_symbol_changes_nothing(self, warehouse):
        seed(warehouse)
        seed(warehouse, symbol="XOM", drop_at=20)
        before = ohlcv_history(warehouse, ["CVX", "XOM"], price_basis="adjusted")
        warehouse.record_manual_split("XOM", SPLIT_DAY, D("2"))
        after = ohlcv_history(warehouse, ["CVX", "XOM"], price_basis="adjusted")
        assert before.history.bars_for("CVX") == after.history.bars_for("CVX")
        assert before.history.bars_for("XOM") != after.history.bars_for("XOM")

    def test_no_manual_splits_is_byte_identical(self, warehouse):
        seed(warehouse)
        bundle = ohlcv_history(warehouse, ["CVX"], price_basis="adjusted")
        expected = [
            (b.open, b.high, b.low, b.close, b.volume, b.adj_close)
            for b in bundle.history.bars_for("CVX")
        ]
        assert all(t == (D(100),) * 4 + (1000, D(100)) for t in expected[:20])
        assert all(t == (D(50),) * 4 + (1000, D(50)) for t in expected[20:])
        assert not any("MANUAL" in n for n in bundle.limitations)

    def test_a_vendor_split_on_another_date_does_not_stop_the_manual_one(self, warehouse):
        """A vendor split already folded into adj_close at day 10, and a missed one at 20.

        If _apply_manual_splits were a no-op the second discontinuity would remain.
        """
        bars = []
        for i, day in enumerate(DAYS):
            raw = D(120) if i < 10 else (D(60) if i < 20 else D(30))
            adj = D(60) if i < 20 else D(30)
            bars.append(Bar(symbol="CVX", trade_date=day, open=raw, high=raw, low=raw,
                            close=raw, volume=100, adj_close=adj,
                            source="tiingo"))
        warehouse.upsert_bars(bars)
        warehouse.upsert_corporate_actions([
            {"symbol": "CVX", "effective_date": DAYS[10], "action_type": "split",
             "ratio": D("2"), "source": "tiingo"},
        ])
        warehouse.record_manual_split("CVX", DAYS[20], D("2"))
        series = closes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted"))
        assert len(set(series)) == 1

    def test_a_manual_split_the_vendor_already_folded_in_is_not_applied_again(self, warehouse):
        bars = []
        for i, day in enumerate(DAYS):
            raw = D(100) if i < 20 else D(50)
            bars.append(Bar(symbol="CVX", trade_date=day, open=raw, high=raw, low=raw,
                            close=raw, volume=1000, adj_close=D(50), source="tiingo"))
        warehouse.upsert_bars(bars)
        warehouse.record_manual_split("CVX", SPLIT_DAY, D("2"))
        bundle = ohlcv_history(warehouse, ["CVX"], price_basis="adjusted")
        assert set(closes(bundle)) == {D(50)}
        assert bundle.history.bars_for("CVX")[0].volume == 2000
        note = [n for n in bundle.limitations if "MANUAL SPLIT SKIPPED" in n]
        assert note and "CVX" in note[0] and str(SPLIT_DAY) in note[0]
        assert not any("MANUAL SPLIT ADJUSTMENT" in n for n in bundle.limitations)

    def test_a_manual_split_with_no_bar_on_its_date_cannot_be_verified_and_is_skipped(
        self, warehouse
    ):
        seed(warehouse)
        warehouse.record_manual_split("CVX", date(2030, 1, 2), D("2"))
        bundle = ohlcv_history(warehouse, ["CVX"], price_basis="adjusted")
        assert any("MANUAL SPLIT SKIPPED" in n for n in bundle.limitations)

    def test_an_exact_manual_adjustment_is_applied_when_the_vendor_shows_the_jump(self, warehouse):
        bars = []
        for i, day in enumerate(DAYS):
            raw = D(100) if i < 20 else D(100) / D("2.081110")
            bars.append(Bar(symbol="CVX", trade_date=day, open=raw, high=raw, low=raw,
                            close=raw, volume=1000, adj_close=raw, source="tiingo"))
        warehouse.upsert_bars(bars)
        warehouse.record_manual_split("CVX", SPLIT_DAY, D("2.081110"))
        series = closes(ohlcv_history(warehouse, ["CVX"], price_basis="adjusted"))
        assert max(series) - min(series) < D("0.0001")

    def test_the_raw_basis_ignores_manual_splits(self, warehouse):
        seed(warehouse)
        warehouse.record_manual_split("CVX", SPLIT_DAY, D("2"))
        series = closes(ohlcv_history(warehouse, ["CVX"], price_basis="raw"))
        assert series[19] == D(100) and series[20] == D(50)

    def test_applied_manual_splits_are_declared_in_the_limitations(self, warehouse):
        seed(warehouse)
        warehouse.record_manual_split("CVX", SPLIT_DAY, D("2"))
        bundle = ohlcv_history(warehouse, ["CVX"], price_basis="adjusted")
        assert any("MANUAL SPLIT" in n and "CVX" in n for n in bundle.limitations)
