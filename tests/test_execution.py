"""The fill model.

This is where look-ahead bias either gets prevented or quietly introduced, so
the first test class is about refusing to fill on the signal bar at all.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.execution import FillModel
from signalforge.config.models import ExecutionConfig
from signalforge.core.errors import LookAheadError
from signalforge.core.types import Bar, Order, OrderType, Side

from tests.conftest import make_bar

D = Decimal
SIGNAL_DAY = date(2020, 1, 6)
FILL_DAY = date(2020, 1, 7)


def mk_order(**kw) -> Order:
    base = dict(
        order_id="o1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=100,
        order_type=OrderType.MARKET,
        signal_date=SIGNAL_DAY,
        strategy_id="s",
        reason="r",
    )
    base.update(kw)
    return Order(**base)


def mk_bar(day=FILL_DAY, **kw) -> Bar:
    base = dict(open_=D("100"), high=D("102"), low=D("98"), volume=1_000_000)
    base.update(kw)
    close = base.pop("close", D("101"))
    return make_bar("AAPL", day, close, **base)


def model(**overrides) -> FillModel:
    return FillModel(ExecutionConfig(**overrides))


class TestLookAheadRefusal:
    def test_refuses_to_fill_on_the_signal_bar(self):
        fm = model()
        with pytest.raises(LookAheadError):
            fm.fill(mk_order(), mk_bar(day=SIGNAL_DAY), previous_close=D("100"))

    def test_refuses_to_fill_on_a_bar_before_the_signal(self):
        fm = model()
        with pytest.raises(LookAheadError):
            fm.fill(mk_order(), mk_bar(day=date(2020, 1, 2)), previous_close=D("100"))

    def test_fills_on_the_bar_after_the_signal(self):
        fm = model()
        fill = fm.fill(mk_order(), mk_bar(), previous_close=D("100"))
        assert fill is not None
        assert fill.fill_date == FILL_DAY


class TestMarketOrders:
    def test_a_buy_fills_at_the_open_plus_slippage(self):
        fm = model(slippage_bps=D("5"))
        fill = fm.fill(mk_order(), mk_bar(), previous_close=D("100"))
        # 100 * (1 + 0.0005) = 100.05
        assert fill.price == D("100.05")
        assert fill.reference_price == D("100")

    def test_a_sell_fills_at_the_open_minus_slippage(self):
        fm = model(slippage_bps=D("5"))
        fill = fm.fill(mk_order(side=Side.SELL), mk_bar(), previous_close=D("100"))
        assert fill.price == D("99.95")

    def test_slippage_always_works_against_the_order(self):
        fm = model(slippage_bps=D("50"))
        buy = fm.fill(mk_order(), mk_bar(), previous_close=D("100"))
        sell = fm.fill(mk_order(side=Side.SELL), mk_bar(), previous_close=D("100"))
        assert buy.price > D("100")
        assert sell.price < D("100")

    def test_slippage_cost_is_recorded_for_reporting(self):
        fm = model(slippage_bps=D("5"))
        fill = fm.fill(mk_order(quantity=100), mk_bar(), previous_close=D("100"))
        # 0.05 per share on 100 shares
        assert fill.slippage_cost == D("5.000")

    def test_zero_slippage_is_possible_but_not_the_default(self):
        assert ExecutionConfig().slippage_bps > 0
        fm = model(slippage_bps=D("0"))
        fill = fm.fill(mk_order(), mk_bar(), previous_close=D("100"))
        assert fill.price == D("100")

    def test_commission_is_per_share_with_a_floor(self):
        fm = model(commission_per_share=D("0.005"), min_commission=D("1.00"))
        small = fm.fill(mk_order(quantity=10), mk_bar(), previous_close=D("100"))
        assert small.fees == D("1.00")  # floor binds
        large = fm.fill(mk_order(quantity=1000), mk_bar(), previous_close=D("100"))
        assert large.fees == D("5.000")  # per-share binds


class TestGaps:
    def test_a_gap_up_against_a_buy_costs_extra_slippage(self):
        fm = model(slippage_bps=D("5"), gap_slippage_multiplier=D("2.0"))
        # Previous close 100, opens at 110: a 10% gap through our reference.
        gapped = mk_bar(open_=D("110"), high=D("112"), low=D("109"), close=D("111"))
        fill = fm.fill(mk_order(), gapped, previous_close=D("100"))
        # 110 * (1 + 0.0005 * 2) = 110.11
        assert fill.price == D("110.11")

    def test_a_gap_in_our_favour_does_not_award_a_discount(self):
        # A buy gapping down is lucky, but we do not model luck as a bonus:
        # slippage stays at the base rate rather than turning negative.
        fm = model(slippage_bps=D("5"), gap_slippage_multiplier=D("2.0"))
        gapped = mk_bar(open_=D("90"), high=D("92"), low=D("89"), close=D("91"))
        fill = fm.fill(mk_order(), gapped, previous_close=D("100"))
        assert fill.price == D("90.045")

    def test_no_previous_close_means_no_gap_adjustment(self):
        fm = model(slippage_bps=D("5"))
        fill = fm.fill(mk_order(), mk_bar(), previous_close=None)
        assert fill.price == D("100.05")


class TestLimitOrders:
    def test_a_buy_limit_above_the_open_fills_at_the_open(self):
        fm = model(slippage_bps=D("0"))
        order = mk_order(order_type=OrderType.LIMIT, limit_price=D("105"))
        fill = fm.fill(order, mk_bar(), previous_close=D("100"))
        assert fill.price == D("100")

    def test_a_buy_limit_inside_the_bar_fills_at_the_limit(self):
        fm = model(slippage_bps=D("0"))
        order = mk_order(order_type=OrderType.LIMIT, limit_price=D("99"))
        fill = fm.fill(order, mk_bar(), previous_close=D("100"))
        assert fill.price == D("99")

    def test_a_buy_limit_below_the_bar_low_does_not_fill(self):
        fm = model()
        order = mk_order(order_type=OrderType.LIMIT, limit_price=D("90"))
        assert fm.fill(order, mk_bar(), previous_close=D("100")) is None

    def test_a_sell_limit_above_the_bar_high_does_not_fill(self):
        fm = model()
        order = mk_order(
            side=Side.SELL, order_type=OrderType.LIMIT, limit_price=D("110")
        )
        assert fm.fill(order, mk_bar(), previous_close=D("100")) is None

    def test_a_sell_limit_inside_the_bar_fills_at_the_limit(self):
        fm = model(slippage_bps=D("0"))
        order = mk_order(
            side=Side.SELL, order_type=OrderType.LIMIT, limit_price=D("101")
        )
        fill = fm.fill(order, mk_bar(), previous_close=D("100"))
        assert fill.price == D("101")

    def test_a_limit_touched_only_at_the_extreme_does_not_fill(self):
        # Assuming a fill because the low printed exactly at our limit means
        # assuming we were first in the queue. We refuse that assumption.
        fm = model()
        order = mk_order(order_type=OrderType.LIMIT, limit_price=D("98"))
        assert fm.fill(order, mk_bar(low=D("98")), previous_close=D("100")) is None


class TestLiquidity:
    def test_a_zero_volume_bar_produces_no_fill(self):
        fm = model()
        assert fm.fill(mk_order(), mk_bar(volume=0), previous_close=D("100")) is None

    def test_an_order_above_the_participation_cap_is_partially_filled(self):
        fm = model(max_participation_of_volume=D("0.01"), allow_partial_fills=True)
        fill = fm.fill(
            mk_order(quantity=50_000), mk_bar(volume=1_000_000), previous_close=D("100")
        )
        assert fill.quantity == 10_000  # 1% of 1,000,000

    def test_partial_fills_can_be_switched_off_entirely(self):
        fm = model(max_participation_of_volume=D("0.01"), allow_partial_fills=False)
        assert (
            fm.fill(mk_order(quantity=50_000), mk_bar(volume=1_000_000), previous_close=D("100"))
            is None
        )

    def test_an_order_inside_the_cap_fills_in_full(self):
        fm = model(max_participation_of_volume=D("0.01"))
        fill = fm.fill(mk_order(quantity=100), mk_bar(volume=1_000_000), previous_close=D("100"))
        assert fill.quantity == 100


class TestFillIdentity:
    def test_each_fill_carries_a_distinct_id_derived_from_the_order_and_date(self):
        fm = model()
        a = fm.fill(mk_order(order_id="oA"), mk_bar(), previous_close=D("100"))
        b = fm.fill(mk_order(order_id="oB"), mk_bar(), previous_close=D("100"))
        assert a.fill_id != b.fill_id

    def test_the_same_order_on_the_same_bar_yields_the_same_fill_id(self):
        # Replay safety: re-running a bar must not create a second fill.
        fm = model()
        a = fm.fill(mk_order(), mk_bar(), previous_close=D("100"))
        b = fm.fill(mk_order(), mk_bar(), previous_close=D("100"))
        assert a.fill_id == b.fill_id

    def test_a_fill_is_deterministic(self):
        fm = model()
        a = fm.fill(mk_order(), mk_bar(), previous_close=D("100"))
        b = fm.fill(mk_order(), mk_bar(), previous_close=D("100"))
        assert (a.price, a.quantity, a.fees, a.slippage_cost) == (
            b.price,
            b.quantity,
            b.fees,
            b.slippage_cost,
        )
