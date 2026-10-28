"""Instruments whose source publishes no volume.

An index series (NASDAQCOM) has a close and nothing else. Two wrong answers
are available: invent a volume so the engine runs, which makes the
participation and liquidity caps silently meaningless, or let the
zero-volume rule block every order, which makes the series unusable.

The right answer is to record that volume is NOT KNOWN, skip exactly the
checks that depend on it, and say so loudly in the run's limitations. These
tests pin that behaviour so neither wrong answer creeps back in.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.execution import FillModel
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import RiskAction
from signalforge.core.types import Bar, Order, OrderType, Portfolio, Side
from signalforge.risk.engine import RiskEngine, RiskState

from tests.conftest import business_days

D = Decimal
DAYS = business_days(date(2020, 1, 1), 120)


def index_bar(day: date, close: str, symbol: str = "NASDAQCOM") -> Bar:
    c = D(close)
    return Bar(
        symbol=symbol,
        trade_date=day,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=0,
        adj_close=c,
        source="fred",
        volume_known=False,
    )


def mk_order(**kw) -> Order:
    base = dict(
        order_id="o1",
        symbol="NASDAQCOM",
        side=Side.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        signal_date=DAYS[100],
        strategy_id="s",
        reason="r",
    )
    base.update(kw)
    return Order(**base)


class TestBarVolumeKnownFlag:
    def test_a_bar_defaults_to_volume_known(self):
        bar = Bar(
            symbol="AAPL",
            trade_date=DAYS[0],
            open=D("1"),
            high=D("1"),
            low=D("1"),
            close=D("1"),
            volume=100,
        )
        assert bar.volume_known is True

    def test_a_bar_with_unknown_volume_must_report_zero_volume(self):
        # Carrying both a volume number and "volume unknown" would be two
        # contradictory truths in one record.
        with pytest.raises(Exception):
            Bar(
                symbol="NASDAQCOM",
                trade_date=DAYS[0],
                open=D("1"),
                high=D("1"),
                low=D("1"),
                close=D("1"),
                volume=500,
                volume_known=False,
            )


class TestFillModelWithUnknownVolume:
    def test_a_bar_with_unknown_volume_still_fills(self):
        fm = FillModel(ExecutionConfig())
        fill = fm.fill(
            mk_order(signal_date=DAYS[0]), index_bar(DAYS[1], "100"), previous_close=D("100")
        )
        assert fill is not None
        assert fill.quantity == 10

    def test_the_participation_cap_does_not_apply_when_volume_is_unknown(self):
        fm = FillModel(ExecutionConfig(max_participation_of_volume=D("0.01")))
        fill = fm.fill(
            mk_order(signal_date=DAYS[0], quantity=5_000_000),
            index_bar(DAYS[1], "100"),
            previous_close=D("100"),
        )
        assert fill.quantity == 5_000_000

    def test_a_genuine_zero_volume_bar_still_does_not_fill(self):
        # The exemption must apply only to "not published", never to a real
        # zero print, or the liquidity guard is gone for everyone.
        fm = FillModel(ExecutionConfig())
        real_zero = Bar(
            symbol="NASDAQCOM",
            trade_date=DAYS[1],
            open=D("100"),
            high=D("100"),
            low=D("100"),
            close=D("100"),
            volume=0,
            volume_known=True,
        )
        assert fm.fill(mk_order(signal_date=DAYS[0]), real_zero, previous_close=D("100")) is None


class TestRiskEngineWithUnknownVolume:
    def test_the_liquidity_breaker_does_not_trip_on_unknown_volume(self):
        history = PriceHistory.from_bars([index_bar(d, "100") for d in DAYS])
        engine = RiskEngine(RiskConfig(), ExecutionConfig())
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        decision = engine.evaluate(
            mk_order(), Portfolio(cash=D("100000")), history.view(DAYS[100]), state
        )
        assert decision.action is RiskAction.ACCEPT

    def test_a_real_zero_volume_bar_is_still_refused(self):
        bars = [
            Bar(
                symbol="NASDAQCOM",
                trade_date=d,
                open=D("100"),
                high=D("100"),
                low=D("100"),
                close=D("100"),
                volume=0,
                volume_known=True,
            )
            for d in DAYS
        ]
        engine = RiskEngine(RiskConfig(), ExecutionConfig())
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        decision = engine.evaluate(
            mk_order(),
            Portfolio(cash=D("100000")),
            PriceHistory.from_bars(bars).view(DAYS[100]),
            state,
        )
        assert decision.action is RiskAction.REJECT
        assert "volume" in decision.rule or "liquidity" in decision.rule

    def test_the_participation_cap_is_not_applied_when_volume_is_unknown(self):
        history = PriceHistory.from_bars([index_bar(d, "100") for d in DAYS])
        engine = RiskEngine(
            RiskConfig(max_single_name_exposure=D("1.0")), ExecutionConfig()
        )
        state = RiskState()
        state.open_day(DAYS[100], D("100000"))
        decision = engine.evaluate(
            mk_order(quantity=900), Portfolio(cash=D("100000")), history.view(DAYS[100]), state
        )
        # With a known volume of 0 this would be rejected outright; the cap is
        # simply absent here, so cash and exposure are what bind.
        assert decision.allows_trade


class TestLimitationIsAnnounced:
    def test_an_unknown_volume_run_says_so_in_its_limitations(self):
        from signalforge.backtesting.engine import BacktestEngine

        class Nothing:
            strategy_id = "nothing"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                return []

        history = PriceHistory.from_bars([index_bar(d, "100") for d in DAYS])
        engine = BacktestEngine(
            ResearchConfig(starting_cash=D("100000")), RiskConfig()
        )
        result = engine.run(Nothing(), history)
        joined = " ".join(result.data_limitations).lower()
        assert "volume" in joined
        assert "participation" in joined or "liquidity" in joined
