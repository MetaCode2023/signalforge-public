"""Corporate actions through the paper broker and its ledger.

The booking arithmetic is pinned in test_corporate_action_booking.py. These
tests pin the wiring: when an action is applied, what the broker's marks and
breakers see afterwards, which pending orders it expires, and what the ledger
records.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.errors import ValidationError
from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.types import CorporateAction, Fill, OrderStatus, OrderType, Side
from signalforge.paper.broker import PaperBroker
from signalforge.paper.ledger import EventType

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 6)
D0, D1, D2 = DAYS[0], DAYS[1], DAYS[2]


def history(closes: dict[str, list[str]]) -> PriceHistory:
    bars = []
    for symbol, series in closes.items():
        for i, close in enumerate(series):
            bars.append(make_bar(symbol, DAYS[i], D(close), volume=10_000_000))
    return PriceHistory.from_bars(bars)


def broker(**risk) -> PaperBroker:
    research = ResearchConfig(starting_cash=D("100000"), universe=("AAPL", "MSFT"))
    return PaperBroker(research, RiskConfig(**risk))


def hold(b: PaperBroker, symbol: str, quantity: int, price: str) -> None:
    b.portfolio().apply_fill(
        Fill("seed", symbol, Side.BUY, quantity, D(price), D("0"), D("0"), DAYS[0])
    )


def day0(b: PaperBroker, h: PriceHistory):
    b.open_session(D0, h.view(D0))
    return b


def split(symbol: str, num: int, den: int, day=D1) -> CorporateAction:
    return CorporateAction(symbol, day, "split", "test", numerator=num, denominator=den)


def dividend(symbol: str, cash: str, day=D1) -> CorporateAction:
    return CorporateAction.dividend(symbol, day, D(cash), "test")


def order(b: PaperBroker, symbol: str, side: Side = Side.BUY, quantity: int = 10):
    return b.create_order(
        symbol=symbol,
        side=side,
        quantity=quantity,
        signal_date=D0,
        strategy_id="manual",
        reason="test order",
        order_type=OrderType.MARKET,
    )


def held_book(closes: dict[str, list[str]], symbol: str, quantity: int, price: str):
    h = history(closes)
    b = broker()
    hold(b, symbol, quantity, price)
    b.open_session(D0, h.view(D0))
    prior = b.close_session(D0, h.view(D0))
    return h, b, prior


class TestSplitContinuity:
    def test_four_for_one_split_does_not_trip_the_daily_loss_breaker(self):
        h, b, prior = held_book({"AAPL": ["400", "100", "100"]}, "AAPL", 100, "400")

        b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 4, 1)])

        status = b.status()
        assert b.portfolio().quantity_of("AAPL") == 400
        assert abs(status["equity"] - prior.equity) <= D("0.01")
        assert status["daily_loss"] == D("0")
        assert status["breakers"]["daily_loss_tripped"] is False
        opened = b.ledger.of_type(EventType.SESSION_OPENED)[-1]
        assert abs(opened.payload["opening_equity"] - prior.equity) <= D("0.01")

    def test_without_the_action_the_same_day_does_trip_it(self):
        h, b, prior = held_book({"AAPL": ["400", "100", "100"]}, "AAPL", 100, "400")
        b.open_session(D1, h.view(D1))
        assert b.status()["breakers"]["daily_loss_tripped"] is True

    def test_split_rescales_the_previous_mark_for_the_fill_model(self):
        h, b, _ = held_book({"AAPL": ["400", "100", "100"]}, "AAPL", 100, "400")
        b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 4, 1)])
        assert b._previous_marks["AAPL"] == D("100")

    def test_adjustment_kind_books_and_the_event_says_so(self):
        h, b, _ = held_book({"GE": ["50", "100", "100"]}, "GE", 1000, "50")
        action = CorporateAction(
            "GE", D1, "split", "test", numerator=1281, denominator=1000, kind="adjustment"
        )
        b.open_session(D1, h.view(D1), corporate_actions=[action])

        assert b.portfolio().quantity_of("GE") == 1281
        event = b.ledger.of_type(EventType.CORPORATE_ACTION)[0]
        assert event.payload["kind"] == "adjustment"
        assert event.payload["action_id"] == f"GE:{D1.isoformat()}:split"
        assert b._previous_marks["GE"] == (D("50") * 1000 / 1281).quantize(D("0.000001"))


class TestDividends:
    def test_holder_at_prior_close_is_paid_on_the_ex_date(self):
        h, b, _ = held_book({"AAPL": ["100", "99", "99"]}, "AAPL", 100, "100")
        cash = b.portfolio().cash
        b.open_session(D1, h.view(D1), corporate_actions=[dividend("AAPL", "1.00")])
        assert b.portfolio().cash == cash + D("100.00")
        event = b.ledger.of_type(EventType.CORPORATE_ACTION)[0]
        assert event.payload["kind"] == "dividend"
        assert event.payload["cash_delta"] == D("100.00")
        assert event.payload["note"] == "pay date unknown; booked on ex-date"
        assert b._previous_marks["AAPL"] == D("99")

    def test_special_dividend_is_continuous_for_equity_peak_and_breaker(self):
        h, b, prior = held_book({"AAPL": ["100", "80", "80"]}, "AAPL", 500, "100")
        b.open_session(D1, h.view(D1), corporate_actions=[dividend("AAPL", "20.00")])
        status = b.status()
        assert abs(status["daily_loss"]) <= D("0.0001")
        assert b._state.day_start_equity == prior.equity
        assert status["peak_equity"] == prior.equity
        assert b._previous_marks["AAPL"] == D("80")

    def test_dividend_that_would_zero_the_mark_is_refused(self):
        h, b, _ = held_book({"AAPL": ["100", "80", "80"]}, "AAPL", 10, "100")
        with pytest.raises(ValidationError):
            b.open_session(D1, h.view(D1), corporate_actions=[dividend("AAPL", "100.00")])
        assert b.portfolio().cash == D("99000")

    def test_buy_filled_on_the_ex_date_gets_nothing(self):
        h = history({"AAPL": ["100", "100", "100"]})
        b = broker(allow_pyramiding=True)
        hold(b, "AAPL", 10, "100")
        b.open_session(D0, h.view(D0))
        b.submit(order(b, "AAPL", Side.BUY, 10))
        b.close_session(D0, h.view(D0))
        cash = b.portfolio().cash
        b.open_session(D1, h.view(D1), corporate_actions=[dividend("AAPL", "1.00")])
        assert b.portfolio().cash == cash + D("10.00")
        b.settle(D1, h.view(D1))
        assert b.portfolio().quantity_of("AAPL") == 20
        event = b.ledger.of_type(EventType.CORPORATE_ACTION)[0]
        assert (event.payload["shares_before"], event.payload["cash_delta"]) == (10, D("10.00"))
        assert len(b.ledger.of_type(EventType.CORPORATE_ACTION)) == 1

    def test_sell_filled_on_the_ex_date_still_receives_it(self):
        h = history({"AAPL": ["100", "100", "100"]})
        b = broker()
        hold(b, "AAPL", 10, "100")
        b.open_session(D0, h.view(D0))
        b.submit(order(b, "AAPL", Side.SELL, 10))
        b.close_session(D0, h.view(D0))
        cash = b.portfolio().cash
        b.open_session(D1, h.view(D1), corporate_actions=[dividend("AAPL", "1.00")])
        assert b.portfolio().cash == cash + D("10.00")
        b.settle(D1, h.view(D1))
        assert b.portfolio().quantity_of("AAPL") == 0
        event = b.ledger.of_type(EventType.CORPORATE_ACTION)[0]
        assert event.payload["cash_delta"] == D("10.00")
        assert event.payload["shares_before"] == 10


class TestPendingOrders:
    def _setup(self):
        h = history({"AAPL": ["400", "100", "100"], "MSFT": ["100", "100", "100"]})
        b = broker()
        b.open_session(D0, h.view(D0))
        aapl = b.submit(order(b, "AAPL"))
        msft = b.submit(order(b, "MSFT"))
        b.close_session(D0, h.view(D0))
        return h, b, aapl, msft

    def test_split_expires_the_pending_order_in_that_symbol_only(self):
        h, b, aapl, msft = self._setup()
        b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 4, 1)])

        assert b.pending_count == 1
        expired = b.ledger.of_type(EventType.ORDER_EXPIRED)
        assert [e.payload["order_id"] for e in expired] == [aapl.order_id]
        assert "split" in expired[0].payload["detail"]
        filled = b.settle(D1, h.view(D1))
        assert [f.symbol for f in filled] == ["MSFT"]
        logged = {o.order_id: o.status for o in b.orders}
        assert logged[aapl.order_id] is OrderStatus.EXPIRED
        assert logged[msft.order_id] is OrderStatus.FILLED

    def test_a_dividend_does_not_expire_orders(self):
        h, b, aapl, _ = self._setup()
        b.open_session(D1, h.view(D1), corporate_actions=[dividend("AAPL", "1.00")])
        assert b.pending_count == 2
        assert b.ledger.of_type(EventType.ORDER_EXPIRED) == ()

    def test_expired_buy_releases_its_reservation(self):
        h, b, aapl, msft = self._setup()
        reserved = b._reservations.cash
        b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 4, 1)])
        assert b._reservations.quantity_for("AAPL") == 0
        assert b._reservations.quantity_for("MSFT") == 10
        assert b._reservations.cash < reserved


class TestIdempotenceAndLedger:
    def test_the_same_action_across_two_sessions_books_once(self):
        h, b, _ = held_book({"AAPL": ["400", "100", "100"]}, "AAPL", 100, "400")
        action = split("AAPL", 4, 1)
        b.open_session(D1, h.view(D1), corporate_actions=[action])
        b.close_session(D1, h.view(D1))
        b.open_session(D2, h.view(D2), corporate_actions=[action])
        assert b.portfolio().quantity_of("AAPL") == 400
        assert len(b.ledger.of_type(EventType.CORPORATE_ACTION)) == 1
        assert b._previous_marks["AAPL"] == D("100")

    def test_an_action_for_another_date_is_ignored(self):
        h, b, _ = held_book({"AAPL": ["400", "100", "100"]}, "AAPL", 100, "400")
        b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 4, 1, day=D2)])
        assert b.portfolio().quantity_of("AAPL") == 100

    def test_corporate_action_event_carries_the_full_payload_and_verifies(self):
        h, b, _ = held_book({"AAPL": ["100", "100", "100"]}, "AAPL", 15, "100")
        b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 3, 2)])

        event = b.ledger.of_type(EventType.CORPORATE_ACTION)[0]
        for key in (
            "action_id", "symbol", "kind", "shares_before", "shares_after", "cash_delta",
            "cash_in_lieu", "residual", "realized_pnl_delta",
        ):
            assert key in event.payload
        assert event.payload["kind"] == "split"
        assert (event.payload["shares_before"], event.payload["shares_after"]) == (15, 22)
        assert b.ledger.verify() is True
        assert event.compute_hash() == event.event_hash
        assert event.previous_hash == b.ledger.events[event.sequence - 2].event_hash

    def test_flat_book_still_records_a_zero_event_and_consumes_the_action(self):
        h = history({"AAPL": ["100", "100", "100"]})
        b = broker()
        b.open_session(D0, h.view(D0))
        b.close_session(D0, h.view(D0))
        action = split("AAPL", 2, 1)
        b.open_session(D1, h.view(D1), corporate_actions=[action])
        event = b.ledger.of_type(EventType.CORPORATE_ACTION)[0]
        assert (event.payload["shares_before"], event.payload["shares_after"]) == (0, 0)
        assert event.payload["cash_delta"] == D("0")
        assert action.action_id in b.portfolio()._applied_action_ids

    def test_corporate_action_event_precedes_the_expiry_it_causes(self):
        h = history({"AAPL": ["400", "100", "100"]})
        b = broker()
        b.open_session(D0, h.view(D0))
        b.submit(order(b, "AAPL"))
        b.close_session(D0, h.view(D0))
        b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 4, 1)])
        types = [e.event_type for e in b.ledger.events]
        assert types.index(EventType.CORPORATE_ACTION) < types.index(EventType.ORDER_EXPIRED)

    def test_restarted_broker_falls_back_to_the_views_prior_bar(self):
        h = history({"AAPL": ["400", "100", "100"]})
        b = broker()
        hold(b, "AAPL", 100, "400")
        assert b._previous_marks == {}
        b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 4, 1)])
        assert b.portfolio().quantity_of("AAPL") == 400
        assert len(b.ledger.of_type(EventType.CORPORATE_ACTION)) == 1

    def test_held_position_with_no_prior_bar_anywhere_fails_closed(self):
        h = PriceHistory.from_bars([make_bar("AAPL", D1, D("100"), volume=10_000_000)])
        b = broker()
        hold(b, "AAPL", 100, "400")
        with pytest.raises(ValidationError):
            b.open_session(D1, h.view(D1), corporate_actions=[split("AAPL", 4, 1)])
        assert b.portfolio().quantity_of("AAPL") == 100

    def test_flat_with_no_prior_bar_is_recorded_not_raised(self):
        h = PriceHistory.from_bars([make_bar("AAPL", D1, D("100"), volume=10_000_000)])
        b = broker()
        action = split("AAPL", 4, 1)
        b.open_session(D1, h.view(D1), corporate_actions=[action])
        assert action.action_id in b.portfolio()._applied_action_ids
        assert len(b.ledger.of_type(EventType.CORPORATE_ACTION)) == 1

    def test_no_actions_leaves_the_ledger_unchanged(self):
        h = history({"AAPL": ["100", "101", "102"]})
        plain, empty = broker(), broker()
        for b, kwargs in ((plain, {}), (empty, {"corporate_actions": []})):
            for day in (D0, D1):
                b.open_session(day, h.view(day), **kwargs)
                b.close_session(day, h.view(day))
        assert plain.ledger.head() == empty.ledger.head()
        assert plain.ledger.head().length == 4
