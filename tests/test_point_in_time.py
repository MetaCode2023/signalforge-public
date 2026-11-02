"""The look-ahead guard.

A strategy is handed a PointInTimeView, never a full series. The view
physically cannot return a bar dated after its as_of. These tests are the
single most important ones in the project: if they pass, no strategy can see
the future by accident, regardless of how it is written.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from signalforge.core.errors import LookAheadError, ValidationError
from signalforge.core.history import PriceHistory, PointInTimeView
from signalforge.core.types import Bar

D = Decimal


def mk_bar(day: int, close: str, symbol: str = "TEST", volume: int = 1000) -> Bar:
    c = D(close)
    return Bar(
        symbol=symbol,
        trade_date=date(2020, 1, day),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        adj_close=c,
        source="fixture",
    )


@pytest.fixture
def history() -> PriceHistory:
    bars = [mk_bar(d, str(100 + d)) for d in range(1, 11)]
    return PriceHistory.from_bars(bars)


class TestPriceHistory:
    def test_knows_its_symbols_and_dates(self, history):
        assert history.symbols == ("TEST",)
        assert history.trading_days[0] == date(2020, 1, 1)
        assert history.trading_days[-1] == date(2020, 1, 10)

    def test_rejects_duplicate_symbol_date_pairs(self):
        with pytest.raises(Exception):
            PriceHistory.from_bars([mk_bar(1, "100"), mk_bar(1, "101")])

    def test_trading_days_are_sorted_and_unique(self):
        bars = [mk_bar(3, "103"), mk_bar(1, "101"), mk_bar(2, "102")]
        h = PriceHistory.from_bars(bars)
        assert h.trading_days == (date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3))


class TestPointInTimeView:
    def test_closes_stop_at_as_of_inclusive(self, history):
        view = history.view(date(2020, 1, 5))
        closes = view.closes("TEST")
        assert len(closes) == 5
        assert closes[-1] == D("105")

    def test_cannot_reach_a_future_bar(self, history):
        view = history.view(date(2020, 1, 5))
        with pytest.raises(LookAheadError):
            view.bar("TEST", date(2020, 1, 6))

    def test_can_reach_a_past_bar(self, history):
        view = history.view(date(2020, 1, 5))
        assert view.bar("TEST", date(2020, 1, 3)).close == D("103")

    def test_latest_bar_is_the_as_of_bar(self, history):
        view = history.view(date(2020, 1, 5))
        assert view.latest("TEST").trade_date == date(2020, 1, 5)

    def test_latest_falls_back_to_the_most_recent_prior_bar(self, history):
        # 2020-01-11 is not in the series; the latest known bar is the 10th.
        view = history.view(date(2020, 1, 11))
        assert view.latest("TEST").trade_date == date(2020, 1, 10)

    def test_latest_is_none_before_any_data_exists(self, history):
        view = history.view(date(2019, 1, 1))
        assert view.latest("TEST") is None

    def test_trading_days_are_truncated_at_as_of(self, history):
        view = history.view(date(2020, 1, 4))
        assert view.trading_days[-1] == date(2020, 1, 4)
        assert len(view.trading_days) == 4

    def test_window_returns_at_most_n_trailing_closes(self, history):
        view = history.view(date(2020, 1, 10))
        assert view.closes("TEST", lookback=3) == (D("108"), D("109"), D("110"))

    def test_window_shorter_than_requested_is_returned_as_is(self, history):
        view = history.view(date(2020, 1, 2))
        assert view.closes("TEST", lookback=50) == (D("101"), D("102"))

    def test_has_history_reports_whether_warmup_is_satisfied(self, history):
        assert history.view(date(2020, 1, 3)).has_history("TEST", 3) is True
        assert history.view(date(2020, 1, 3)).has_history("TEST", 4) is False

    def test_unknown_symbol_returns_empty_rather_than_raising(self, history):
        view = history.view(date(2020, 1, 5))
        assert view.closes("NOPE") == ()
        assert view.latest("NOPE") is None

    def test_view_is_immutable(self, history):
        view = history.view(date(2020, 1, 5))
        with pytest.raises(Exception):
            view.as_of = date(2020, 1, 10)

    def test_mutating_returned_closes_cannot_corrupt_the_history(self, history):
        view = history.view(date(2020, 1, 5))
        closes = view.closes("TEST")
        assert isinstance(closes, tuple), "a mutable sequence would let callers edit history"

    def test_two_views_of_the_same_history_are_independent(self, history):
        early = history.view(date(2020, 1, 2))
        late = history.view(date(2020, 1, 9))
        assert len(early.closes("TEST")) == 2
        assert len(late.closes("TEST")) == 9

    def test_cross_section_returns_only_symbols_with_a_bar_at_or_before_as_of(self):
        bars = [
            mk_bar(1, "100", symbol="A"),
            mk_bar(5, "200", symbol="B"),
        ]
        h = PriceHistory.from_bars(bars)
        view = h.view(date(2020, 1, 3))
        assert set(view.cross_section()) == {"A"}
        later = h.view(date(2020, 1, 6))
        assert set(later.cross_section()) == {"A", "B"}

    def test_marks_only_include_symbols_known_at_as_of(self):
        bars = [mk_bar(1, "100", symbol="A"), mk_bar(5, "200", symbol="B")]
        h = PriceHistory.from_bars(bars)
        assert h.view(date(2020, 1, 3)).marks() == {"A": D("100")}

    def test_returns_series_is_derived_only_from_visible_closes(self, history):
        view = history.view(date(2020, 1, 4))
        rets = view.returns("TEST")
        assert len(rets) == 3  # 4 closes produce 3 returns


class TestNextTradingDay:
    def test_next_trading_day_after_a_known_day(self, history):
        assert history.next_trading_day(date(2020, 1, 5)) == date(2020, 1, 6)

    def test_next_trading_day_skips_a_gap(self):
        bars = [mk_bar(1, "100"), mk_bar(6, "106")]
        h = PriceHistory.from_bars(bars)
        assert h.next_trading_day(date(2020, 1, 1)) == date(2020, 1, 6)

    def test_next_trading_day_is_none_at_the_end_of_the_series(self, history):
        assert history.next_trading_day(date(2020, 1, 10)) is None

    def test_next_trading_day_from_a_non_trading_day(self):
        bars = [mk_bar(1, "100"), mk_bar(6, "106")]
        h = PriceHistory.from_bars(bars)
        assert h.next_trading_day(date(2020, 1, 3)) == date(2020, 1, 6)


class TestAccessorsMatchFullSliceSemantics:
    """The bisect-based accessors must equal a naive full-slice reference."""

    @staticmethod
    def _reference(history, symbol, as_of):
        return tuple(b for b in history.bars_for(symbol) if b.trade_date <= as_of)

    @settings(max_examples=60, deadline=None)
    @given(
        starts=st.lists(st.integers(1, 20), min_size=1, max_size=4),
        lengths=st.lists(st.integers(1, 15), min_size=1, max_size=4),
        as_of_offset=st.integers(0, 40),
        lookback=st.integers(1, 25),
    )
    def test_view_accessors_match_full_slice_semantics(
        self, starts, lengths, as_of_offset, lookback
    ):
        base = date(2020, 1, 1)
        bars = []
        for index, (start, length) in enumerate(zip(starts, lengths)):
            for i in range(length):
                c = D(50 + index * 10 + i)
                bars.append(
                    Bar(
                        symbol=f"S{index}",
                        trade_date=base + timedelta(days=start + i),
                        open=c, high=c, low=c, close=c, volume=1000,
                        adj_close=c + D("0.5"), source="fixture",
                    )
                )
        history = PriceHistory.from_bars(bars)
        as_of = base + timedelta(days=as_of_offset)
        view = history.view(as_of)
        seen = []
        for symbol in history.symbols:
            ref = self._reference(history, symbol, as_of)
            assert view.bars(symbol) == ref
            assert view.bars(symbol, lookback) == ref[-lookback:]
            assert view.closes(symbol, lookback) == tuple(b.close for b in ref[-lookback:])
            assert view.adjusted_closes(symbol, lookback) == tuple(
                b.adj_close for b in ref[-lookback:]
            )
            prices = tuple(b.adj_close for b in ref[-(lookback + 1):])
            assert view.returns(symbol, lookback) == tuple(
                prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))
            )
            assert view.latest(symbol) == (ref[-1] if ref else None)
            for required in (0, 1, len(ref), len(ref) + 1):
                assert view.has_history(symbol, required) is (len(ref) >= required)
            if ref:
                seen.append(symbol)
        assert view.cross_section() == tuple(seen)
        assert view.marks() == {s: self._reference(history, s, as_of)[-1].close for s in seen}

    def test_bars_lookback_zero_still_raises(self, history):
        view = history.view(date(2020, 1, 5))
        with pytest.raises(ValidationError):
            view.bars("TEST", 0)
        with pytest.raises(ValidationError):
            view.bars("TEST", -1)
