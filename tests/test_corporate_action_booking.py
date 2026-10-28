"""Booking splits and dividends against the book.

Pure arithmetic: nothing here touches the broker or a warehouse. A split must
preserve position value (within a cent of rounding), keep shares whole, and
pay the fractional remainder as cash. A replayed action must change nothing.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.errors import ValidationError
from signalforge.core.types import CorporateAction, Fill, Portfolio, Side
from signalforge.paper.corporate_actions import (
    CorporateActionBooking,
    apply_corporate_action,
    dividend_cash,
)

D = Decimal
DAY = date(2020, 8, 31)
CLOSE = D("100")


def book(quantity: int, price: str = "100", cash: str = "100000") -> Portfolio:
    p = Portfolio(cash=D(cash))
    p.apply_fill(Fill("o1", "AAA", Side.BUY, quantity, D(price), D("0"), D("0"), date(2020, 8, 1)))
    return p


def split(num: int, den: int) -> CorporateAction:
    return CorporateAction("AAA", DAY, "split", "tiingo", numerator=num, denominator=den)


def dividend(cash: str) -> CorporateAction:
    return CorporateAction.dividend("AAA", DAY, D(cash), "tiingo")


class TestSplits:
    def test_two_for_one_doubles_shares_with_no_cash(self):
        p = book(100)
        cash_before = p.cash
        b = apply_corporate_action(p, split(2, 1), CLOSE)
        assert (b.shares_before, b.shares_after) == (100, 200)
        assert b.cash_delta == D("0") and b.cash_in_lieu == D("0")
        assert p.quantity_of("AAA") == 200
        assert p.cash == cash_before

    def test_three_for_two_pays_cash_in_lieu_of_the_half_share(self):
        p = book(15)
        cash_before = p.cash
        b = apply_corporate_action(p, split(3, 2), CLOSE)
        assert (b.shares_before, b.shares_after) == (15, 22)
        assert b.cash_in_lieu == D("33.33")
        assert b.cash_delta == D("33.33")
        assert b.residual == D("100") / D("3") - D("33.33")
        assert p.cash == cash_before + D("33.33")

    def test_reverse_split_leaves_a_whole_share_and_pays_the_rest(self):
        p = book(15)
        b = apply_corporate_action(p, split(1, 10), CLOSE)
        assert (b.shares_before, b.shares_after) == (15, 1)
        assert b.cash_in_lieu == D("500.00")
        assert p.quantity_of("AAA") == 1

    def test_reverse_split_rounding_to_zero_closes_the_position(self):
        p = book(5)
        cash_before = p.cash
        b = apply_corporate_action(p, split(1, 10), CLOSE)
        assert b.shares_after == 0
        assert b.cash_in_lieu == D("500.00")
        assert "AAA" not in p.positions
        assert p.cash == cash_before + D("500.00")
        assert p.realized_pnl == b.realized_pnl_delta == D("0.00")

    @pytest.mark.parametrize(
        "qty,num,den,close",
        [(15, 3, 2, "101.37"), (15, 1, 10, "7.13"), (7, 7, 1, "33.333"),
         (101, 5, 3, "12.345678"), (5, 1, 10, "10"), (1000, 1907, 2000, "55.55")],
    )
    def test_the_accounting_identity_holds_across_a_split(self, qty, num, den, close):
        p = book(qty, price="9.37")
        start = D("100000")
        pre, post = D(close), D(close) * den / num
        equity_before = p.equity({"AAA": pre})
        assert equity_before - start == p.realized_pnl + p.unrealized_pnl({"AAA": pre})
        apply_corporate_action(p, split(num, den), pre)
        marks = {"AAA": post} if "AAA" in p.positions else {}
        gap = p.equity(marks) - start - (p.realized_pnl + p.unrealized_pnl(marks))
        assert abs(gap) <= D("0.000000001")
        assert abs(p.equity(marks) - equity_before) <= D("0.005")

    def test_the_accounting_identity_holds_across_a_dividend(self):
        p = book(100, price="9.37")
        start = D("100000")
        b = apply_corporate_action(p, dividend("0.235"), CLOSE)
        marks = {"AAA": CLOSE}
        assert p.equity(marks) - start == p.realized_pnl + p.unrealized_pnl(marks)
        assert p.equity(marks) == start + 100 * (CLOSE - D("9.37")) + b.cash_delta

    def test_an_adjustment_books_like_a_split_and_is_value_preserving(self):
        p = book(1000, price="50")
        action = CorporateAction.split("AAA", DAY, D("0.9535"), "tiingo")
        before = p.equity({"AAA": D("60")})
        b = apply_corporate_action(p, action, D("60"))
        assert action.kind == "adjustment"
        assert b.shares_after == 1000 * 1907 // 2000
        assert abs(p.equity({"AAA": D("60") * 2000 / 1907}) - before) <= D("0.005")

    def test_shares_are_int_and_money_is_decimal(self):
        b = apply_corporate_action(book(15), split(3, 2), CLOSE)
        assert type(b.shares_before) is int and type(b.shares_after) is int
        for value in (b.cash_delta, b.cash_in_lieu, b.residual, b.realized_pnl_delta):
            assert isinstance(value, Decimal)

    def test_cost_basis_carries_over_pro_rata_and_the_fraction_is_realized(self):
        p = book(15, price="10")
        b = apply_corporate_action(p, split(3, 2), D("10"))
        position = p.positions["AAA"]
        assert position.quantity == 22
        assert position.cost_basis == D("150") - D("150") * 1 / 45
        assert b.cash_in_lieu == D("3.33")
        assert b.realized_pnl_delta == D("3.33") - D("150") / 45
        assert position.realized_pnl == b.realized_pnl_delta

    def test_a_closed_position_books_its_realized_pnl_to_the_book(self):
        p = book(5, price="10")
        b = apply_corporate_action(p, split(1, 10), D("10"))
        assert b.realized_pnl_delta == D("50.00") - D("50")
        assert p.realized_pnl == b.realized_pnl_delta

    def test_replay_returns_none_and_leaves_the_book_unchanged(self):
        p = book(15)
        action = split(3, 2)
        assert apply_corporate_action(p, action, CLOSE) is not None
        snapshot = (p.cash, p.quantity_of("AAA"), p.positions["AAA"].cost_basis, p.realized_pnl)
        assert apply_corporate_action(p, action, CLOSE) is None
        assert snapshot == (p.cash, p.quantity_of("AAA"), p.positions["AAA"].cost_basis, p.realized_pnl)

    def test_no_position_is_a_no_op(self):
        p = Portfolio(cash=D("1000"))
        assert apply_corporate_action(p, split(2, 1), CLOSE) is None
        assert apply_corporate_action(p, dividend("1"), CLOSE) is None
        assert p.cash == D("1000") and not p.positions

    def test_a_non_positive_prior_close_is_refused(self):
        with pytest.raises(ValidationError):
            apply_corporate_action(book(15), split(3, 2), D("0"))


class TestDividends:
    def test_a_long_holder_is_credited(self):
        p = book(100)
        cash_before = p.cash
        b = apply_corporate_action(p, dividend("0.235"), CLOSE)
        assert b.cash_delta == D("23.50")
        assert (b.shares_before, b.shares_after) == (100, 100)
        assert b.cash_in_lieu == D("0")
        assert p.cash == cash_before + D("23.50")
        assert p.realized_pnl == D("23.50") == b.realized_pnl_delta

    def test_cash_rounds_half_even_to_the_cent(self):
        assert dividend_cash(1, D("0.125")) == D("0.12")
        assert dividend_cash(1, D("0.135")) == D("0.14")

    def test_the_sign_follows_the_quantity(self):
        assert dividend_cash(-100, D("0.50")) == D("-50.00")
        assert dividend_cash(100, D("0.50")) == D("50.00")

    def test_replay_credits_once(self):
        p = book(100)
        action = dividend("0.50")
        assert apply_corporate_action(p, action, CLOSE) is not None
        cash = p.cash
        assert apply_corporate_action(p, action, CLOSE) is None
        assert p.cash == cash

    def test_the_booking_carries_the_action_id(self):
        b = apply_corporate_action(book(100), dividend("0.50"), CLOSE)
        assert isinstance(b, CorporateActionBooking)
        assert b.action_id == "AAA:2020-08-31:dividend"
        assert b.symbol == "AAA"


class TestEntitlementIsRecordedOnEvaluation:
    def test_a_dividend_evaluated_while_flat_cannot_be_claimed_later(self):
        p = Portfolio(cash=D("100000"))
        action = dividend("1.00")
        assert apply_corporate_action(p, action, CLOSE) is None
        p.apply_fill(Fill("o1", "AAA", Side.BUY, 100, D("100"), D("0"), D("0"), date(2020, 9, 1)))
        cash = p.cash
        assert apply_corporate_action(p, action, CLOSE) is None
        assert p.cash == cash


class TestUnknownShapesAreRefused:
    def test_an_action_type_outside_the_known_branches_raises(self):
        action = CorporateAction("AAA", DAY, "split", "t", numerator=2, denominator=1)
        object.__setattr__(action, "action_type", "spinoff")
        with pytest.raises(ValidationError):
            apply_corporate_action(book(10), action, CLOSE)
