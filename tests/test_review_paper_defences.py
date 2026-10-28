"""The paper broker must have the same two defences as the backtest engine.

Found by code review, with measured reproduction. The backtest engine gained
fill-time reconciliation and reservations during the gauntlet; the paper
broker never did, and the two defects compound:

  1. `submit()` passed no `Reservations`, so every cap was computed against
     FILLED positions only. Measured: two orders each granted the full
     single-name cap, committing 1.799x equity, and `cash_cap` approving
     $179,900 of orders against $100,000 of cash with no margin. This is
     verbatim what the Reservations docstring calls "worse than having no
     cap, because the log still says every order was approved".

  2. That over-commitment then makes `apply_fill` raise at settle time, and
     `settle()` had no reconciliation and no try/finally — so the exception
     propagated, `self._pending` was never reassigned, and already-applied
     orders stayed queued. On the next session they re-filled. Because
     `fill_id` embeds the fill DATE, the portfolio's replay protection does
     not recognise them: one accepted order of 100 shares became a 200-share
     position. The offending order also never left the queue, so the broker
     re-raised every session forever.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.types import OrderStatus, OrderType, Side
from signalforge.paper.broker import PaperBroker
from signalforge.paper.ledger import EventType

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 40)


def history(price: str = "100", symbols=("AAA", "BBB")) -> PriceHistory:
    bars = []
    for symbol in symbols:
        bars.extend(make_bar(symbol, d, D(price), volume=10_000_000) for d in DAYS)
    return PriceHistory.from_bars(bars)


def broker(cash="100000", execution=None, **risk) -> PaperBroker:
    return PaperBroker(
        ResearchConfig(
            starting_cash=D(cash),
            universe=("AAA", "BBB"),
            execution=ExecutionConfig(**(execution or {})),
        ),
        RiskConfig(**risk),
    )


def order(b, day, symbol="AAA", quantity=10, side=Side.BUY, strategy="s1", reason="r"):
    return b.create_order(
        symbol=symbol,
        side=side,
        quantity=quantity,
        signal_date=day,
        strategy_id=strategy,
        reason=reason,
        order_type=OrderType.MARKET,
    )


class TestReservationsAreEnforced:
    def test_two_strategies_cannot_each_take_the_full_single_name_cap(self):
        b = broker(max_single_name_exposure=D("1.0"), max_positions=5)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        first = b.submit(order(b, day, quantity=900, strategy="s1"))
        second = b.submit(order(b, day, quantity=899, strategy="s2"))
        committed = sum(
            o.quantity * 100 for o in (first, second) if o.status is OrderStatus.ACCEPTED
        )
        assert committed <= 100_000, (
            f"committed {committed} against 100,000 of cash with no margin; "
            "the second order did not see the first one's commitment"
        )

    def test_the_second_order_is_reduced_or_rejected_not_fully_approved(self):
        b = broker(max_single_name_exposure=D("1.0"), max_positions=5)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        b.submit(order(b, day, quantity=900, strategy="s1"))
        second = b.submit(order(b, day, quantity=899, strategy="s2"))
        assert second.status is OrderStatus.REJECTED or second.quantity < 899

    def test_pending_orders_consume_the_position_count_cap(self):
        b = broker(max_positions=1)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        first = b.submit(order(b, day, symbol="AAA", quantity=1))
        second = b.submit(order(b, day, symbol="BBB", quantity=1))
        assert first.status is OrderStatus.ACCEPTED
        assert second.status is OrderStatus.REJECTED

    def test_a_reservation_is_released_once_the_order_fills(self):
        b = broker(max_single_name_exposure=D("1.0"))
        b.open_session(DAYS[2], history().view(DAYS[2]))
        b.submit(order(b, DAYS[2], quantity=100))
        b.close_session(DAYS[2], history().view(DAYS[2]))
        b.open_session(DAYS[3], history().view(DAYS[3]))
        b.settle(DAYS[3], history().view(DAYS[3]))
        assert b.pending_count == 0
        # With the reservation released and the position real, a further order
        # is judged against the filled book, not a stale commitment.
        after = b.submit(order(b, DAYS[3], quantity=1, strategy="s2"))
        assert after.status in (OrderStatus.ACCEPTED, OrderStatus.REJECTED)

    def test_a_reservation_is_released_when_an_order_expires(self):
        b = broker(max_positions=1)
        b.open_session(DAYS[2], history().view(DAYS[2]))
        b.submit(
            b.create_order(
                symbol="AAA",
                side=Side.BUY,
                quantity=10,
                signal_date=DAYS[2],
                strategy_id="s1",
                reason="unreachable limit",
                order_type=OrderType.LIMIT,
                limit_price=D("1"),
            )
        )
        b.close_session(DAYS[2], history().view(DAYS[2]))
        b.open_session(DAYS[3], history().view(DAYS[3]))
        b.settle(DAYS[3], history().view(DAYS[3]))
        assert b.pending_count == 0
        # The expired order must not still be holding a position slot.
        fresh = b.submit(order(b, DAYS[3], symbol="BBB", quantity=1))
        assert fresh.status is OrderStatus.ACCEPTED


class TestSettleNeverDoubleApplies:
    def test_settle_does_not_raise_when_the_book_cannot_fund_an_order(self):
        b = broker(max_single_name_exposure=D("1.0"), max_positions=5)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        b.submit(order(b, day, symbol="AAA", quantity=100, strategy="s1"))
        b.submit(order(b, day, symbol="BBB", quantity=999, strategy="s2"))
        b.close_session(day, history().view(day))
        b.open_session(DAYS[3], history().view(DAYS[3]))
        b.settle(DAYS[3], history().view(DAYS[3]))  # must not raise

    def test_the_queue_is_always_drained_even_when_a_fill_is_refused(self):
        b = broker(max_single_name_exposure=D("1.0"), max_positions=5)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        b.submit(order(b, day, symbol="AAA", quantity=100, strategy="s1"))
        b.submit(order(b, day, symbol="BBB", quantity=999, strategy="s2"))
        b.close_session(day, history().view(day))
        b.open_session(DAYS[3], history().view(DAYS[3]))
        b.settle(DAYS[3], history().view(DAYS[3]))
        assert b.pending_count == 0, (
            "an order that could not be funded stayed queued; it would re-fill "
            "every session forever"
        )

    def test_one_accepted_order_never_produces_two_positions_worth_of_shares(self):
        b = broker(max_single_name_exposure=D("1.0"), max_positions=5)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        b.submit(order(b, day, symbol="AAA", quantity=100, strategy="s1"))
        b.submit(order(b, day, symbol="BBB", quantity=999, strategy="s2"))
        b.close_session(day, history().view(day))
        for d in DAYS[3:6]:
            b.open_session(d, history().view(d))
            b.settle(d, history().view(d))
            b.close_session(d, history().view(d))
        assert b.portfolio().quantity_of("AAA") <= 100, (
            f"held {b.portfolio().quantity_of('AAA')} from one 100-share order"
        )

    def test_the_over_commitment_is_refused_at_submit_and_recorded(self):
        """With reservations enforced, this is now caught EARLIER.

        The over-commitment used to pass submit and blow up at fill time.
        Reservations refuse it while it is still an order, which is the
        better outcome, so the trace to look for is a submit-time rejection
        rather than a fill-time reduction.
        """
        b = broker(max_single_name_exposure=D("1.0"), max_positions=5)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        first = b.submit(order(b, day, symbol="AAA", quantity=100, strategy="s1"))
        second = b.submit(order(b, day, symbol="BBB", quantity=999, strategy="s2"))
        # Reduced rather than rejected is the right answer: what the book can
        # fund is approved and the excess is refused.
        assert second.quantity < 999
        committed = (first.quantity + second.quantity) * 100
        assert committed <= 100_000, f"committed {committed} against 100,000 cash"
        rules = {e.rule for e in b.risk_events}
        assert "cash_available" in rules or "gross_leverage" in rules
        reductions = [
            e for e in b.ledger.events if e.event_type is EventType.ORDER_REDUCED
        ]
        assert reductions and reductions[-1].payload["rule"]

    def test_a_fill_time_reduction_is_still_recorded_when_one_occurs(self):
        # A gap up between signal and fill is the case reservations cannot
        # prevent, so the fill-time path must still leave a trace.
        bars = [make_bar("AAA", d, D("100"), volume=10_000_000) for d in DAYS[:4]]
        bars.append(make_bar("AAA", DAYS[4], D("400"), open_=D("400"), volume=10_000_000))
        bars += [make_bar("AAA", d, D("400"), volume=10_000_000) for d in DAYS[5:10]]
        hist = PriceHistory.from_bars(bars)
        b = broker(cash="10500", max_single_name_exposure=D("1.0"))
        b.open_session(DAYS[3], hist.view(DAYS[3]))
        b.submit(order(b, DAYS[3], quantity=100))
        b.close_session(DAYS[3], hist.view(DAYS[3]))
        b.open_session(DAYS[4], hist.view(DAYS[4]))
        b.settle(DAYS[4], hist.view(DAYS[4]))
        rules = {e.rule for e in b.risk_events}
        assert "cash_available_at_fill" in rules
        assert b.portfolio().cash >= 0

    def test_the_ledger_fill_count_matches_the_book(self):
        b = broker(max_single_name_exposure=D("1.0"), max_positions=5)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        b.submit(order(b, day, symbol="AAA", quantity=100, strategy="s1"))
        b.submit(order(b, day, symbol="BBB", quantity=999, strategy="s2"))
        b.close_session(day, history().view(day))
        for d in DAYS[3:6]:
            b.open_session(d, history().view(d))
            b.settle(d, history().view(d))
            b.close_session(d, history().view(d))
        ledger_fills = [e for e in b.ledger.events if e.event_type is EventType.FILL]
        assert len(ledger_fills) == len(b.portfolio().fills), (
            "the audit ledger over-reports fills relative to the book"
        )

    def test_a_sell_of_shares_already_gone_is_clamped_not_raised(self):
        b = broker(execution={"slippage_bps": D("0")})
        b.open_session(DAYS[2], history().view(DAYS[2]))
        b.submit(order(b, DAYS[2], quantity=10))
        b.close_session(DAYS[2], history().view(DAYS[2]))
        b.open_session(DAYS[3], history().view(DAYS[3]))
        b.settle(DAYS[3], history().view(DAYS[3]))
        # Two independent exits for the same position, from two strategies so
        # the duplicate-intent guard does not catch them.
        b.submit(order(b, DAYS[3], quantity=10, side=Side.SELL, strategy="s1", reason="x1"))
        b.submit(order(b, DAYS[3], quantity=10, side=Side.SELL, strategy="s2", reason="x2"))
        b.close_session(DAYS[3], history().view(DAYS[3]))
        b.open_session(DAYS[4], history().view(DAYS[4]))
        b.settle(DAYS[4], history().view(DAYS[4]))  # must not raise
        assert b.portfolio().quantity_of("AAA") == 0
        assert b.pending_count == 0


class TestBrokerAccountingStillHolds:
    def test_equity_identity_holds_through_a_contended_session(self):
        b = broker(max_single_name_exposure=D("1.0"), max_positions=5)
        day = DAYS[2]
        b.open_session(day, history().view(day))
        b.submit(order(b, day, symbol="AAA", quantity=100, strategy="s1"))
        b.submit(order(b, day, symbol="BBB", quantity=999, strategy="s2"))
        statement = b.close_session(day, history().view(day))
        assert statement.equity == statement.cash + statement.gross_exposure
        for d in DAYS[3:8]:
            b.open_session(d, history().view(d))
            b.settle(d, history().view(d))
            s = b.close_session(d, history().view(d))
            assert s.equity == s.cash + s.gross_exposure
            assert s.cash >= 0
