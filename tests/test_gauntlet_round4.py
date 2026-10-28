"""Adversarial gauntlet, round 4: found by running the real OHLCV data.

Only one defect, but a quiet one. `ohlcv_history` loaded every bar for a
symbol regardless of source. The warehouse holds AAPL twice: 506 bars from a
2015-2017 sample and 4 bars from a 2026 cross-section snapshot. Loading
"AAPL" therefore produced a single history with a 9.5-year hole in it, and
nothing said so.

A 20-day moving average or a z-score computed across that hole is meaningless
but looks exactly like a real number. The engine's own limitation detector did
not catch it either, because every individual bar is valid.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.core.errors import ValidationError
from signalforge.data.storage import Warehouse
from signalforge.pipeline import ohlcv_history

from tests.conftest import business_days, make_bar

D = Decimal


@pytest.fixture
def warehouse():
    w = Warehouse(":memory:")
    yield w
    w.close()


def store(warehouse, bars):
    warehouse.upsert_bars(bars)


def contiguous(symbol: str, source: str, start: date, count: int):
    return [make_bar(symbol, d, D("100"), volume=1_000_000, source=source)
            for d in business_days(start, count)]


class TestDiscontinuousHistoryIsRefusedOrDeclared:
    def test_a_multi_year_hole_in_one_symbol_is_detected(self, warehouse):
        store(warehouse, contiguous("AAPL", "sample_2015", date(2015, 2, 17), 60))
        store(warehouse, contiguous("AAPL", "snapshot_2026", date(2026, 10, 20), 4))
        with pytest.raises(ValidationError) as excinfo:
            ohlcv_history(warehouse, ["AAPL"])
        message = str(excinfo.value)
        assert "AAPL" in message
        assert "gap" in message.lower()

    def test_the_error_names_both_sources_so_the_cause_is_obvious(self, warehouse):
        store(warehouse, contiguous("AAPL", "sample_2015", date(2015, 2, 17), 60))
        store(warehouse, contiguous("AAPL", "snapshot_2026", date(2026, 10, 20), 4))
        with pytest.raises(ValidationError) as excinfo:
            ohlcv_history(warehouse, ["AAPL"])
        message = str(excinfo.value)
        assert "sample_2015" in message and "snapshot_2026" in message

    def test_restricting_to_one_source_loads_cleanly(self, warehouse):
        store(warehouse, contiguous("AAPL", "sample_2015", date(2015, 2, 17), 60))
        store(warehouse, contiguous("AAPL", "snapshot_2026", date(2026, 10, 20), 4))
        bundle = ohlcv_history(warehouse, ["AAPL"], sources=["sample_2015"])
        assert len(bundle.history.bars_for("AAPL")) == 60
        assert "sample_2015" in bundle.description

    def test_an_ordinary_holiday_gap_is_not_treated_as_a_hole(self, warehouse):
        # A long weekend or an exchange holiday must not trip this.
        bars = contiguous("AAPL", "s", date(2015, 2, 17), 60)
        kept = [b for b in bars if b.trade_date != bars[30].trade_date]
        store(warehouse, kept)
        bundle = ohlcv_history(warehouse, ["AAPL"])
        assert len(bundle.history.bars_for("AAPL")) == 59

    def test_a_gap_just_under_the_threshold_is_declared_not_refused(self, warehouse):
        # Big enough to matter, not big enough to be certainly two datasets:
        # it loads, but the limitation must state it.
        store(warehouse, contiguous("AAPL", "s", date(2015, 1, 5), 40))
        store(warehouse, contiguous("AAPL", "s", date(2015, 6, 1), 40))
        bundle = ohlcv_history(warehouse, ["AAPL"])
        joined = " ".join(bundle.limitations).lower()
        assert "gap" in joined
        assert "aapl" in joined

    def test_a_clean_single_source_history_carries_no_gap_limitation(self, warehouse):
        store(warehouse, contiguous("AAPL", "s", date(2015, 2, 17), 300))
        bundle = ohlcv_history(warehouse, ["AAPL"])
        assert not any("gap" in n.lower() for n in bundle.limitations)

    def test_two_symbols_with_different_spans_are_fine(self, warehouse):
        # Different listing dates across symbols are normal, not a hole.
        store(warehouse, contiguous("AAA", "s", date(2015, 1, 5), 300))
        store(warehouse, contiguous("BBB", "s", date(2016, 1, 5), 200))
        bundle = ohlcv_history(warehouse, ["AAA", "BBB"])
        assert set(bundle.history.symbols) == {"AAA", "BBB"}

    def test_an_empty_result_still_raises_clearly(self, warehouse):
        with pytest.raises(ValidationError):
            ohlcv_history(warehouse, ["NOSUCH"])
