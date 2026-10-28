"""Per-regime performance and selection bias must reach the page.

Both are now real computed results, and neither was rendered: the dashboard
called `compute_metrics` without regime labels, so `regimes` was empty, and
nothing displayed the multiple-testing correction at all.

A result that exists in the code and not on the page is the same problem as
one that was never computed, because the operator reads the page.
"""

import re
from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.backtesting.metrics import compute_metrics
from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.dashboard.sections import regime_table_html, selection_bias_html
from signalforge.pipeline import volatility_regime_labels

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2000, 1, 3), 700)


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


@pytest.fixture
def metrics_with_regimes():
    hist = PriceHistory.from_bars(
        [make_bar("X", d, D(100 + i), volume=1_000_000) for i, d in enumerate(DAYS)]
    )
    research = ResearchConfig(starting_cash=D("100000"), universe=("X",))
    result = BacktestEngine(research, RiskConfig()).run(Hold(), hist)
    vol = [(d, D(str(10 + (i * 13) % 35))) for i, d in enumerate(DAYS)]
    labels = volatility_regime_labels(vol, warmup=100)
    return compute_metrics(result, config=research, regime_labels=labels)


@pytest.fixture
def metrics_without_regimes():
    hist = PriceHistory.from_bars(
        [make_bar("X", d, D(100 + i), volume=1_000_000) for i, d in enumerate(DAYS[:300])]
    )
    research = ResearchConfig(starting_cash=D("100000"), universe=("X",))
    result = BacktestEngine(research, RiskConfig()).run(Hold(), hist)
    return compute_metrics(result, config=research)


class TestRegimeTable:
    def test_it_renders_a_row_per_regime(self, metrics_with_regimes):
        html = regime_table_html("r0", metrics_with_regimes)
        assert html.count('class="row"') == len(metrics_with_regimes.regimes)

    def test_each_regime_label_appears(self, metrics_with_regimes):
        html = regime_table_html("r0", metrics_with_regimes)
        for label in metrics_with_regimes.regimes:
            assert label in html

    def test_an_unclassified_run_says_so_with_a_reason(self, metrics_without_regimes):
        html = regime_table_html("r0", metrics_without_regimes)
        assert "DATA UNAVAILABLE" in html
        assert "not classified" in html.lower() or "no regime" in html.lower()

    def test_it_never_renders_a_blank_cell(self, metrics_with_regimes):
        html = regime_table_html("r0", metrics_with_regimes)
        assert "<td></td>" not in html
        assert "<td> </td>" not in html

    def test_a_none_sharpe_renders_as_not_available(self, metrics_with_regimes):
        import dataclasses

        regimes = dict(metrics_with_regimes.regimes)
        first = next(iter(regimes))
        regimes[first] = dataclasses.replace(regimes[first], sharpe=None)
        patched = dataclasses.replace(metrics_with_regimes, regimes=regimes)
        html = regime_table_html("r0", patched)
        assert "not available" in html.lower()

    def test_a_hostile_regime_label_is_escaped(self, metrics_with_regimes):
        import dataclasses

        regimes = dict(metrics_with_regimes.regimes)
        first = next(iter(regimes))
        hostile = "<script>alert(1)</script>"
        regimes[hostile] = dataclasses.replace(regimes.pop(first), label=hostile)
        patched = dataclasses.replace(metrics_with_regimes, regimes=regimes)
        html = regime_table_html("r0", patched)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_every_regime_carries_its_observation_count(self, metrics_with_regimes):
        html = regime_table_html("r0", metrics_with_regimes)
        for regime in metrics_with_regimes.regimes.values():
            assert str(regime.observations) in html

    def test_insufficient_evidence_is_shown_per_regime(self, metrics_with_regimes):
        # Most regimes will be thin; that has to be visible, not buried.
        html = regime_table_html("r0", metrics_with_regimes)
        flagged = [
            r for r in metrics_with_regimes.regimes.values()
            if r.evidence.value == "INSUFFICIENT_EVIDENCE"
        ]
        if flagged:
            assert "INSUFFICIENT_EVIDENCE" in html


class TestSelectionBiasPanel:
    def summary(self, n_variants, best, strategies=5):
        from signalforge.learning.overfitting import FamilyWiseSummary, deflated_sharpe

        dsr = deflated_sharpe(best, n_variants, 2967)
        return FamilyWiseSummary(
            n_variants=n_variants,
            n_experiments=15,
            n_strategies=strategies,
            best_strategy="xs_momentum",
            best_observed_sharpe=best,
            expected_max_sharpe_under_null=dsr.expected_max_sharpe,
            excess_over_null=best - dsr.expected_max_sharpe,
            deflated_sharpe=dsr,
            message="measured",
        )

    def test_it_states_the_variant_count_and_the_best_result(self):
        html = selection_bias_html(self.summary(5, 0.676))
        assert "5" in html
        assert "0.676" in html or "0.68" in html

    def test_a_failing_correction_is_stated_prominently(self):
        html = selection_bias_html(self.summary(5, 0.676))
        assert "<strong" in html
        assert "not" in html.lower()

    def test_a_passing_correction_is_labelled_differently(self):
        passing = selection_bias_html(self.summary(1, 0.676))
        failing = selection_bias_html(self.summary(5, 0.676))
        assert passing != failing

    def test_the_strategy_name_is_escaped(self):
        import dataclasses

        s = self.summary(5, 0.676)
        s = dataclasses.replace(s, best_strategy="<script>alert(1)</script>")
        html = selection_bias_html(s)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_an_empty_registry_renders_an_explicit_state(self):
        from signalforge.learning.overfitting import FamilyWiseSummary

        empty = FamilyWiseSummary(0, 0, 0, None, None, None, None, None, "none")
        html = selection_bias_html(empty)
        assert "DATA UNAVAILABLE" in html
        assert "no experiment" in html.lower() or "not" in html.lower()

    def test_it_explains_what_the_correction_means(self):
        # A number with no explanation invites the wrong reading.
        html = selection_bias_html(self.summary(5, 0.676))
        lowered = html.lower()
        assert "tried" in lowered
        assert "null" in lowered or "luck" in lowered or "random" in lowered
