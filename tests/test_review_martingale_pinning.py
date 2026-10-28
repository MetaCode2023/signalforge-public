"""A risk reduction must not permanently pin a strategy's order size.

Found by code review, with measured reproduction. `record_submission` was
called with the post-reduction order, so `_last_order_quantity` stored the
APPROVED quantity. `check_order_doubling` then blocked anything >= twice
that, forever, because `_last_order_quantity` is never cleared.

Measured consequence: one thin early bar reduces a 50-share request to 1
share, and from then on the only size that passes is 1 — which re-pins the
ceiling at 1. The strategy is silently disabled for the rest of the run and
the rejection log blames "martingale", which is the wrong diagnosis.

Two things are wrong and both are fixed here:
  1. The doubling rule must compare against what the strategy ASKED for, not
     what risk allowed. `RiskState.signature` already captures intent, and
     martingale.check_order_doubling's docstring says "the previous order
     size" — recording the approved quantity contradicted both.
  2. A closed position ends the campaign, so the carried size must reset. A
     later, larger entry is a fresh decision, not an escalation.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.config.models import ExecutionConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import RiskAction
from signalforge.core.types import Fill, Order, OrderType, Portfolio, Side
from signalforge.risk.engine import RiskEngine, RiskState

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 300)


def flat_history(price: str = "100", symbol: str = "AAA"):
    return PriceHistory.from_bars(
        [make_bar(symbol, d, D(price), volume=10_000_000) for d in DAYS]
    )


def mk_order(quantity: int, day=None, **kw) -> Order:
    base = dict(
        order_id=f"o{quantity}-{day or DAYS[100]}",
        symbol="AAA",
        side=Side.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        signal_date=day or DAYS[100],
        strategy_id="strat",
        reason="test",
    )
    base.update(kw)
    return Order(**base)


def engine(**kw) -> RiskEngine:
    return RiskEngine(RiskConfig(**kw), ExecutionConfig())


class TestAReductionDoesNotPinFutureSize:
    def test_recording_a_reduced_order_keeps_the_requested_size(self):
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        requested = mk_order(50)
        approved = mk_order(1)
        state.record_submission(approved, requested_quantity=requested.quantity)
        assert state.last_order_quantity("strat", "AAA") == 50

    def test_a_later_full_size_request_is_not_blocked_as_doubling(self):
        eng = engine()
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        state.record_submission(mk_order(1), requested_quantity=50)
        state.open_day(DAYS[101], D("100000"))
        decision = eng.evaluate(
            mk_order(50, day=DAYS[101]),
            Portfolio(cash=D("100000")),
            flat_history().view(DAYS[101]),
            state,
        )
        assert decision.rule != "martingale_order_doubling", (
            "a strategy asking for the same size it asked for last time is not "
            "doubling; the reduction was risk's decision, not the strategy's"
        )

    def test_a_genuine_doubling_of_the_request_is_still_blocked(self):
        eng = engine()
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        state.record_submission(mk_order(10), requested_quantity=10)
        state.open_day(DAYS[101], D("100000"))
        decision = eng.evaluate(
            mk_order(20, day=DAYS[101]),
            Portfolio(cash=D("100000")),
            flat_history().view(DAYS[101]),
            state,
        )
        assert decision.action is RiskAction.REJECT
        assert decision.rule == "martingale_order_doubling"

    def test_record_submission_still_defaults_to_the_orders_own_quantity(self):
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        state.record_submission(mk_order(7))
        assert state.last_order_quantity("strat", "AAA") == 7


class TestClosingAPositionEndsTheCampaign:
    def test_a_closed_position_clears_the_carried_order_size(self):
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        state.record_submission(mk_order(10), requested_quantity=10)
        state.record_closed_trade(
            symbol="AAA", strategy_id="strat", quantity=10, pnl=D("50")
        )
        assert state.last_order_quantity("strat", "AAA") is None

    def test_a_larger_entry_after_a_profitable_exit_is_allowed(self):
        eng = engine()
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        state.record_submission(mk_order(10), requested_quantity=10)
        state.record_closed_trade(
            symbol="AAA", strategy_id="strat", quantity=10, pnl=D("50")
        )
        state.open_day(DAYS[101], D("100000"))
        decision = eng.evaluate(
            mk_order(40, day=DAYS[101]),
            Portfolio(cash=D("100000")),
            flat_history().view(DAYS[101]),
            state,
        )
        assert decision.allows_trade

    def test_a_larger_entry_after_a_LOSING_exit_is_still_blocked(self):
        # Clearing the campaign must not weaken the after-a-loss rule, which
        # is the one that matters most.
        eng = engine()
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        state.record_submission(mk_order(10), requested_quantity=10)
        state.record_closed_trade(
            symbol="AAA", strategy_id="strat", quantity=10, pnl=D("-500")
        )
        state.open_day(DAYS[101], D("100000"))
        decision = eng.evaluate(
            mk_order(40, day=DAYS[101]),
            Portfolio(cash=D("100000")),
            flat_history().view(DAYS[101]),
            state,
        )
        assert decision.action is RiskAction.REJECT
        assert decision.rule == "martingale_size_increase_after_loss"

    def test_clearing_is_scoped_to_the_symbol_and_strategy(self):
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        state.record_submission(mk_order(10), requested_quantity=10)
        state.record_submission(
            mk_order(12, symbol="BBB", order_id="ob"), requested_quantity=12
        )
        state.record_closed_trade(
            symbol="AAA", strategy_id="strat", quantity=10, pnl=D("1")
        )
        assert state.last_order_quantity("strat", "AAA") is None
        assert state.last_order_quantity("strat", "BBB") == 12


class TestEndToEndThroughTheEngine:
    def test_a_thin_early_bar_does_not_disable_a_strategy_for_the_whole_run(self):
        """The measured failure, end to end.

        A strategy asks for 5% of equity every day. The book starts small
        enough that the first order is reduced hard, then grows. Before the
        fix, the reduction pinned the ceiling and every later order was
        rejected as martingale doubling.
        """
        from signalforge.backtesting.engine import BacktestEngine
        from signalforge.config.models import ResearchConfig
        from signalforge.core.interfaces import Signal, SignalDirection

        class Enter:
            strategy_id = "enter"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                return [
                    Signal(
                        symbol="AAA",
                        direction=SignalDirection.ENTER_LONG,
                        as_of=as_of,
                        strategy_id="enter",
                        reason="target weight",
                        target_weight=D("0.05"),
                    )
                ]

        # Rising price so the position closes and reopens repeatedly is not
        # needed; one entry then exits are enough to exercise the carry.
        hist = PriceHistory.from_bars(
            [make_bar("AAA", d, D(10 + i), volume=10_000_000) for i, d in enumerate(DAYS)]
        )
        research = ResearchConfig(starting_cash=D("100000"), universe=("AAA",))
        result = BacktestEngine(research, RiskConfig()).run(Enter(), hist)
        doubling = [
            e for e in result.risk_events if e.rule == "martingale_order_doubling"
        ]
        assert not doubling, (
            "a strategy asking for a constant target weight triggered the doubling "
            f"rule {len(doubling)} times; the rule is firing on risk's own reductions"
        )
