"""Core domain types: bars, orders, fills, positions, portfolio accounting."""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.errors import AccountingError, ValidationError
from signalforge.core.types import (
    Bar,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Position,
    Side,
)

D = Decimal


def bar(**kw):
    base = dict(
        symbol="AAPL",
        trade_date=date(2016, 1, 4),
        open=D("100"),
        high=D("105"),
        low=D("99"),
        close=D("104"),
        adj_close=D("104"),
        volume=1_000_000,
    )
    base.update(kw)
    return Bar(**base)


def mk_order(**kw):
    base = dict(
        order_id="o1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        signal_date=date(2016, 1, 4),
        strategy_id="s",
        reason="r",
    )
    base.update(kw)
    return Order(**base)


def mk_fill(**kw):
    base = dict(
        order_id="o1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=10,
        price=D("100"),
        fees=D("0"),
        slippage_cost=D("0"),
        fill_date=date(2016, 1, 5),
    )
    base.update(kw)
    return Fill(**base)


class TestBar:
    def test_accepts_a_consistent_bar(self):
        b = bar()
        assert b.symbol == "AAPL"
        assert b.close == D("104")

    def test_rejects_high_below_low(self):
        with pytest.raises(ValidationError):
            bar(high=D("98"), low=D("99"))

    def test_rejects_close_above_high(self):
        with pytest.raises(ValidationError):
            bar(close=D("200"))

    def test_rejects_close_below_low(self):
        with pytest.raises(ValidationError):
            bar(close=D("1"))

    def test_rejects_open_outside_high_low(self):
        with pytest.raises(ValidationError):
            bar(open=D("200"))

    def test_rejects_negative_price(self):
        with pytest.raises(ValidationError):
            bar(low=D("-1"), open=D("-1"), close=D("-1"), high=D("-1"))

    def test_rejects_negative_volume(self):
        with pytest.raises(ValidationError):
            bar(volume=-5)

    def test_allows_zero_volume(self):
        # A halted or untraded name is a real condition, not a malformed bar.
        assert bar(volume=0).volume == 0

    def test_is_immutable(self):
        b = bar()
        with pytest.raises(Exception):
            b.close = D("1")


class TestOrder:
    def test_rejects_non_positive_quantity(self):
        with pytest.raises(ValidationError):
            mk_order(quantity=0)

    def test_rejects_negative_quantity(self):
        with pytest.raises(ValidationError):
            mk_order(quantity=-5)

    def test_rejects_fractional_quantity(self):
        with pytest.raises(ValidationError):
            mk_order(quantity=1.5)

    def test_limit_order_requires_a_limit_price(self):
        with pytest.raises(ValidationError):
            mk_order(order_type=OrderType.LIMIT)

    def test_market_order_must_not_carry_a_limit_price(self):
        with pytest.raises(ValidationError):
            mk_order(limit_price=D("100"))

    def test_new_order_starts_in_new_status(self):
        assert mk_order().status is OrderStatus.NEW

    def test_requires_a_reason(self):
        # Every order carries why it exists; the trade ledger depends on it.
        with pytest.raises(ValidationError):
            mk_order(reason="")

    def test_requires_a_strategy_id(self):
        with pytest.raises(ValidationError):
            mk_order(strategy_id="")


class TestPosition:
    def test_opening_a_long_sets_average_cost(self):
        p = Position(symbol="AAPL")
        p.apply_buy(quantity=10, price=D("100"), fees=D("1"))
        assert p.quantity == 10
        # Fees are capitalised into cost basis: 1001 / 10
        assert p.average_cost == D("100.1")
        assert p.realized_pnl == D("0")

    def test_adding_to_a_long_blends_average_cost(self):
        p = Position(symbol="AAPL")
        p.apply_buy(quantity=10, price=D("100"), fees=D("0"))
        p.apply_buy(quantity=10, price=D("120"), fees=D("0"))
        assert p.quantity == 20
        assert p.average_cost == D("110")

    def test_selling_realizes_pnl_against_average_cost(self):
        p = Position(symbol="AAPL")
        p.apply_buy(quantity=10, price=D("100"), fees=D("0"))
        p.apply_sell(quantity=4, price=D("150"), fees=D("0"))
        assert p.quantity == 6
        assert p.realized_pnl == D("200")
        assert p.average_cost == D("100")

    def test_sell_fees_reduce_realized_pnl(self):
        p = Position(symbol="AAPL")
        p.apply_buy(quantity=10, price=D("100"), fees=D("0"))
        p.apply_sell(quantity=10, price=D("110"), fees=D("5"))
        assert p.realized_pnl == D("95")

    def test_selling_more_than_held_is_an_accounting_error(self):
        p = Position(symbol="AAPL")
        p.apply_buy(quantity=10, price=D("100"), fees=D("0"))
        with pytest.raises(AccountingError):
            p.apply_sell(quantity=11, price=D("100"), fees=D("0"))

    def test_shorting_from_flat_is_an_accounting_error(self):
        p = Position(symbol="AAPL")
        with pytest.raises(AccountingError):
            p.apply_sell(quantity=1, price=D("100"), fees=D("0"))

    def test_closing_fully_leaves_zero_quantity_and_zero_cost(self):
        p = Position(symbol="AAPL")
        p.apply_buy(quantity=10, price=D("100"), fees=D("0"))
        p.apply_sell(quantity=10, price=D("110"), fees=D("0"))
        assert p.quantity == 0
        assert p.average_cost == D("0")
        assert p.realized_pnl == D("100")


class TestPortfolio:
    def test_starts_with_configured_cash_and_no_positions(self):
        pf = Portfolio(cash=D("100000"))
        assert pf.cash == D("100000")
        assert pf.positions == {}
        assert pf.equity({}) == D("100000")

    def test_equity_equals_cash_plus_marked_positions(self):
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(mk_fill(quantity=100, price=D("100"), fees=D("1")))
        assert pf.cash == D("89999")
        assert pf.equity({"AAPL": D("100")}) == D("99999")
        assert pf.equity({"AAPL": D("110")}) == D("100999")

    def test_buying_can_never_increase_cash(self):
        pf = Portfolio(cash=D("100000"))
        before = pf.cash
        pf.apply_fill(mk_fill(quantity=1))
        assert pf.cash < before

    def test_selling_more_than_held_is_refused_and_leaves_state_untouched(self):
        pf = Portfolio(cash=D("100000"))
        snapshot = pf.cash
        with pytest.raises(AccountingError):
            pf.apply_fill(mk_fill(side=Side.SELL, quantity=1))
        assert pf.cash == snapshot
        assert "AAPL" not in pf.positions

    def test_replaying_the_same_fill_is_idempotent(self):
        pf = Portfolio(cash=D("100000"))
        f = mk_fill()
        pf.apply_fill(f)
        cash_after_first = pf.cash
        pf.apply_fill(f)
        assert pf.cash == cash_after_first
        assert pf.positions["AAPL"].quantity == 10

    def test_two_distinct_fills_of_the_same_order_both_apply(self):
        # Partial fills share an order_id, so idempotency keys on fill_id.
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(mk_fill(fill_id="f1", quantity=5))
        pf.apply_fill(mk_fill(fill_id="f2", quantity=5))
        assert pf.positions["AAPL"].quantity == 10

    def test_insufficient_cash_is_an_accounting_error(self):
        pf = Portfolio(cash=D("100"))
        with pytest.raises(AccountingError):
            pf.apply_fill(mk_fill(quantity=10, price=D("100")))
        assert pf.cash == D("100")

    def test_closed_position_is_dropped_from_the_book(self):
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(mk_fill(order_id="o1", fill_id="f1", quantity=10, price=D("100")))
        pf.apply_fill(
            mk_fill(
                order_id="o2",
                fill_id="f2",
                side=Side.SELL,
                quantity=10,
                price=D("110"),
                fill_date=date(2016, 1, 6),
            )
        )
        assert "AAPL" not in pf.positions
        assert pf.cash == D("100100")
        assert pf.realized_pnl == D("100")

    def test_equity_refuses_to_mark_a_position_with_no_price(self):
        # A missing mark must fail loudly, never silently value the book at zero.
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(mk_fill())
        with pytest.raises(AccountingError):
            pf.equity({})

    def test_cash_never_goes_negative_across_a_long_only_sequence(self):
        pf = Portfolio(cash=D("1000"))
        for i in range(9):
            pf.apply_fill(mk_fill(fill_id=f"f{i}", quantity=1, price=D("100")))
        assert pf.cash == D("100")
        with pytest.raises(AccountingError):
            pf.apply_fill(mk_fill(fill_id="f99", quantity=2, price=D("100")))
        assert pf.cash == D("100")

    def test_fills_are_recorded_in_an_append_only_ledger(self):
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(mk_fill(fill_id="f1", quantity=1))
        pf.apply_fill(mk_fill(fill_id="f2", quantity=1))
        pf.apply_fill(mk_fill(fill_id="f1", quantity=1))  # replay, ignored
        assert [f.fill_id for f in pf.fills] == ["f1", "f2"]

    def test_total_fees_and_slippage_are_tracked(self):
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(mk_fill(fill_id="f1", fees=D("1"), slippage_cost=D("2")))
        pf.apply_fill(mk_fill(fill_id="f2", fees=D("3"), slippage_cost=D("4")))
        assert pf.total_fees == D("4")
        assert pf.total_slippage_cost == D("6")
