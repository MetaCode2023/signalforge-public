"""Market-regime labelling from a real volatility series.

The brief requires performance reported by market regime. The metrics layer
supported it from the start, but nothing ever passed it labels, so `regimes`
came back as an empty dict on every real run — a required figure shipping
blank rather than broken.

Labels must be assigned from information available at the time. A tercile
boundary computed over the whole history is a look-ahead: in 1995 nobody knew
what the 1995-2026 VIX distribution would be. These tests pin an expanding
(point-in-time) classification instead.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.core.errors import ValidationError
from signalforge.pipeline import (
    VOLATILITY_REGIMES,
    volatility_regime_labels,
)

D = Decimal
START = date(2000, 1, 3)


def series(values, start=START):
    return [(start + timedelta(days=i), D(str(v))) for i, v in enumerate(values)]


class TestVolatilityRegimeLabels:
    def test_it_labels_every_date_after_the_warmup(self):
        obs = series(range(1, 400))  # 399 observations
        labels = volatility_regime_labels(obs, warmup=100)
        assert len(labels) == len(obs) - 100

    def test_the_warmup_period_is_left_unlabelled_rather_than_guessed(self):
        obs = series(range(1, 400))
        labels = volatility_regime_labels(obs, warmup=100)
        for day, _ in obs[:100]:
            assert day not in labels

    def test_every_label_is_one_of_the_declared_regimes(self):
        labels = volatility_regime_labels(series(range(1, 400)), warmup=50)
        assert set(labels.values()) <= set(VOLATILITY_REGIMES)

    def test_a_high_reading_lands_in_the_high_regime(self):
        # 300 calm readings then a spike: the spike must be classified high.
        values = [10.0] * 300 + [90.0]
        obs = series(values)
        labels = volatility_regime_labels(obs, warmup=100)
        assert labels[obs[-1][0]] == "high_vol"

    def test_a_low_reading_lands_in_the_low_regime(self):
        values = list(range(20, 320)) + [1.0]
        obs = series(values)
        labels = volatility_regime_labels(obs, warmup=100)
        assert labels[obs[-1][0]] == "low_vol"

    def test_classification_uses_only_past_observations(self):
        """The label for day T must not change when later data is appended.

        A tercile boundary computed over the whole series would move every
        historical label as new data arrived, which is look-ahead bias
        dressed up as a regime definition.
        """
        base = series([float(10 + (i * 7) % 40) for i in range(300)])
        extended = base + series(
            [500.0] * 50, start=base[-1][0] + timedelta(days=1)
        )
        early = volatility_regime_labels(base, warmup=100)
        late = volatility_regime_labels(extended, warmup=100)
        for day in early:
            assert early[day] == late[day], (
                f"label for {day} changed when later data was appended; the "
                "classification is looking ahead"
            )

    def test_a_constant_series_is_classified_without_dividing_by_zero(self):
        labels = volatility_regime_labels(series([20.0] * 300), warmup=100)
        assert len(labels) == 200
        assert set(labels.values()) <= set(VOLATILITY_REGIMES)

    def test_too_short_a_series_yields_no_labels_rather_than_raising(self):
        assert volatility_regime_labels(series([10.0] * 10), warmup=100) == {}

    def test_an_empty_series_is_safe(self):
        assert volatility_regime_labels([], warmup=10) == {}

    def test_a_negative_warmup_is_refused(self):
        with pytest.raises(ValidationError):
            volatility_regime_labels(series([10.0] * 50), warmup=-1)

    def test_non_positive_observations_are_ignored_not_coerced(self):
        obs = series([10.0] * 150)
        obs[50] = (obs[50][0], D("0"))
        labels = volatility_regime_labels(obs, warmup=100)
        assert obs[50][0] not in labels


class TestRegimesReachTheMetrics:
    def test_a_real_run_populates_the_regimes_mapping(self):
        from signalforge.backtesting.engine import BacktestEngine
        from signalforge.backtesting.metrics import compute_metrics
        from signalforge.config.models import ResearchConfig, RiskConfig
        from signalforge.core.history import PriceHistory
        from signalforge.core.interfaces import Signal, SignalDirection

        from tests.conftest import business_days, make_bar

        days = business_days(date(2000, 1, 3), 700)
        hist = PriceHistory.from_bars(
            [make_bar("X", d, D(100 + i), volume=1_000_000) for i, d in enumerate(days)]
        )

        class Hold:
            strategy_id = "hold"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                return [
                    Signal(
                        symbol="X",
                        direction=SignalDirection.ENTER_LONG,
                        as_of=as_of,
                        strategy_id="hold",
                        reason="hold",
                        target_weight=D("0.05"),
                    )
                ]

        research = ResearchConfig(starting_cash=D("100000"), universe=("X",))
        result = BacktestEngine(research, RiskConfig()).run(Hold(), hist)
        vol = [(d, D(str(10 + (i * 13) % 35))) for i, d in enumerate(days)]
        labels = volatility_regime_labels(vol, warmup=100)
        metrics = compute_metrics(result, config=research, regime_labels=labels)
        assert metrics.regimes, "regimes must not be empty when labels are supplied"
        for label, regime in metrics.regimes.items():
            assert regime.observations > 0
            assert regime.label == label

    def test_without_labels_the_mapping_is_empty_and_that_is_honest(self):
        # An empty mapping must mean "no labels supplied", never "no regimes
        # found", so the reporting layer can say which.
        from signalforge.backtesting.metrics import compute_metrics
        from signalforge.config.models import ResearchConfig
        from signalforge.core.interfaces import BacktestResult

        empty = BacktestResult(
            strategy_id="s",
            run_id="r",
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 2),
            starting_cash=D("100000"),
        )
        assert compute_metrics(empty, config=ResearchConfig()).regimes == {}
