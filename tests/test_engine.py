"""The backtest engine event loop.

Engine tests use strategies defined locally in this file rather than the real
strategy library, so that an engine failure is never mistaken for a strategy
failure and the two can be worked on independently.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.errors import LookAheadError
from signalforge.core.history import PointInTimeView, PriceHistory
from signalforge.core.interfaces import RiskAction, Signal, SignalDirection
from signalforge.core.types import OrderStatus

from tests.conftest import business_days, make_bar

D = Decimal


# --------------------------------------------------------------------------
# Test strategies
# --------------------------------------------------------------------------
class BuyOnceStrategy:
    """Buys a fixed weight of one symbol on a named date, then holds."""

    strategy_id = "buy_once"
    warmup_days = 0

    def __init__(self, symbol: str, on: date, weight: str = "0.05"):
        self.symbol = symbol
        self.on = on
        self.weight = D(weight)

    def parameters(self):
        return {"symbol": self.symbol, "on": self.on.isoformat(), "weight": str(self.weight)}

    def generate_signals(self, as_of, view):
        if as_of != self.on:
            return []
        return [
            Signal(
                symbol=self.symbol,
                direction=SignalDirection.ENTER_LONG,
                as_of=as_of,
                strategy_id=self.strategy_id,
                reason="scheduled entry",
                target_weight=self.weight,
            )
        ]


class DoNothingStrategy:
    strategy_id = "do_nothing"
    warmup_days = 0

    def parameters(self):
        return {}

    def generate_signals(self, as_of, view):
        return []


class PeekingStrategy:
    """Deliberately tries to read a future bar. Must not be able to."""

    strategy_id = "peeking"
    warmup_days = 0

    def __init__(self, symbol: str):
        self.symbol = symbol

    def parameters(self):
        return {"symbol": self.symbol}

    def generate_signals(self, as_of, view):
        view.bar(self.symbol, as_of + timedelta(days=30))
        return []


class EnterThenExitStrategy:
    strategy_id = "enter_exit"
    warmup_days = 0

    def __init__(self, symbol: str, enter_on: date, exit_on: date):
        self.symbol = symbol
        self.enter_on = enter_on
        self.exit_on = exit_on

    def parameters(self):
        return {"symbol": self.symbol}

    def generate_signals(self, as_of, view):
        if as_of == self.enter_on:
            return [
                Signal(
                    symbol=self.symbol,
                    direction=SignalDirection.ENTER_LONG,
                    as_of=as_of,
                    strategy_id=self.strategy_id,
                    reason="enter",
                    target_weight=D("0.05"),
                )
            ]
        if as_of == self.exit_on:
            return [
                Signal(
                    symbol=self.symbol,
                    direction=SignalDirection.EXIT_LONG,
                    as_of=as_of,
                    strategy_id=self.strategy_id,
                    reason="exit",
                )
            ]
        return []


class GreedyStrategy:
    """Asks for the whole book every single day. Risk must contain it."""

    strategy_id = "greedy"
    warmup_days = 0

    def __init__(self, symbol: str):
        self.symbol = symbol

    def parameters(self):
        return {"symbol": self.symbol}

    def generate_signals(self, as_of, view):
        return [
            Signal(
                symbol=self.symbol,
                direction=SignalDirection.ENTER_LONG,
                as_of=as_of,
                strategy_id=self.strategy_id,
                reason="maximum size",
                target_weight=D("1.0"),
            )
        ]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
DAYS = business_days(date(2020, 1, 1), 120)


@pytest.fixture
def rising() -> PriceHistory:
    return PriceHistory.from_bars(
        [make_bar("AAPL", d, D(100 + i), volume=10_000_000) for i, d in enumerate(DAYS)]
    )


def engine(**kw) -> BacktestEngine:
    research = ResearchConfig(
        starting_cash=D(kw.pop("cash", "100000")),
        universe=("AAPL",),
        execution=ExecutionConfig(**kw.pop("execution", {})),
    )
    return BacktestEngine(research, RiskConfig(**kw.pop("risk", {})))


class TestLookAheadPrevention:
    def test_a_strategy_that_peeks_forward_cannot_succeed(self, rising):
        with pytest.raises(LookAheadError):
            engine().run(PeekingStrategy("AAPL"), rising)

    def test_a_signal_on_day_t_fills_on_day_t_plus_one(self, rising):
        signal_day = DAYS[10]
        result = engine().run(BuyOnceStrategy("AAPL", signal_day), rising)
        assert len(result.fills) == 1
        assert result.fills[0].fill_date == DAYS[11]
        assert result.orders[0].signal_date == signal_day

    def test_the_fill_price_comes_from_the_next_bar_not_the_signal_bar(self, rising):
        signal_day = DAYS[10]
        result = engine().run(
            BuyOnceStrategy("AAPL", signal_day), rising, execution={"slippage_bps": D("0")}
        )
        # Bar i has close 100+i and open equal to close in this fixture, so the
        # fill must be at 111 (day 11), never 110 (the signal day's close).
        assert result.fills[0].price == D("111")

    def test_a_longer_fill_delay_is_honoured(self, rising):
        result = engine().run(
            BuyOnceStrategy("AAPL", DAYS[10]), rising, execution={"fill_delay_days": 3}
        )
        assert result.fills[0].fill_date == DAYS[13]

    def test_a_fill_skips_a_non_trading_gap(self):
        # Bars exist on index 0 and 5 only; a signal on the first must fill on
        # the next bar that actually exists, not on a calendar guess.
        days = [DAYS[0], DAYS[5]]
        hist = PriceHistory.from_bars(
            [make_bar("AAPL", d, D("100"), volume=10_000_000) for d in days]
        )
        result = engine().run(BuyOnceStrategy("AAPL", DAYS[0]), hist)
        assert result.fills[0].fill_date == DAYS[5]

    def test_a_signal_on_the_last_bar_never_fills(self, rising):
        result = engine().run(BuyOnceStrategy("AAPL", DAYS[-1]), rising)
        assert result.fills == []
        assert result.orders[-1].status is OrderStatus.EXPIRED


class TestAccountingInvariants:
    def test_equity_equals_cash_plus_marked_positions_every_single_day(self, rising):
        result = engine().run(BuyOnceStrategy("AAPL", DAYS[10]), rising)
        for point in result.equity_curve:
            assert point.equity == point.cash + point.gross_exposure

    def test_cash_never_goes_negative(self, rising):
        result = engine().run(GreedyStrategy("AAPL"), rising)
        assert all(p.cash >= 0 for p in result.equity_curve)

    def test_a_do_nothing_strategy_leaves_equity_exactly_at_starting_cash(self, rising):
        result = engine().run(DoNothingStrategy(), rising)
        assert result.final_equity == D("100000")
        assert all(p.equity == D("100000") for p in result.equity_curve)

    def test_one_equity_point_per_trading_day(self, rising):
        result = engine().run(DoNothingStrategy(), rising)
        assert len(result.equity_curve) == len(DAYS)
        assert [p.valuation_date for p in result.equity_curve] == list(DAYS)

    def test_fees_and_slippage_are_deducted_not_ignored(self, rising):
        with_costs = engine().run(
            BuyOnceStrategy("AAPL", DAYS[10]),
            rising,
            execution={"slippage_bps": D("50"), "commission_per_share": D("0.10")},
        )
        without = engine().run(
            BuyOnceStrategy("AAPL", DAYS[10]),
            rising,
            execution={"slippage_bps": D("0"), "commission_per_share": D("0")},
        )
        assert with_costs.final_equity < without.final_equity

    def test_a_round_trip_realizes_profit_in_a_rising_market(self, rising):
        result = engine().run(
            EnterThenExitStrategy("AAPL", DAYS[10], DAYS[80]),
            rising,
            execution={"slippage_bps": D("0"), "commission_per_share": D("0"), "min_commission": D("0")},
        )
        assert len(result.fills) == 2
        assert result.final_equity > D("100000")

    def test_peak_equity_is_monotonically_non_decreasing(self, rising):
        result = engine().run(BuyOnceStrategy("AAPL", DAYS[10]), rising)
        peaks = [p.peak_equity for p in result.equity_curve]
        assert peaks == sorted(peaks)

    def test_drawdown_is_zero_on_a_monotonically_rising_curve(self, rising):
        result = engine().run(BuyOnceStrategy("AAPL", DAYS[10]), rising)
        assert result.equity_curve[-1].drawdown == 0


class TestRiskIntegration:
    def test_a_greedy_strategy_is_held_to_the_single_name_cap_when_it_buys(self, rising):
        # The cap binds at order time. Afterwards the position is free to drift
        # above it as the price appreciates, and the engine must NOT force-sell
        # to defend a percentage: that would be a rule inventing trades.
        result = engine().run(GreedyStrategy("AAPL"), rising)
        by_date = {p.valuation_date: p for p in result.equity_curve}
        for f in result.fills:
            point = by_date[f.fill_date]
            notional = f.price * f.quantity
            assert notional <= point.equity * D("0.051")

    def test_rejected_orders_appear_in_the_risk_event_log(self, rising):
        result = engine().run(GreedyStrategy("AAPL"), rising)
        assert result.risk_events
        assert any(e.action is not RiskAction.ACCEPT for e in result.risk_events)

    def test_a_rejected_order_moves_no_money(self, rising):
        result = engine(cash="10").run(
            BuyOnceStrategy("AAPL", DAYS[10], weight="0.05"), rising
        )
        assert result.fills == []
        assert result.final_equity == D("10")

    def test_every_order_carries_a_reason_into_the_ledger(self, rising):
        result = engine().run(EnterThenExitStrategy("AAPL", DAYS[10], DAYS[80]), rising)
        assert all(o.reason for o in result.orders)
        assert {o.reason for o in result.orders} == {"enter", "exit"}

    def test_pyramiding_is_blocked_so_a_greedy_strategy_buys_once(self, rising):
        result = engine().run(GreedyStrategy("AAPL"), rising)
        assert len(result.fills) == 1


class TestReproducibility:
    def test_two_runs_of_the_same_inputs_produce_identical_results(self, rising):
        a = engine().run(BuyOnceStrategy("AAPL", DAYS[10]), rising, run_id="fixed")
        b = engine().run(BuyOnceStrategy("AAPL", DAYS[10]), rising, run_id="fixed")
        assert [(f.fill_id, f.price, f.quantity) for f in a.fills] == [
            (f.fill_id, f.price, f.quantity) for f in b.fills
        ]
        assert [p.equity for p in a.equity_curve] == [p.equity for p in b.equity_curve]

    def test_the_same_data_always_fingerprints_the_same(self, rising):
        a = engine().run(DoNothingStrategy(), rising)
        b = engine().run(DoNothingStrategy(), rising)
        assert a.dataset_fingerprint == b.dataset_fingerprint
        assert a.dataset_fingerprint

    def test_different_data_fingerprints_differently(self, rising):
        other = PriceHistory.from_bars(
            [make_bar("AAPL", d, D(200 + i), volume=10_000_000) for i, d in enumerate(DAYS)]
        )
        a = engine().run(DoNothingStrategy(), rising)
        b = engine().run(DoNothingStrategy(), other)
        assert a.dataset_fingerprint != b.dataset_fingerprint

    def test_replaying_a_result_does_not_duplicate_fills(self, rising):
        result = engine().run(BuyOnceStrategy("AAPL", DAYS[10]), rising)
        fill_ids = [f.fill_id for f in result.fills]
        assert len(fill_ids) == len(set(fill_ids))

    def test_the_result_records_the_parameters_that_produced_it(self, rising):
        strategy = BuyOnceStrategy("AAPL", DAYS[10])
        result = engine().run(strategy, rising)
        assert result.parameters == strategy.parameters()


class TestWindowing:
    def test_a_start_date_truncates_the_run(self, rising):
        result = engine().run(DoNothingStrategy(), rising, start=DAYS[50])
        assert result.equity_curve[0].valuation_date == DAYS[50]
        assert result.start_date == DAYS[50]

    def test_an_end_date_truncates_the_run(self, rising):
        result = engine().run(DoNothingStrategy(), rising, end=DAYS[50])
        assert result.equity_curve[-1].valuation_date == DAYS[50]

    def test_a_window_with_no_trading_days_is_refused(self, rising):
        with pytest.raises(Exception):
            engine().run(DoNothingStrategy(), rising, start=date(2050, 1, 1))

    def test_the_strategy_only_sees_bars_inside_its_run_window(self, rising):
        seen: list[date] = []

        class Recorder:
            strategy_id = "recorder"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                seen.append(as_of)
                return []

        engine().run(Recorder(), rising, start=DAYS[10], end=DAYS[20])
        assert seen == list(DAYS[10:21])

    def test_history_before_the_window_remains_available_for_warmup(self, rising):
        lengths: list[int] = []

        class Recorder:
            strategy_id = "recorder"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                lengths.append(len(view.closes("AAPL")))
                return []

        engine().run(Recorder(), rising, start=DAYS[50], end=DAYS[51])
        # Warmup data from before the window is visible; the window governs
        # which days the strategy is asked about, not what it may look back at.
        assert lengths[0] == 51


class TestLimitations:
    def test_data_limitations_travel_with_the_result(self, rising):
        result = engine().run(
            DoNothingStrategy(), rising, data_limitations=["synthetic fixture data"]
        )
        assert "synthetic fixture data" in result.data_limitations

    def test_a_close_only_series_is_flagged_automatically(self):
        # Bars where open == high == low == close carry no intraday range, so
        # the engine must say so rather than let a reader assume otherwise.
        hist = PriceHistory.from_bars(
            [make_bar("IDX", d, D("100"), volume=1_000_000) for d in DAYS]
        )
        result = engine().run(DoNothingStrategy(), hist)
        assert any("close-only" in note.lower() for note in result.data_limitations)

    def test_the_run_records_its_symbols_and_bar_count(self, rising):
        result = engine().run(DoNothingStrategy(), rising)
        assert result.symbols == ("AAPL",)
        assert result.bar_count == len(DAYS)
