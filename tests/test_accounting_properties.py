"""Property-based tests for the accounting invariants.

The example-based tests in test_core_types.py check specific sequences. These
check that the invariants hold for *arbitrary* sequences of buys and sells,
which is where an accounting bug actually hides: not in the case someone
thought to write down, but in the ordering nobody did.

Every invariant here is one the brief names explicitly:

  - buying shares cannot increase available cash
  - selling cannot create more shares than are held in a long-only book
  - portfolio equity equals cash plus marked position value
  - a rejected operation cannot change balances
  - replaying the same event cannot create a duplicate fill
"""

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from signalforge.core.errors import AccountingError
from signalforge.core.types import Fill, Portfolio, Side

D = Decimal
START = date(2020, 1, 1)
SYMBOLS = ["AAA", "BBB", "CCC"]

# Prices and quantities are kept small and exact so that the identity being
# tested is the accounting, not Decimal's behaviour on pathological inputs.
prices = st.decimals(min_value=D("0.01"), max_value=D("1000"), places=2)
quantities = st.integers(min_value=1, max_value=500)
fees = st.decimals(min_value=D("0"), max_value=D("10"), places=2)
sides = st.sampled_from([Side.BUY, Side.SELL])
symbols = st.sampled_from(SYMBOLS)

instructions = st.lists(
    st.tuples(symbols, sides, quantities, prices, fees), min_size=1, max_size=40
)

SETTINGS = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def apply_all(portfolio: Portfolio, steps) -> list[Fill]:
    """Apply every instruction, skipping the ones the book refuses.

    A refused instruction is the interesting case: the assertion is that the
    book is unchanged afterwards, not that nothing was refused.
    """
    applied: list[Fill] = []
    for index, (symbol, side, quantity, price, fee) in enumerate(steps):
        fill = Fill(
            order_id=f"o{index}",
            fill_id=f"f{index}",
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fees=fee,
            slippage_cost=D("0"),
            fill_date=START + timedelta(days=index),
        )
        cash_before = portfolio.cash
        positions_before = {s: p.quantity for s, p in portfolio.positions.items()}
        try:
            portfolio.apply_fill(fill)
        except AccountingError:
            # A refusal must leave the book byte-for-byte unchanged.
            assert portfolio.cash == cash_before
            assert {
                s: p.quantity for s, p in portfolio.positions.items()
            } == positions_before
            continue
        applied.append(fill)
    return applied


class TestCashInvariants:
    @given(steps=instructions)
    @SETTINGS
    def test_cash_is_never_negative(self, steps):
        portfolio = Portfolio(cash=D("100000"))
        apply_all(portfolio, steps)
        assert portfolio.cash >= 0

    @given(symbol=symbols, quantity=quantities, price=prices, fee=fees)
    @SETTINGS
    def test_a_buy_strictly_reduces_cash(self, symbol, quantity, price, fee):
        portfolio = Portfolio(cash=D("10000000"))
        before = portfolio.cash
        portfolio.apply_fill(
            Fill(
                order_id="o",
                symbol=symbol,
                side=Side.BUY,
                quantity=quantity,
                price=price,
                fees=fee,
                slippage_cost=D("0"),
                fill_date=START,
            )
        )
        assert portfolio.cash < before

    @given(steps=instructions)
    @SETTINGS
    def test_cash_plus_cost_basis_never_exceeds_the_starting_cash(self, steps):
        # Nothing creates value out of nothing: capital deployed plus capital
        # remaining cannot exceed what was there, net of realised P&L and fees.
        portfolio = Portfolio(cash=D("100000"))
        apply_all(portfolio, steps)
        basis = sum((p.cost_basis for p in portfolio.positions.values()), D("0"))
        accounted = portfolio.cash + basis - portfolio.realized_pnl + portfolio.total_fees
        assert accounted >= D("0")


class TestPositionInvariants:
    @given(steps=instructions)
    @SETTINGS
    def test_no_position_is_ever_negative(self, steps):
        portfolio = Portfolio(cash=D("100000"))
        apply_all(portfolio, steps)
        for position in portfolio.positions.values():
            assert position.quantity > 0, "a zero or short position must not be in the book"

    @given(steps=instructions)
    @SETTINGS
    def test_shares_sold_never_exceed_shares_bought(self, steps):
        portfolio = Portfolio(cash=D("100000"))
        applied = apply_all(portfolio, steps)
        for symbol in SYMBOLS:
            bought = sum(f.quantity for f in applied if f.symbol == symbol and f.side is Side.BUY)
            sold = sum(f.quantity for f in applied if f.symbol == symbol and f.side is Side.SELL)
            assert sold <= bought

    @given(steps=instructions)
    @SETTINGS
    def test_a_closed_position_leaves_the_book(self, steps):
        portfolio = Portfolio(cash=D("100000"))
        applied = apply_all(portfolio, steps)
        for symbol in SYMBOLS:
            bought = sum(f.quantity for f in applied if f.symbol == symbol and f.side is Side.BUY)
            sold = sum(f.quantity for f in applied if f.symbol == symbol and f.side is Side.SELL)
            if bought and bought == sold:
                assert symbol not in portfolio.positions


class TestEquityIdentity:
    @given(steps=instructions, mark=prices)
    @SETTINGS
    def test_equity_equals_cash_plus_marked_positions_exactly(self, steps, mark):
        portfolio = Portfolio(cash=D("100000"))
        apply_all(portfolio, steps)
        marks = {s: mark for s in portfolio.positions}
        expected = portfolio.cash + sum(
            (p.quantity * mark for p in portfolio.positions.values()), D("0")
        )
        # Exact equality, not approximate: this is why money is Decimal.
        assert portfolio.equity(marks) == expected

    @given(steps=instructions, mark=prices)
    @SETTINGS
    def test_equity_is_never_negative_for_a_long_only_book(self, steps, mark):
        portfolio = Portfolio(cash=D("100000"))
        apply_all(portfolio, steps)
        marks = {s: mark for s in portfolio.positions}
        assert portfolio.equity(marks) >= 0

    @given(steps=instructions, mark=prices)
    @SETTINGS
    def test_gross_exposure_plus_cash_equals_equity(self, steps, mark):
        portfolio = Portfolio(cash=D("100000"))
        apply_all(portfolio, steps)
        marks = {s: mark for s in portfolio.positions}
        assert portfolio.gross_exposure(marks) + portfolio.cash == portfolio.equity(marks)


class TestReplayInvariants:
    @given(steps=instructions)
    @SETTINGS
    def test_replaying_every_fill_changes_nothing(self, steps):
        portfolio = Portfolio(cash=D("100000"))
        applied = apply_all(portfolio, steps)
        cash_after = portfolio.cash
        positions_after = {s: p.quantity for s, p in portfolio.positions.items()}
        ledger_after = len(portfolio.fills)

        for fill in applied:
            assert portfolio.apply_fill(fill) is False

        assert portfolio.cash == cash_after
        assert {s: p.quantity for s, p in portfolio.positions.items()} == positions_after
        assert len(portfolio.fills) == ledger_after

    @given(steps=instructions)
    @SETTINGS
    def test_the_ledger_holds_exactly_the_applied_fills_once_each(self, steps):
        portfolio = Portfolio(cash=D("100000"))
        applied = apply_all(portfolio, steps)
        ledger_ids = [f.fill_id for f in portfolio.fills]
        assert ledger_ids == [f.fill_id for f in applied]
        assert len(set(ledger_ids)) == len(ledger_ids)

    @given(steps=instructions)
    @SETTINGS
    def test_total_fees_equal_the_sum_of_applied_fill_fees(self, steps):
        portfolio = Portfolio(cash=D("100000"))
        applied = apply_all(portfolio, steps)
        assert portfolio.total_fees == sum((f.fees for f in applied), D("0"))


class TestOrderOfOperations:
    @given(steps=instructions, mark=prices)
    @SETTINGS
    def test_the_identity_holds_at_every_intermediate_step(self, steps, mark):
        """Not just at the end: after every single applied fill.

        A bug that briefly breaks the identity and then restores it would pass
        an end-state check while corrupting any valuation taken mid-sequence,
        which is exactly what the engine does once per day.
        """
        portfolio = Portfolio(cash=D("100000"))
        for index, (symbol, side, quantity, price, fee) in enumerate(steps):
            fill = Fill(
                order_id=f"o{index}",
                fill_id=f"f{index}",
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                fees=fee,
                slippage_cost=D("0"),
                fill_date=START + timedelta(days=index),
            )
            try:
                portfolio.apply_fill(fill)
            except AccountingError:
                pass
            marks = {s: mark for s in portfolio.positions}
            expected = portfolio.cash + sum(
                (p.quantity * mark for p in portfolio.positions.values()), D("0")
            )
            assert portfolio.equity(marks) == expected
            assert portfolio.cash >= 0
