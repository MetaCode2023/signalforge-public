"""Adversarial gauntlet, round 1: data integrity, bias, and valuation.

Ranked by severity when the round was planned:

 1. Adjusted-close anchoring. A back-adjusted series is computed from the
    whole history, so the LEVEL of adj_close at time T depends on corporate
    actions after T. Ratio- and return-based strategies are unaffected
    because they are scale-invariant; a level-dependent one would be
    contaminated. Severity: high, because it is invisible. Mitigation: prove
    scale invariance for every shipped strategy so that adding a
    level-dependent one breaks a test instead of quietly leaking.
 2. Stale valuation of a position whose symbol stopped printing. `marks()`
    returns the last known close forever, so a delisted holding is carried at
    its final price indefinitely and equity is overstated. Severity: high.
 3. Survivorship bias in the universe and in the cross-section snapshot.
    Severity: high, but not fixable with the data available; must be stated.
 4. A dataset fingerprint that ignores a field the engine acts on.
 5. Non-positive observations in a macro series being coerced to a price.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import SignalDirection
from signalforge.core.types import Bar, Side
from signalforge.pipeline import series_history
from signalforge.strategies.registry import build

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 400)


def engine(**risk) -> BacktestEngine:
    return BacktestEngine(
        ResearchConfig(starting_cash=D("100000"), universe=("X",)), RiskConfig(**risk)
    )


SPECS = [
    ("ma_trend", {"fast_window": 10, "slow_window": 30, "target_weight": D("0.05"), "symbols": ("X",)}),
    ("zscore_mean_reversion", {"window": 20, "entry_z": D("2"), "exit_z": D("0"),
                               "target_weight": D("0.05"), "max_holding_days": 20, "symbols": ("X",)}),
    ("vol_targeted_trend", {"fast_window": 10, "slow_window": 30, "target_volatility": D("0.10"),
                            "vol_window": 20, "max_weight": D("0.05"), "annualisation_factor": 252,
                            "symbols": ("X",)}),
    ("buy_and_hold", {"symbols": ("X",), "target_weight": D("0.05")}),
    ("xs_momentum", {"lookback_days": 60, "skip_days": 5, "top_n": 1,
                     "target_weight_total": D("0.05"), "min_history_days": 70}),
]


def wiggly(scale: Decimal) -> PriceHistory:
    """A series with trend, noise and mean-reverting excursions, scaled."""
    bars = []
    for i, day in enumerate(DAYS):
        base = 100 + i * Decimal("0.3")
        noise = Decimal(((i * 37) % 23) - 11) / 2
        close = (base + noise) * scale
        bars.append(make_bar("X", day, close, volume=10_000_000))
    return PriceHistory.from_bars(bars)


class TestAdjustedCloseAnchoringIsSafeForEveryShippedStrategy:
    """Scale invariance is what makes back-adjustment harmless here.

    Back-adjusting multiplies every bar before a corporate action by a
    constant. If a strategy's signals are unchanged when the whole series is
    multiplied by a constant, then the choice of adjustment anchor cannot
    change its decisions, and the future information embedded in the anchor
    cannot leak into them.
    """

    @pytest.mark.parametrize("strategy_id,params", SPECS, ids=[s[0] for s in SPECS])
    def test_signal_directions_are_invariant_to_a_uniform_price_scaling(
        self, strategy_id, params
    ):
        plain = wiggly(D("1"))
        scaled = wiggly(D("7.5"))
        a = build(strategy_id, **params)
        b = build(strategy_id, **params)
        for day in DAYS[100:400:17]:
            left = [(s.symbol, s.direction) for s in a.generate_signals(day, plain.view(day))]
            right = [(s.symbol, s.direction) for s in b.generate_signals(day, scaled.view(day))]
            assert left == right, (
                f"{strategy_id} changed its mind when the price series was scaled by a "
                "constant. It is level-dependent, so a back-adjusted series would leak "
                "future corporate-action information into its decisions. Such a "
                "strategy must read unadjusted prices and handle actions explicitly."
            )

    def test_the_guard_catches_a_deliberately_level_dependent_strategy(self):
        # Proof the test above has teeth: a strategy keyed to an absolute
        # price level must fail the same comparison.
        class LevelDependent:
            strategy_id = "level_dependent"
            warmup_days = 1

            def parameters(self):
                return {"threshold": "150"}

            def generate_signals(self, as_of, view):
                from signalforge.core.interfaces import Signal

                bar = view.latest("X")
                if bar is None:
                    return []
                direction = (
                    SignalDirection.ENTER_LONG
                    if bar.close < D("150")
                    else SignalDirection.EXIT_LONG
                )
                return [
                    Signal(
                        symbol="X",
                        direction=direction,
                        as_of=as_of,
                        strategy_id=self.strategy_id,
                        reason="absolute level rule",
                        target_weight=D("0.05") if direction is SignalDirection.ENTER_LONG else D("0"),
                    )
                ]

        plain, scaled = wiggly(D("1")), wiggly(D("7.5"))
        strategy = LevelDependent()
        differed = False
        for day in DAYS[100:400:17]:
            left = [s.direction for s in strategy.generate_signals(day, plain.view(day))]
            right = [s.direction for s in strategy.generate_signals(day, scaled.view(day))]
            if left != right:
                differed = True
                break
        assert differed, "the scale-invariance check would not catch a level-dependent rule"


class TestStalePositionValuation:
    def test_a_holding_that_stops_printing_is_flagged_not_silently_carried(self):
        """A delisted holding must not be carried at its last price in silence.

        The engine keeps valuing it at the last known close, because inventing
        a liquidation would be inventing a trade. But it must say so loudly:
        the equity figure after that point is not trustworthy.
        """
        bars = [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:60]]
        # X stops printing after day 60; Y keeps the calendar running.
        bars += [make_bar("Y", d, D("50"), volume=10_000_000) for d in DAYS[:200]]
        history = PriceHistory.from_bars(bars)

        class BuyXOnce:
            strategy_id = "buy_x"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                from signalforge.core.interfaces import Signal

                if as_of != DAYS[5]:
                    return []
                return [
                    Signal(
                        symbol="X",
                        direction=SignalDirection.ENTER_LONG,
                        as_of=as_of,
                        strategy_id=self.strategy_id,
                        reason="enter",
                        target_weight=D("0.05"),
                    )
                ]

        result = engine().run(BuyXOnce(), history)
        assert result.fills, "setup failed: nothing was bought"
        stale_events = [e for e in result.risk_events if "stale_position" in e.rule]
        assert stale_events, "a position valued on a dead price must raise a risk event"
        assert any("stale" in note.lower() for note in result.data_limitations)

    def test_a_live_holding_raises_no_stale_valuation_event(self):
        history = PriceHistory.from_bars(
            [make_bar("X", d, D(100 + i), volume=10_000_000) for i, d in enumerate(DAYS[:200])]
        )

        class BuyOnce:
            strategy_id = "buy_once"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                from signalforge.core.interfaces import Signal

                if as_of != DAYS[5]:
                    return []
                return [
                    Signal(
                        symbol="X",
                        direction=SignalDirection.ENTER_LONG,
                        as_of=as_of,
                        strategy_id=self.strategy_id,
                        reason="enter",
                        target_weight=D("0.05"),
                    )
                ]

        result = engine().run(BuyOnce(), history)
        assert [e for e in result.risk_events if "stale_position" in e.rule] == []

    def test_the_event_is_raised_once_per_symbol_not_once_per_day(self):
        # A per-day event would bury the rest of the risk log in noise.
        bars = [make_bar("X", d, D("100"), volume=10_000_000) for d in DAYS[:60]]
        bars += [make_bar("Y", d, D("50"), volume=10_000_000) for d in DAYS[:200]]
        history = PriceHistory.from_bars(bars)

        class BuyXOnce:
            strategy_id = "buy_x"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                from signalforge.core.interfaces import Signal

                if as_of != DAYS[5]:
                    return []
                return [
                    Signal(
                        symbol="X",
                        direction=SignalDirection.ENTER_LONG,
                        as_of=as_of,
                        strategy_id=self.strategy_id,
                        reason="enter",
                        target_weight=D("0.05"),
                    )
                ]

        result = engine().run(BuyXOnce(), history)
        assert len([e for e in result.risk_events if "stale_position" in e.rule]) == 1


class TestSurvivorshipBiasIsDeclared:
    def test_a_cross_section_snapshot_run_declares_survivorship_bias(self):
        """A snapshot of today's listings contains no company that failed.

        This cannot be fixed with the data available, so the requirement is
        that it is never silently omitted.
        """
        bars = []
        for symbol in ("AAA", "BBB", "CCC"):
            bars.extend(
                make_bar(symbol, d, D("100"), volume=1_000_000) for d in DAYS[:4]
            )
        history = PriceHistory.from_bars(bars)

        class Nothing:
            strategy_id = "nothing"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                return []

        result = engine().run(
            Nothing(),
            history,
            data_limitations=[
                "SURVIVORSHIP BIAS: this cross-section is a snapshot of symbols "
                "listed today, so every company that delisted or failed is absent."
            ],
        )
        assert any("survivorship" in n.lower() for n in result.data_limitations)


class TestFingerprintCoversEveryFieldTheEngineActsOn:
    def test_two_datasets_differing_only_in_volume_known_fingerprint_differently(self):
        """volume_known changes whether two risk rules fire, so it must hash.

        If it did not, two experiments with materially different liquidity
        assumptions would claim the same dataset version.
        """
        known = PriceHistory.from_bars(
            [make_bar("X", d, D("100"), volume=0) for d in DAYS[:10]]
        )
        unknown = PriceHistory.from_bars(
            [
                Bar(
                    symbol="X",
                    trade_date=d,
                    open=D("100"),
                    high=D("100"),
                    low=D("100"),
                    close=D("100"),
                    volume=0,
                    adj_close=D("100"),
                    source="fixture",
                    volume_known=False,
                )
                for d in DAYS[:10]
            ]
        )
        a = BacktestEngine.fingerprint(known, DAYS[0], DAYS[9])
        b = BacktestEngine.fingerprint(unknown, DAYS[0], DAYS[9])
        assert a != b


class TestSeriesCoercion:
    def test_a_non_positive_observation_is_dropped_not_coerced(self):
        bundle = series_history(
            [
                (DAYS[0], D("100")),
                (DAYS[1], D("0")),
                (DAYS[2], D("-5")),
                (DAYS[3], D("102")),
            ],
            symbol="IDX",
        )
        assert len(bundle.history.bars_for("IDX")) == 2
        assert any("non-positive" in n for n in bundle.limitations)

    def test_an_all_non_positive_series_raises_rather_than_returning_nothing(self):
        with pytest.raises(Exception):
            series_history([(DAYS[0], D("0")), (DAYS[1], D("-1"))], symbol="IDX")
