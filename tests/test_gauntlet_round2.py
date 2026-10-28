"""Adversarial gauntlet, round 2: the gap between signal time and fill time.

Every order in this system is sized against the book as it stood when the
signal fired, and filled a bar later against a book that may have changed.
Each test below is one way that gap turns into a crash or a silent wrong
number on real data.

Ranked by severity when the round was planned:

 1. A buy whose cost rises above available cash before it fills. On real data
    a gap up does this routinely. Unhandled it raises AccountingError from
    inside the event loop and aborts the whole backtest. Severity: critical,
    it stops the run.
 2. Two exit orders for the same position queued on the same bar. The second
    fill tries to sell shares that are gone. Severity: critical, same.
 3. Several buys for different new symbols on one bar, each inside
    max_positions individually but together over it. Severity: high, the cap
    silently fails to hold.
 4. The same, for the single-name and cluster exposure caps.
 5. A corporate action inside a holding period. Not modelled; must be stated
    rather than discovered later.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import RiskAction, Signal, SignalDirection
from signalforge.core.types import OrderStatus, Side

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 60)


def engine(cash="100000", execution=None, **risk) -> BacktestEngine:
    return BacktestEngine(
        ResearchConfig(
            starting_cash=D(cash),
            universe=("X",),
            execution=ExecutionConfig(**(execution or {})),
        ),
        RiskConfig(**risk),
    )


def sig(symbol, direction, as_of, weight="0.05", reason="r", strategy_id="s"):
    return Signal(
        symbol=symbol,
        direction=direction,
        as_of=as_of,
        strategy_id=strategy_id,
        reason=reason,
        target_weight=D(weight) if direction is SignalDirection.ENTER_LONG else D("0"),
    )


class ScriptedStrategy:
    """Emits exactly the signals it is handed, keyed by date."""

    warmup_days = 0

    def __init__(self, script: dict, strategy_id: str = "scripted"):
        self.script = script
        self.strategy_id = strategy_id

    def parameters(self):
        return {"script": sorted(self.script)}

    def generate_signals(self, as_of, view):
        return list(self.script.get(as_of, []))


class TestCashShortfallBetweenSignalAndFill:
    def test_a_price_gap_up_before_the_fill_does_not_abort_the_run(self):
        """Sized on day T's close, filled on a day that gapped up 40%.

        The order needs more cash than the book has. The run must continue
        with the order reduced or refused, never raise out of the event loop.
        """
        bars = [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:5]]
        bars.append(make_bar("X", DAYS[5], D("140"), open_=D("140"), volume=10_000_000))
        bars += [make_bar("X", d, D("140"), volume=10_000_000) for d in DAYS[6:20]]
        history = PriceHistory.from_bars(bars)

        # Spend almost everything, so a 40% gap cannot be funded.
        strategy = ScriptedStrategy(
            {DAYS[4]: [sig("X", SignalDirection.ENTER_LONG, DAYS[4], weight="1.0")]}
        )
        result = engine(max_single_name_exposure=D("1.0")).run(strategy, history)
        assert result.equity_curve, "the run aborted instead of handling the shortfall"
        assert result.final_equity > 0
        assert all(p.cash >= 0 for p in result.equity_curve)

    def test_the_shortfall_is_recorded_rather_than_swallowed(self):
        bars = [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:5]]
        bars.append(make_bar("X", DAYS[5], D("140"), open_=D("140"), volume=10_000_000))
        bars += [make_bar("X", d, D("140"), volume=10_000_000) for d in DAYS[6:20]]
        history = PriceHistory.from_bars(bars)
        strategy = ScriptedStrategy(
            {DAYS[4]: [sig("X", SignalDirection.ENTER_LONG, DAYS[4], weight="1.0")]}
        )
        result = engine(max_single_name_exposure=D("1.0")).run(strategy, history)
        assert any(
            "cash" in e.rule or "shortfall" in e.rule for e in result.risk_events
        ), "a fill reduced for want of cash must leave a trace"

    def test_a_partial_fill_for_want_of_cash_buys_what_it_can(self):
        bars = [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:5]]
        bars.append(make_bar("X", DAYS[5], D("110"), open_=D("110"), volume=10_000_000))
        bars += [make_bar("X", d, D("110"), volume=10_000_000) for d in DAYS[6:20]]
        history = PriceHistory.from_bars(bars)
        strategy = ScriptedStrategy(
            {DAYS[4]: [sig("X", SignalDirection.ENTER_LONG, DAYS[4], weight="1.0")]}
        )
        result = engine(max_single_name_exposure=D("1.0")).run(strategy, history)
        assert result.fills, "a 10% gap should still leave an affordable quantity"
        assert result.fills[0].quantity > 0


class TestDoubleExit:
    def test_two_exit_orders_on_one_bar_do_not_abort_the_run(self):
        """Two strategies, or one strategy twice, both ask to close the book.

        The second fill would sell shares that no longer exist. That must be
        handled, not raised out of the loop.
        """
        history = PriceHistory.from_bars(
            [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:20]]
        )
        script = {
            DAYS[2]: [sig("X", SignalDirection.ENTER_LONG, DAYS[2])],
            DAYS[6]: [
                sig("X", SignalDirection.EXIT_LONG, DAYS[6], reason="exit one"),
                sig("X", SignalDirection.EXIT_LONG, DAYS[6], reason="exit two"),
            ],
        }
        result = engine().run(ScriptedStrategy(script), history)
        assert result.equity_curve
        assert result.final_equity > 0

    def test_the_position_is_not_driven_negative(self):
        history = PriceHistory.from_bars(
            [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:20]]
        )
        script = {
            DAYS[2]: [sig("X", SignalDirection.ENTER_LONG, DAYS[2])],
            DAYS[6]: [
                sig("X", SignalDirection.EXIT_LONG, DAYS[6], reason="exit one"),
                sig("X", SignalDirection.EXIT_LONG, DAYS[6], reason="exit two"),
            ],
        }
        result = engine().run(ScriptedStrategy(script), history)
        sold = sum(f.quantity for f in result.fills if f.side is Side.SELL)
        bought = sum(f.quantity for f in result.fills if f.side is Side.BUY)
        assert sold <= bought

    def test_the_redundant_exit_is_recorded_not_silently_dropped(self):
        history = PriceHistory.from_bars(
            [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:20]]
        )
        script = {
            DAYS[2]: [sig("X", SignalDirection.ENTER_LONG, DAYS[2])],
            DAYS[6]: [
                sig("X", SignalDirection.EXIT_LONG, DAYS[6], reason="exit one"),
                sig("X", SignalDirection.EXIT_LONG, DAYS[6], reason="exit two"),
            ],
        }
        result = engine().run(ScriptedStrategy(script), history)
        statuses = {o.order_id: o.status for o in result.orders}
        assert any(
            s in (OrderStatus.REJECTED, OrderStatus.EXPIRED) for s in statuses.values()
        )


class TestPendingOrdersCountTowardsLimits:
    def test_several_new_symbols_on_one_bar_cannot_breach_max_positions(self):
        """Each order is inside the cap alone; together they are over it.

        Checking the cap against only *filled* positions lets a strategy open
        as many as it likes in one bar, which is the limit failing silently.
        """
        bars = []
        for symbol in ("A", "B", "C", "D"):
            bars.extend(make_bar(symbol, d, D("100"), volume=10_000_000) for d in DAYS[:20])
        history = PriceHistory.from_bars(bars)
        script = {
            DAYS[2]: [
                sig(s, SignalDirection.ENTER_LONG, DAYS[2], reason=f"enter {s}")
                for s in ("A", "B", "C", "D")
            ]
        }
        result = engine(max_positions=2).run(ScriptedStrategy(script), history)
        assert max(p.position_count for p in result.equity_curve) <= 2

    def test_the_breach_attempt_is_recorded(self):
        bars = []
        for symbol in ("A", "B", "C", "D"):
            bars.extend(make_bar(symbol, d, D("100"), volume=10_000_000) for d in DAYS[:20])
        history = PriceHistory.from_bars(bars)
        script = {
            DAYS[2]: [
                sig(s, SignalDirection.ENTER_LONG, DAYS[2], reason=f"enter {s}")
                for s in ("A", "B", "C", "D")
            ]
        }
        result = engine(max_positions=2).run(ScriptedStrategy(script), history)
        assert any(e.rule == "max_positions" for e in result.risk_events)

    def test_split_buys_of_one_symbol_on_one_bar_cannot_breach_its_exposure_cap(self):
        history = PriceHistory.from_bars(
            [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:20]]
        )
        # Four separate entries for the same symbol on the same bar, each
        # individually inside the 5% cap but four times it in total.
        script = {
            DAYS[2]: [
                sig("X", SignalDirection.ENTER_LONG, DAYS[2], weight="0.05", reason=f"slice {i}")
                for i in range(4)
            ]
        }
        result = engine(max_orders_per_symbol_per_day=10, allow_pyramiding=True).run(
            ScriptedStrategy(script), history
        )
        bought = sum(f.quantity for f in result.fills if f.side is Side.BUY)
        # 5% of 100,000 at 100 a share is 50 shares, however it is sliced.
        assert bought <= 50


class TestCorporateActionsAreDeclared:
    def test_a_run_states_that_holdings_are_not_split_adjusted(self):
        history = PriceHistory.from_bars(
            [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:20]]
        )
        result = engine().run(ScriptedStrategy({}), history)
        joined = " ".join(result.data_limitations).lower()
        assert "corporate action" in joined or "split" in joined, (
            "a share count that is never adjusted for a split must be declared; "
            "discovering it from a wrong P&L later is the failure mode"
        )
