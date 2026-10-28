"""Regression: the benchmark must buy inside whatever window it is run over.

Found by running the real NASDAQCOM history. Buy-and-hold entered only on a
symbol's very first bar in all of history, so over the validation window
(starting 2005 on a series beginning in 1971) it emitted HOLD from day one,
never bought, and returned exactly 0.0%. A benchmark that silently stays in
cash makes every strategy look good by comparison, which is the worst
possible failure for the yardstick every other result is judged against.
"""

from datetime import date
from decimal import Decimal

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import SignalDirection
from signalforge.strategies.benchmark import BuyAndHold

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 200)


def history() -> PriceHistory:
    return PriceHistory.from_bars(
        [make_bar("IDX", d, D(100 + i), volume=10_000_000) for i, d in enumerate(DAYS)]
    )


def engine() -> BacktestEngine:
    return BacktestEngine(
        ResearchConfig(starting_cash=D("100000"), universe=("IDX",)), RiskConfig()
    )


class TestBenchmarkEntersInsideItsWindow:
    def test_it_asserts_a_long_on_a_day_deep_into_the_history(self):
        strategy = BuyAndHold(("IDX",), D("0.05"))
        hist = history()
        signals = strategy.generate_signals(DAYS[150], hist.view(DAYS[150]))
        assert [s.direction for s in signals] == [SignalDirection.ENTER_LONG]

    def test_it_actually_buys_when_the_run_starts_late(self):
        result = engine().run(BuyAndHold(("IDX",), D("0.05")), history(), start=DAYS[150])
        assert result.fills, "the benchmark never bought, so it was not a benchmark"
        assert result.final_equity > D("100000")

    def test_it_buys_exactly_once_and_then_holds(self):
        result = engine().run(BuyAndHold(("IDX",), D("0.05")), history(), start=DAYS[150])
        assert len(result.fills) == 1

    def test_it_never_sells(self):
        from signalforge.core.types import Side

        result = engine().run(BuyAndHold(("IDX",), D("0.05")), history(), start=DAYS[100])
        assert all(f.side is Side.BUY for f in result.fills)

    def test_a_full_window_run_still_buys_on_the_first_bar(self):
        result = engine().run(BuyAndHold(("IDX",), D("0.05")), history())
        assert len(result.fills) == 1
        assert result.fills[0].fill_date == DAYS[1]

    def test_it_re_asserts_so_a_rejected_first_order_can_still_fill_later(self):
        # The breaker is tripped on the first day it could have bought, so the
        # entry has to be re-asserted rather than abandoned forever.
        strategy = BuyAndHold(("IDX",), D("0.05"))
        hist = history()
        early = strategy.generate_signals(DAYS[5], hist.view(DAYS[5]))
        later = strategy.generate_signals(DAYS[6], hist.view(DAYS[6]))
        assert early[0].direction is SignalDirection.ENTER_LONG
        assert later[0].direction is SignalDirection.ENTER_LONG

    def test_a_symbol_with_no_data_yet_produces_no_signal(self):
        bars = [make_bar("A", d, D("100"), volume=1_000_000) for d in DAYS[:10]]
        bars += [make_bar("B", d, D("100"), volume=1_000_000) for d in DAYS[50:60]]
        hist = PriceHistory.from_bars(bars)
        strategy = BuyAndHold(("A", "B"), D("0.05"))
        symbols = {s.symbol for s in strategy.generate_signals(DAYS[5], hist.view(DAYS[5]))}
        assert symbols == {"A"}
