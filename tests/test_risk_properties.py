"""Property-based tests for the risk engine.

`test_risk_engine.py` checks each rule against cases someone thought to write
down. These check that the engine's guarantees hold for *arbitrary* order
sequences against arbitrary books, which is where a risk bug hides: not in
the single order that was considered, but in the fourth one on the same bar
whose predecessors had not filled yet. The reservation defect that round 5
found was exactly that shape.

Every property here is a claim the system makes elsewhere, restated so an
arbitrary sequence has to satisfy it:

  - an approved quantity never exceeds the requested one
  - no sequence of approvals breaches the single-name cap
  - no sequence of approvals breaches gross leverage
  - no sequence of approvals spends cash the book does not have
  - a REJECT carries exactly zero, always
  - an exit is never blocked by a breaker, however the breaker was tripped
  - reservations net to zero once every order resolves

## Non-vacuity

A property test that never reaches the interesting branch passes for the
wrong reason, so this file proves it reaches them two ways.

`test_the_generated_sequences_exercise_every_outcome` collects the actions
and binding rules the generator actually produces and fails if any of
ACCEPT, REDUCE or REJECT is missing, or if fewer than four distinct rules
bind. It prints the census so a reader can see the distribution rather than
trust it.

`TestThePropertiesHaveTeeth` runs the same properties against a deliberately
broken engine -- one whose single-name cap is 10x too generous, one that
approves more than was asked, one that lets a REJECT carry a quantity -- and
asserts each mutation is caught. A property nothing can break is not a test.
"""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, event, given, settings
from hypothesis import strategies as st

from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import RiskAction, Signal, SignalDirection
from signalforge.core.types import Fill, Order, OrderType, Portfolio, Side
from signalforge.risk.engine import Reservations, RiskEngine
from signalforge.risk.state import RiskState

from tests.conftest import business_days, make_bar

D = Decimal
ZERO = D("0")
SYMBOLS = ("AAA", "BBB", "CCC")
DAYS = business_days(date(2020, 1, 1), 30)
TODAY = DAYS[-1]
STARTING_CASH = D("100000")

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

prices = st.decimals(min_value=D("0.50"), max_value=D("5000"), places=2)
quantities = st.integers(min_value=1, max_value=4000)
sides = st.sampled_from([Side.BUY, Side.SELL])
symbols = st.sampled_from(SYMBOLS)

# `settle` decides whether an approved order fills straight away or stays
# committed but unfilled. Leaving some unfilled is the whole point: a cap
# computed against filled positions only is the defect this guards.
instructions = st.lists(
    st.tuples(symbols, sides, quantities, st.booleans()), min_size=1, max_size=25
)


@st.composite
def books(draw):
    """A price history plus an opening position in one or more symbols."""
    levels = {symbol: draw(prices) for symbol in SYMBOLS}
    bars = []
    for symbol, level in levels.items():
        for index, day in enumerate(DAYS):
            # A mild drift so `returns` is non-degenerate and the risk-budget
            # cap has a volatility to work with. A flat series makes that
            # rule return None, which would quietly remove it from the test.
            step = D(1) + (D(index % 5) - D(2)) / D(100)
            bars.append(make_bar(symbol, day, (level * step).quantize(D("0.01")),
                                 volume=1_000_000, source="property"))
    history = PriceHistory.from_bars(bars)
    openings = draw(
        st.lists(st.tuples(symbols, st.integers(min_value=1, max_value=200)), max_size=3)
    )
    return history, openings


def execution_config() -> ExecutionConfig:
    return ResearchConfig(starting_cash=STARTING_CASH, universe=SYMBOLS).execution


class Harness:
    """Drives the risk engine the way the backtest engine drives it.

    Same call order, same bookkeeping: evaluate, record the submission with
    the REQUESTED quantity, then either fill the approved order or leave it
    reserved. Deviating from that order would test a different system.
    """

    def __init__(self, history, openings, risk_config=None, engine=None):
        self.history = history
        self.view = history.view(TODAY)
        self.marks = self.view.marks()
        self.config = risk_config or RiskConfig()
        self.execution = execution_config()
        self.engine = engine or RiskEngine(self.config, self.execution)
        self.portfolio = Portfolio(cash=STARTING_CASH)
        self.state = RiskState()
        self.reservations = Reservations()
        self.sequence = 0
        self.decisions = []
        self.open_orders = {}
        for symbol, quantity in openings:
            self._seed_position(symbol, quantity)
        self.state.open_day(TODAY, self.equity())

    def _seed_position(self, symbol, quantity):
        price = self.marks[symbol]
        cost = price * quantity
        if cost + D("1.00") > self.portfolio.cash:
            return
        self.sequence += 1
        self.portfolio.apply_fill(
            Fill(order_id=f"seed{self.sequence}", symbol=symbol, side=Side.BUY,
                 quantity=quantity, price=price, fees=D("1.00"), slippage_cost=ZERO,
                 fill_date=DAYS[0]))

    def equity(self):
        return self.portfolio.equity(
            {s: self.marks[s] for s in self.portfolio.positions}
        )

    def gross(self):
        return self.portfolio.gross_exposure(
            {s: self.marks[s] for s in self.portfolio.positions}
        )

    def order(self, symbol, side, quantity):
        self.sequence += 1
        return Order(
            order_id=f"o{self.sequence}", symbol=symbol, side=side, quantity=quantity,
            order_type=OrderType.MARKET, signal_date=TODAY,
            strategy_id="property", reason="property test",
        )

    def submit(self, symbol, side, quantity, settle):
        order = self.order(symbol, side, quantity)
        decision = self.engine.evaluate(
            order, self.portfolio, self.view, self.state, self.reservations
        )
        self.decisions.append((order, decision))
        if decision.action is RiskAction.REJECT:
            return order, decision
        approved = decision.approved_quantity
        self.state.record_submission(order, requested_quantity=order.quantity)
        if side is Side.BUY:
            self.reservations.add(
                order.order_id, symbol, approved, self.marks[symbol] * approved
            )
            self.open_orders[order.order_id] = (symbol, side, approved)
            if settle:
                self.resolve(order.order_id, fill=True)
        else:
            self.open_orders[order.order_id] = (symbol, side, approved)
            if settle:
                self.resolve(order.order_id, fill=True)
        return order, decision

    def resolve(self, order_id, *, fill):
        """Fill or expire a pending order, releasing what it reserved."""
        symbol, side, quantity = self.open_orders.pop(order_id)
        self.reservations.release(order_id)
        if not fill:
            return
        price = self.marks[symbol]
        fee = max(self.execution.min_commission,
                  self.execution.commission_per_share * quantity)
        try:
            self.portfolio.apply_fill(
                Fill(order_id=order_id, symbol=symbol, side=side, quantity=quantity,
                     price=price, fees=fee, slippage_cost=ZERO, fill_date=TODAY))
        except Exception:
            # A fee the cap did not model can make the fill unaffordable. The
            # cap's own guarantee is checked separately; swallowing this here
            # keeps the sequence running so later orders are still evaluated.
            pass

    def drain(self):
        for order_id in list(self.open_orders):
            self.resolve(order_id, fill=False)


def run_sequence(history, openings, steps, **kw) -> Harness:
    harness = Harness(history, openings, **kw)
    for symbol, side, quantity, settle in steps:
        harness.submit(symbol, side, quantity, settle)
    return harness


# --------------------------------------------------------------------------
# The properties
# --------------------------------------------------------------------------
class TestApprovalIsBoundedByTheRequest:
    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_an_approved_quantity_never_exceeds_the_requested_one(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        for order, decision in harness.decisions:
            assert decision.approved_quantity <= order.quantity
            assert decision.approved_quantity >= 0

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_a_reject_carries_exactly_zero(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        for _, decision in harness.decisions:
            if decision.action is RiskAction.REJECT:
                assert decision.approved_quantity == 0

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_a_reduce_is_strictly_between_zero_and_the_request(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        for order, decision in harness.decisions:
            if decision.action is RiskAction.REDUCE:
                assert 0 < decision.approved_quantity < order.quantity

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_an_accept_approves_the_whole_request(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        for order, decision in harness.decisions:
            if decision.action is RiskAction.ACCEPT:
                assert decision.approved_quantity == order.quantity

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_every_decision_names_a_rule(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        for _, decision in harness.decisions:
            assert decision.rule
            assert decision.reason

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_every_non_accept_is_logged_exactly_once(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        non_accepts = [d for _, d in harness.decisions if d.action is not RiskAction.ACCEPT]
        assert len(harness.engine.events) == len(non_accepts)
        for event_record, decision in zip(harness.engine.events, non_accepts):
            assert event_record.rule == decision.rule
            assert event_record.action is decision.action
            assert event_record.approved_quantity == decision.approved_quantity


class TestNoSequenceBreachesACap:
    """The caps are checked at the moment of approval, against the book and
    the commitments as they stood then. Checking only the end state would
    miss a breach that a later sell unwound.
    """

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_the_single_name_cap_holds_across_held_plus_committed(self, book, steps):
        history, openings = book
        harness = Harness(history, openings)
        for symbol, side, quantity, settle in steps:
            held_before = harness.portfolio.quantity_of(symbol)
            reserved_before = harness.reservations.quantity_for(symbol)
            equity_before = harness.equity()
            _, decision = harness.submit(symbol, side, quantity, settle)
            if side is not Side.BUY or decision.action is RiskAction.REJECT:
                continue
            price = harness.marks[symbol]
            committed = held_before + reserved_before + decision.approved_quantity
            assert committed * price <= equity_before * harness.config.max_single_name_exposure

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_gross_leverage_holds_across_held_plus_committed(self, book, steps):
        history, openings = book
        harness = Harness(history, openings)
        for symbol, side, quantity, settle in steps:
            gross_before = harness.gross()
            reserved_cash_before = harness.reservations.cash
            equity_before = harness.equity()
            _, decision = harness.submit(symbol, side, quantity, settle)
            if side is not Side.BUY or decision.action is RiskAction.REJECT:
                continue
            price = harness.marks[symbol]
            exposure = (
                gross_before
                + reserved_cash_before
                + decision.approved_quantity * price
            )
            assert exposure <= equity_before * harness.config.max_gross_leverage

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_an_approved_buy_is_payable_from_uncommitted_cash(self, book, steps):
        history, openings = book
        harness = Harness(history, openings)
        for symbol, side, quantity, settle in steps:
            available_before = harness.portfolio.cash - harness.reservations.cash
            _, decision = harness.submit(symbol, side, quantity, settle)
            if side is not Side.BUY or decision.action is RiskAction.REJECT:
                continue
            price = harness.marks[symbol]
            approved = decision.approved_quantity
            fee = max(harness.execution.min_commission,
                      harness.execution.commission_per_share * approved)
            assert price * approved + fee <= available_before

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_cash_never_goes_negative_through_the_whole_sequence(self, book, steps):
        history, openings = book
        harness = Harness(history, openings)
        for symbol, side, quantity, settle in steps:
            harness.submit(symbol, side, quantity, settle)
            assert harness.portfolio.cash >= ZERO
        harness.drain()
        assert harness.portfolio.cash >= ZERO

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_a_sell_never_exceeds_what_is_held(self, book, steps):
        history, openings = book
        harness = Harness(history, openings)
        for symbol, side, quantity, settle in steps:
            held_before = harness.portfolio.quantity_of(symbol)
            _, decision = harness.submit(symbol, side, quantity, settle)
            if side is not Side.SELL:
                continue
            assert decision.approved_quantity <= held_before

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_the_position_count_never_exceeds_the_limit(self, book, steps):
        """Run with max_positions=2 against three symbols, deliberately.

        The production limit is 20 and the generator only has three symbols,
        so asserting against the default could never fail however broken the
        rule was. Two is the tightest limit the sequence can actually reach.
        """
        history, _ = book
        harness = Harness(history, [], risk_config=RiskConfig(max_positions=2))
        for symbol, side, quantity, settle in steps:
            harness.submit(symbol, side, quantity, settle)
            committed = set(harness.portfolio.positions) | harness.reservations.symbols
            assert len(committed) <= 2, sorted(committed)


class TestReservationsNetToZero:
    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_every_resolved_order_releases_what_it_committed(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        harness.drain()
        assert harness.reservations.cash == ZERO
        assert harness.reservations.quantities == {}
        assert harness.reservations.symbols == set()

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_filling_instead_of_expiring_also_nets_to_zero(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        for order_id in list(harness.open_orders):
            harness.resolve(order_id, fill=True)
        assert harness.reservations.cash == ZERO

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_committed_cash_never_exceeds_cash_on_hand(self, book, steps):
        history, openings = book
        harness = Harness(history, openings)
        for symbol, side, quantity, settle in steps:
            harness.submit(symbol, side, quantity, settle)
            assert harness.reservations.cash <= harness.portfolio.cash

    @given(book=books(), steps=instructions)
    @SETTINGS
    def test_releasing_an_unknown_order_is_a_no_op(self, book, steps):
        history, openings = book
        harness = run_sequence(history, openings, steps)
        before = harness.reservations.cash
        harness.reservations.release("never-existed")
        assert harness.reservations.cash == before


class TestAnExitIsNeverBlocked:
    """The asymmetry is deliberate and is the single most important one.

    A halted system that cannot reduce an existing position is more dangerous
    than one that keeps trading, so every breaker applies to risk-increasing
    orders only. Tested against arbitrary tripped states rather than the one
    or two a hand-written test would pick.
    """

    @given(
        held=st.integers(min_value=1, max_value=500),
        requested=quantities,
        price=prices,
        paused=st.booleans(),
        drawdown_tripped=st.booleans(),
        daily_loss_tripped=st.booleans(),
    )
    @SETTINGS
    def test_a_sell_survives_every_combination_of_tripped_breakers(
        self, held, requested, price, paused, drawdown_tripped, daily_loss_tripped
    ):
        config = RiskConfig()
        bars = [
            make_bar("AAA", day, price, volume=1_000_000, source="property")
            for day in DAYS
        ]
        history = PriceHistory.from_bars(bars)
        view = history.view(TODAY)
        # Funded from the drawn held quantity and price, not a fixed figure:
        # a flat 1,000,000 could not pay for 385 shares at 2,597.41 and the
        # seeding raised an AccountingError before the property ran.
        portfolio = Portfolio(cash=held * price + D("10000"))
        portfolio.apply_fill(
            Fill(order_id="seed", symbol="AAA", side=Side.BUY, quantity=held,
                 price=price, fees=D("1.00"), slippage_cost=ZERO, fill_date=DAYS[0]))
        state = RiskState()
        state.open_day(TODAY, portfolio.cash + held * price)
        state.paused = paused
        if paused:
            state.pause_reason = "property test"
        if drawdown_tripped:
            state.peak_equity = state.current_equity * D("10")
        if daily_loss_tripped:
            state.day_start_equity = state.current_equity * D("10")

        engine = RiskEngine(config, execution_config())
        order = Order(
            order_id="exit", symbol="AAA", side=Side.SELL, quantity=requested,
            order_type=OrderType.MARKET, signal_date=TODAY,
            strategy_id="property", reason="exit",
        )
        decision = engine.evaluate(order, portfolio, view, state, Reservations())
        assert decision.action is not RiskAction.REJECT
        assert decision.approved_quantity == min(requested, held)
        assert decision.rule not in {
            "manual_pause", "breaker_drawdown", "breaker_daily_loss",
            "stale_data", "zero_volume_liquidity",
        }

    @given(
        requested=quantities,
        price=prices,
        paused=st.booleans(),
        drawdown_tripped=st.booleans(),
    )
    @SETTINGS
    def test_a_buy_is_refused_when_a_breaker_is_tripped(
        self, requested, price, paused, drawdown_tripped
    ):
        """The other half of the asymmetry, so the exemption is not universal."""
        assume(paused or drawdown_tripped)
        config = RiskConfig()
        history = PriceHistory.from_bars(
            [make_bar("AAA", day, price, volume=1_000_000, source="property")
             for day in DAYS]
        )
        view = history.view(TODAY)
        portfolio = Portfolio(cash=D("1000000"))
        state = RiskState()
        state.open_day(TODAY, portfolio.cash)
        state.paused = paused
        if paused:
            state.pause_reason = "property test"
        if drawdown_tripped:
            state.peak_equity = state.current_equity * D("10")

        engine = RiskEngine(config, execution_config())
        order = Order(
            order_id="entry", symbol="AAA", side=Side.BUY, quantity=requested,
            order_type=OrderType.MARKET, signal_date=TODAY,
            strategy_id="property", reason="entry",
        )
        decision = engine.evaluate(order, portfolio, view, state, Reservations())
        assert decision.action is RiskAction.REJECT
        assert decision.approved_quantity == 0
        assert decision.rule in {"manual_pause", "breaker_drawdown"}


# --------------------------------------------------------------------------
# Non-vacuity, part 1: do the generated sequences reach the branches?
# --------------------------------------------------------------------------
class TestTheGeneratedSequencesReachEveryBranch:
    def test_the_generated_sequences_exercise_every_outcome(self):
        """Count what the generator actually produces, and fail if it is thin.

        Written as a single test with its own loop rather than a hypothesis
        property, because the assertion is about the whole population of
        examples and a per-example property cannot see it.
        """
        actions = Counter()
        rules = Counter()
        sides_seen = Counter()
        reserved_at_decision = 0

        @given(book=books(), steps=instructions)
        @settings(
            max_examples=200,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much,
                                   HealthCheck.function_scoped_fixture],
        )
        def collect(book, steps):
            nonlocal reserved_at_decision
            history, openings = book
            harness = Harness(history, openings)
            for symbol, side, quantity, settle in steps:
                if harness.reservations.cash > ZERO:
                    reserved_at_decision += 1
                _, decision = harness.submit(symbol, side, quantity, settle)
                actions[decision.action.value] += 1
                rules[decision.rule] += 1
                sides_seen[side.value] += 1

        collect()

        print("\nrisk-engine property census")
        print("  actions:", dict(actions))
        print("  binding rules:", dict(rules.most_common()))
        print("  sides:", dict(sides_seen))
        print("  decisions taken with cash already committed:", reserved_at_decision)

        assert actions["ACCEPT"] > 0, "no order was ever accepted"
        assert actions["REDUCE"] > 0, "no order was ever reduced"
        assert actions["REJECT"] > 0, "no order was ever rejected"
        assert sides_seen["BUY"] > 0 and sides_seen["SELL"] > 0
        binding = {r for r in rules if r != "all_rules_passed"}
        assert len(binding) >= 4, f"only {len(binding)} rules ever bound: {sorted(binding)}"
        assert reserved_at_decision > 0, (
            "no decision was ever taken while cash was committed by an unfilled "
            "order, which is the condition the reservation caps exist for"
        )


# --------------------------------------------------------------------------
# Non-vacuity, part 2: can the properties detect a broken engine?
# --------------------------------------------------------------------------
class GenerousSingleNameCap(RiskEngine):
    """Mutation: the single-name cap is 10x too permissive."""

    def _decide(self, order, portfolio, view, state, reservations):
        inflated = self.config.model_copy(
            update={"max_single_name_exposure": self.config.max_single_name_exposure * D("10")}
        )
        real, self.config = self.config, inflated
        try:
            return super()._decide(order, portfolio, view, state, reservations)
        finally:
            self.config = real


class ApprovesMoreThanAsked(RiskEngine):
    """Mutation: every acceptance is rounded up by one share."""

    def _decide(self, order, portfolio, view, state, reservations):
        decision = super()._decide(order, portfolio, view, state, reservations)
        if decision.action is RiskAction.REJECT:
            return decision
        return type(decision)(
            action=decision.action, rule=decision.rule, reason=decision.reason,
            approved_quantity=decision.approved_quantity + 1,
        )


class BlocksExitsOnABreaker(RiskEngine):
    """Mutation: the breaker exemption for exits is removed."""

    def _decide(self, order, portfolio, view, state, reservations):
        if state.paused:
            from signalforge.risk.engine import _reject

            return _reject("manual_pause", "paused, and this mutation blocks exits too")
        return super()._decide(order, portfolio, view, state, reservations)


class TestThePropertiesHaveTeeth:
    """Each mutation must be caught by the property that claims to cover it."""

    HISTORY = PriceHistory.from_bars(
        [
            make_bar(symbol, day, D("100"), volume=1_000_000, source="property")
            for symbol in SYMBOLS
            for day in DAYS
        ]
    )
    # Two unfilled buys at 100 against a 100,000 book. The 5% cap allows 50
    # shares in AAA, so the honest engine reduces the second order to 10 and
    # lands exactly on the cap, while the mutant takes all 70.
    #
    # The two quantities differ on purpose. Repeating 40 twice made both
    # engines approve 40 and no more, and the reason was the duplicate guard
    # rather than the cap: identical symbol, side, quantity, date and
    # strategy is one intent submitted twice. The mutation then looked
    # harmless, which is the exact way a teeth test passes vacuously.
    # `max_orders_per_symbol_per_day` is 2, so a third order would be refused
    # for yet another unrelated reason.
    STEPS = [("AAA", Side.BUY, 40, False), ("AAA", Side.BUY, 30, False)]

    def _total_approved(self, engine):
        harness = Harness(self.HISTORY, [], engine=engine)
        approved = 0
        for symbol, side, quantity, settle in self.STEPS:
            _, decision = harness.submit(symbol, side, quantity, settle)
            approved += decision.approved_quantity
        return harness, approved

    def test_a_too_generous_single_name_cap_is_caught(self):
        config = RiskConfig()
        allowance = STARTING_CASH * config.max_single_name_exposure

        honest, honest_approved = self._total_approved(
            RiskEngine(config, execution_config())
        )
        assert honest_approved * honest.marks["AAA"] <= allowance

        mutant, mutant_approved = self._total_approved(
            GenerousSingleNameCap(config, execution_config())
        )
        assert mutant_approved > honest_approved, (
            "the mutation did not change the decision, so this proves nothing"
        )
        assert mutant_approved * mutant.marks["AAA"] > allowance, (
            "the mutated cap was not detected by the cap property"
        )

    def test_an_over_approval_is_caught(self):
        harness = Harness(
            self.HISTORY, [], engine=ApprovesMoreThanAsked(RiskConfig(), execution_config())
        )
        over = False
        for symbol, side, quantity, settle in self.STEPS:
            order, decision = harness.submit(symbol, side, quantity, settle)
            if decision.approved_quantity > order.quantity:
                over = True
        assert over, "an engine approving more than requested was not detected"

    def test_a_reject_carrying_a_quantity_cannot_even_be_constructed(self):
        from signalforge.core.interfaces import RiskDecision

        with pytest.raises(ValidationError):
            RiskDecision(
                action=RiskAction.REJECT, rule="mutant",
                reason="a rejection with a quantity", approved_quantity=5,
            )

    def test_blocking_an_exit_on_a_breaker_is_caught(self):
        price = D("100")
        history = PriceHistory.from_bars(
            [make_bar("AAA", day, price, volume=1_000_000, source="property")
             for day in DAYS]
        )
        portfolio = Portfolio(cash=D("100000"))
        portfolio.apply_fill(
            Fill(order_id="seed", symbol="AAA", side=Side.BUY, quantity=10,
                 price=price, fees=D("1.00"), slippage_cost=ZERO, fill_date=DAYS[0]))
        state = RiskState()
        state.open_day(TODAY, portfolio.cash + 10 * price)
        state.paused = True
        state.pause_reason = "mutation"

        order = Order(
            order_id="exit", symbol="AAA", side=Side.SELL, quantity=10,
            order_type=OrderType.MARKET, signal_date=TODAY,
            strategy_id="property", reason="exit",
        )
        mutant = BlocksExitsOnABreaker(RiskConfig(), execution_config())
        decision = mutant.evaluate(order, portfolio, history.view(TODAY), state, Reservations())
        assert decision.action is RiskAction.REJECT, (
            "the mutation did not take effect, so this proves nothing"
        )

        real = RiskEngine(RiskConfig(), execution_config())
        honest = real.evaluate(order, portfolio, history.view(TODAY), state, Reservations())
        assert honest.action is not RiskAction.REJECT
        assert honest.approved_quantity == 10
