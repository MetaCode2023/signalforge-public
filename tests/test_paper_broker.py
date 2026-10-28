"""The paper broker, and the proof that it cannot reach a real brokerage.

TestNoLiveOrderPath is the most important class in this file. It does not
check a flag that could be flipped; it checks the structure of the shipped
package: that no module reachable from the paper execution path imports a
network library, and that a complete paper session runs with every socket
call rigged to explode.
"""

import ast
import socket
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.errors import AccountingError, LiveTradingAttempt, ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import RiskAction
from signalforge.core.types import OrderStatus, OrderType, Side
from signalforge.paper.broker import PaperBroker
from signalforge.paper.ledger import EventType, TradeLedger

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 40)
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "signalforge"


@pytest.fixture
def history() -> PriceHistory:
    return PriceHistory.from_bars(
        [make_bar("AAPL", d, D(100 + i), volume=10_000_000) for i, d in enumerate(DAYS)]
    )


def broker(**kw) -> PaperBroker:
    research = ResearchConfig(
        starting_cash=D(kw.pop("cash", "100000")),
        universe=("AAPL",),
        execution=ExecutionConfig(**kw.pop("execution", {})),
    )
    return PaperBroker(research, RiskConfig(**kw.pop("risk", {})))


def propose(b: PaperBroker, day, **kw):
    base = dict(
        symbol="AAPL",
        side=Side.BUY,
        quantity=10,
        signal_date=day,
        strategy_id="manual",
        reason="test order",
        order_type=OrderType.MARKET,
    )
    base.update(kw)
    return b.create_order(**base)


# ==========================================================================
# The safety proof
# ==========================================================================
NETWORK_MODULES = {
    "socket",
    "requests",
    "urllib",
    "urllib3",
    "http",
    "httpx",
    "aiohttp",
    "ftplib",
    "telnetlib",
    "smtplib",
    "websocket",
    "websockets",
    "paramiko",
    "boto3",
    "schwab",
    "schwabdev",
    "tda",
    "ib_insync",
    "alpaca",
    "ccxt",
}

BROKER_TOKENS = (
    "schwab",
    "place_order",
    "submit_live",
    "live_order",
    "real_money",
    "oauth_token",
    "account_number",
    "routing_number",
    "tdameritrade",
    "interactive_brokers",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def _paper_path_modules() -> list[Path]:
    """Modules reachable from the paper execution path.

    The data layer is deliberately excluded: it is allowed to fetch market
    data over the network. It is not allowed to be part of order execution,
    and `test_the_paper_path_does_not_import_the_data_layer` pins that.
    """
    roots = ["paper", "risk", "backtesting", "strategies", "core", "config"]
    out: list[Path] = []
    for root in roots:
        out.extend(sorted((PACKAGE_ROOT / root).rglob("*.py")))
    return out


class TestNoLiveOrderPath:
    def test_no_module_in_the_paper_path_imports_a_network_library(self):
        offenders = {}
        for path in _paper_path_modules():
            hits = _imported_modules(path) & NETWORK_MODULES
            if hits:
                offenders[path.relative_to(PACKAGE_ROOT).as_posix()] = sorted(hits)
        assert offenders == {}, (
            "the paper execution path must not be able to reach a network at all; "
            f"found: {offenders}"
        )

    def test_the_paper_path_does_not_import_the_data_layer(self):
        # Market data ingestion may use the network. Order execution may not,
        # so the execution path must not be able to borrow the data layer's
        # network access transitively.
        offenders = []
        for path in sorted((PACKAGE_ROOT / "paper").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "signalforge.data" in source:
                offenders.append(path.name)
        assert offenders == []

    def test_no_broker_vendor_token_appears_anywhere_in_the_package(self):
        offenders = {}
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            lowered = path.read_text(encoding="utf-8").lower()
            hits = [t for t in BROKER_TOKENS if t in lowered]
            if hits:
                offenders[path.name] = hits
        assert offenders == {}, f"live-brokerage vocabulary found: {offenders}"

    def test_the_broker_exposes_no_method_that_could_reach_a_venue(self):
        # Exact names rather than substrings: "realized_pnl" contains "real"
        # and a substring check would flag it, which would make this test
        # noise instead of evidence.
        forbidden = {
            "place_order",
            "send_order",
            "route_order",
            "transmit",
            "connect",
            "authenticate",
            "login",
            "set_credentials",
            "broker_api",
        }
        b = broker()
        attributes = {name.lower() for name in dir(b)}
        assert attributes & forbidden == set()
        assert not any("schwab" in name for name in attributes)
        assert b.is_paper is True

    def test_a_full_paper_session_runs_with_every_socket_call_rigged_to_fail(
        self, history, monkeypatch
    ):
        def explode(*args, **kwargs):
            raise AssertionError("the paper engine attempted a network connection")

        monkeypatch.setattr(socket, "socket", explode)
        monkeypatch.setattr(socket, "create_connection", explode)

        b = broker()
        for day in DAYS[:10]:
            view = history.view(day)
            b.open_session(day, view)
            if day == DAYS[2]:
                b.submit(propose(b, day))
            b.settle(day, view)
            b.close_session(day, view)
        assert b.portfolio().quantity_of("AAPL") > 0

    def test_is_paper_is_true_and_cannot_be_reassigned(self):
        b = broker()
        assert b.is_paper is True
        with pytest.raises(Exception):
            b.is_paper = False

    def test_the_guard_raises_if_an_external_route_is_ever_invoked(self):
        # A tripwire for a future contributor: if anyone wires an external
        # venue in, routing through the guard raises rather than executing.
        b = broker()
        with pytest.raises(LiveTradingAttempt):
            b._refuse_external_execution("someone wired a vendor SDK in")


# ==========================================================================
# Ordinary broker behaviour
# ==========================================================================
class TestOrderLifecycle:
    def test_an_accepted_order_is_pending_until_the_next_session(self, history):
        b = broker()
        day = DAYS[2]
        b.open_session(day, history.view(day))
        order = b.submit(propose(b, day))
        assert order.status is OrderStatus.ACCEPTED
        assert b.portfolio().quantity_of("AAPL") == 0

    def test_a_pending_order_fills_on_the_following_session(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2]))
        b.close_session(DAYS[2], history.view(DAYS[2]))
        b.open_session(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        assert b.portfolio().quantity_of("AAPL") == 10

    def test_a_risk_rejected_order_never_reaches_the_pending_queue(self, history):
        b = broker(cash="100")
        day = DAYS[2]
        b.open_session(day, history.view(day))
        order = b.submit(propose(b, day))
        assert order.status is OrderStatus.REJECTED
        assert b.pending_count == 0
        assert b.portfolio().cash == D("100")

    def test_a_reduced_order_is_queued_at_the_approved_quantity(self, history):
        b = broker()
        day = DAYS[2]
        b.open_session(day, history.view(day))
        order = b.submit(propose(b, day, quantity=10_000))
        assert order.status is OrderStatus.ACCEPTED
        assert order.quantity < 10_000

    def test_a_duplicate_order_is_refused(self, history):
        b = broker()
        day = DAYS[2]
        b.open_session(day, history.view(day))
        b.submit(propose(b, day))
        second = b.submit(propose(b, day))
        assert second.status is OrderStatus.REJECTED

    def test_an_unfilled_order_expires_rather_than_lingering(self, history):
        # A limit far below the market never fills; it must not sit in the
        # book and surprise the operator later.
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2], order_type=OrderType.LIMIT, limit_price=D("1")))
        b.close_session(DAYS[2], history.view(DAYS[2]))
        b.open_session(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        assert b.pending_count == 0
        assert b.portfolio().quantity_of("AAPL") == 0

    def test_selling_more_than_held_is_refused_by_risk_not_by_a_crash(self, history):
        b = broker()
        day = DAYS[2]
        b.open_session(day, history.view(day))
        order = b.submit(propose(b, day, side=Side.SELL, quantity=5))
        assert order.status is OrderStatus.REJECTED

    def test_an_order_for_an_unknown_symbol_is_refused(self, history):
        b = broker()
        day = DAYS[2]
        b.open_session(day, history.view(day))
        order = b.submit(propose(b, day, symbol="NOSUCH"))
        assert order.status is OrderStatus.REJECTED


class TestPause:
    def test_pausing_blocks_new_buys(self, history):
        b = broker()
        day = DAYS[2]
        b.open_session(day, history.view(day))
        b.pause("operator halted trading")
        order = b.submit(propose(b, day))
        assert order.status is OrderStatus.REJECTED

    def test_pausing_still_allows_an_exit(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2]))
        b.close_session(DAYS[2], history.view(DAYS[2]))
        b.open_session(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        b.pause("operator halted trading")
        order = b.submit(propose(b, DAYS[3], side=Side.SELL, quantity=10))
        assert order.status is OrderStatus.ACCEPTED

    def test_resuming_restores_normal_operation(self, history):
        b = broker()
        day = DAYS[2]
        b.open_session(day, history.view(day))
        b.pause("halted")
        b.resume()
        order = b.submit(propose(b, day))
        assert order.status is OrderStatus.ACCEPTED

    def test_the_pause_reason_is_recorded(self, history):
        b = broker()
        b.pause("maintenance window")
        assert "maintenance window" in b.status()["pause_reason"]


class TestLedger:
    def test_every_event_is_appended_with_a_monotonic_sequence(self):
        ledger = TradeLedger()
        ledger.append(EventType.ORDER_SUBMITTED, date(2020, 1, 1), {"order_id": "o1"})
        ledger.append(EventType.ORDER_ACCEPTED, date(2020, 1, 1), {"order_id": "o1"})
        assert [e.sequence for e in ledger.events] == [1, 2]

    def test_the_ledger_is_append_only(self):
        ledger = TradeLedger()
        ledger.append(EventType.ORDER_SUBMITTED, date(2020, 1, 1), {"order_id": "o1"})
        with pytest.raises(Exception):
            ledger.events[0] = None
        with pytest.raises(Exception):
            ledger.events.clear()

    def test_each_event_chains_to_the_previous_one(self):
        ledger = TradeLedger()
        ledger.append(EventType.ORDER_SUBMITTED, date(2020, 1, 1), {"order_id": "o1"})
        ledger.append(EventType.FILL, date(2020, 1, 2), {"order_id": "o1"})
        assert ledger.events[1].previous_hash == ledger.events[0].event_hash

    def test_tampering_is_detectable(self):
        ledger = TradeLedger()
        ledger.append(EventType.ORDER_SUBMITTED, date(2020, 1, 1), {"order_id": "o1"})
        ledger.append(EventType.FILL, date(2020, 1, 2), {"order_id": "o1"})
        assert ledger.verify() is True
        ledger._events[0] = ledger._events[0].__class__(
            sequence=1,
            event_type=EventType.ORDER_SUBMITTED,
            event_date=date(2020, 1, 1),
            payload={"order_id": "TAMPERED"},
            previous_hash=ledger._events[0].previous_hash,
            event_hash=ledger._events[0].event_hash,
        )
        assert ledger.verify() is False

    def test_an_event_payload_is_frozen_against_later_edits(self):
        ledger = TradeLedger()
        payload = {"order_id": "o1"}
        ledger.append(EventType.ORDER_SUBMITTED, date(2020, 1, 1), payload)
        payload["order_id"] = "mutated"
        assert ledger.events[0].payload["order_id"] == "o1"

    def test_the_broker_records_the_whole_lifecycle(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2]))
        b.close_session(DAYS[2], history.view(DAYS[2]))
        b.open_session(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        kinds = [e.event_type for e in b.ledger.events]
        assert EventType.SESSION_OPENED in kinds
        assert EventType.ORDER_ACCEPTED in kinds
        assert EventType.FILL in kinds

    def test_a_rejection_is_recorded_with_its_rule(self, history):
        b = broker(cash="100")
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2]))
        rejections = [e for e in b.ledger.events if e.event_type is EventType.ORDER_REJECTED]
        assert rejections
        assert rejections[0].payload["rule"]


class TestStatements:
    def test_an_end_of_day_statement_reports_real_state(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2]))
        b.close_session(DAYS[2], history.view(DAYS[2]))
        b.open_session(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        statement = b.close_session(DAYS[3], history.view(DAYS[3]))
        assert statement.statement_date == DAYS[3]
        assert statement.equity == statement.cash + statement.gross_exposure
        assert statement.positions["AAPL"].quantity == 10
        assert len(statement.fills) == 1

    def test_the_statement_is_labelled_simulated(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        statement = b.close_session(DAYS[2], history.view(DAYS[2]))
        assert statement.simulated is True
        assert "SIMULATED" in statement.render().upper()

    def test_a_statement_for_a_flat_book_still_renders(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        statement = b.close_session(DAYS[2], history.view(DAYS[2]))
        assert statement.equity == D("100000")
        assert statement.positions == {}

    def test_statements_accumulate_one_per_session(self, history):
        b = broker()
        for day in DAYS[:5]:
            b.open_session(day, history.view(day))
            b.settle(day, history.view(day))
            b.close_session(day, history.view(day))
        assert len(b.statements) == 5
        assert [s.statement_date for s in b.statements] == list(DAYS[:5])


class TestAccounting:
    def test_equity_identity_holds_after_every_session(self, history):
        b = broker()
        for i, day in enumerate(DAYS[:10]):
            view = history.view(day)
            b.open_session(day, view)
            if i == 1:
                b.submit(propose(b, day))
            b.settle(day, view)
            statement = b.close_session(day, view)
            assert statement.equity == statement.cash + statement.gross_exposure

    def test_cash_never_goes_negative(self, history):
        b = broker()
        for i, day in enumerate(DAYS[:10]):
            view = history.view(day)
            b.open_session(day, view)
            b.submit(propose(b, day, quantity=40 + i, reason=f"order {i}"))
            b.settle(day, view)
            b.close_session(day, view)
            assert b.portfolio().cash >= 0

    def test_replaying_a_settled_session_does_not_duplicate_fills(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2]))
        b.close_session(DAYS[2], history.view(DAYS[2]))
        b.open_session(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        assert b.portfolio().quantity_of("AAPL") == 10
        assert len(b.portfolio().fills) == 1

    def test_submitting_before_a_session_is_opened_is_refused(self):
        b = broker()
        with pytest.raises(ValidationError):
            b.submit(
                b.create_order(
                    symbol="AAPL",
                    side=Side.BUY,
                    quantity=1,
                    signal_date=DAYS[0],
                    strategy_id="manual",
                    reason="r",
                    order_type=OrderType.MARKET,
                )
            )

    def test_a_round_trip_realizes_profit_and_frees_the_cash(self, history):
        b = broker(execution={"slippage_bps": D("0"), "commission_per_share": D("0"), "min_commission": D("0")})
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2]))
        b.close_session(DAYS[2], history.view(DAYS[2]))
        b.open_session(DAYS[3], history.view(DAYS[3]))
        b.settle(DAYS[3], history.view(DAYS[3]))
        b.submit(propose(b, DAYS[3], side=Side.SELL, quantity=10, reason="exit"))
        b.close_session(DAYS[3], history.view(DAYS[3]))
        b.open_session(DAYS[4], history.view(DAYS[4]))
        b.settle(DAYS[4], history.view(DAYS[4]))
        assert b.portfolio().quantity_of("AAPL") == 0
        assert b.portfolio().cash > D("100000")


class TestStatus:
    def test_status_reports_the_risk_engine_state(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        status = b.status()
        assert status["is_paper"] is True
        assert status["paused"] is False
        assert "equity" in status
        assert status["breakers"]["daily_loss_tripped"] is False

    def test_status_reports_a_tripped_breaker(self, history):
        b = broker()
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b._state.mark_equity(DAYS[2], D("90000"))
        assert b.status()["breakers"]["daily_loss_tripped"] is True

    def test_risk_events_are_exposed_for_the_dashboard(self, history):
        b = broker(cash="100")
        b.open_session(DAYS[2], history.view(DAYS[2]))
        b.submit(propose(b, DAYS[2]))
        assert b.risk_events
        assert b.risk_events[0].action is RiskAction.REJECT
