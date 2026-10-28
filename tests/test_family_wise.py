"""Family-wise multiple testing: the denominator that actually applies.

`multiple_testing_summary` counts variants PER STRATEGY. On the real registry
every strategy had exactly one parameter set, so it reported 1 trial each and
no adjustment — while five strategies had in fact been tried and the best of
them reported.

The question a researcher actually asks is "did any of the things I tried
beat the benchmark", and its denominator is the number of things tried, not
one. Measured on the real registry: the best observed Sharpe of 0.676
survives as significant at 1 trial and fails at 4 or more, so the honest
answer depends entirely on using the right count.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.data.storage import Warehouse
from signalforge.learning.overfitting import family_wise_summary
from signalforge.learning.registry import ExperimentRegistry

from tests.conftest import business_days

D = Decimal
DAYS = business_days(date(2000, 1, 3), 400)


@pytest.fixture
def registry():
    wh = Warehouse(":memory:")
    try:
        yield ExperimentRegistry(wh), wh
    finally:
        wh.close()


def record(reg, strategy, sharpe, *, window="validation", params=None, fingerprint="fp"):
    from signalforge.config.models import SplitConfig

    splits = SplitConfig(
        train_start=DAYS[0],
        train_end=DAYS[100],
        validation_start=DAYS[101],
        validation_end=DAYS[200],
        test_start=DAYS[201],
        test_end=DAYS[-1],
    )
    return reg.record(
        strategy_name=strategy,
        strategy_version="0.1.0",
        parameters=params or {"w": "0.05"},
        dataset_fingerprint=fingerprint,
        dataset_description="d",
        window=window,
        splits=splits,
        transaction_cost_assumptions={"slippage_bps": "5"},
        engine_version="0.1.0",
        random_seed=1,
        hypothesis="h",
        metrics={"sharpe": str(sharpe), "observations": "1000"},
        max_drawdown=D("0.05"),
        trade_count=40,
        evidence_level="SUFFICIENT",
    )


class TestFamilyWiseSummary:
    def test_it_counts_every_distinct_variant_across_strategies(self, registry):
        reg, _ = registry
        record(reg, "a", "0.5")
        record(reg, "b", "0.6")
        record(reg, "c", "0.4")
        summary = family_wise_summary(reg)
        assert summary.n_variants == 3

    def test_the_same_variant_in_several_windows_counts_once(self, registry):
        reg, _ = registry
        for window in ("train", "validation"):
            record(reg, "a", "0.5", window=window)
        assert family_wise_summary(reg).n_variants == 1

    def test_two_parameter_sets_of_one_strategy_count_twice(self, registry):
        reg, _ = registry
        record(reg, "a", "0.5", params={"w": "0.05"})
        record(reg, "a", "0.6", params={"w": "0.10"})
        assert family_wise_summary(reg).n_variants == 2

    def test_it_reports_the_best_observed_sharpe_and_which_strategy(self, registry):
        reg, _ = registry
        record(reg, "a", "0.5")
        record(reg, "b", "0.68")
        summary = family_wise_summary(reg)
        assert float(summary.best_observed_sharpe) == pytest.approx(0.68)
        assert summary.best_strategy == "b"

    def test_a_single_variant_carries_no_multiple_testing_penalty(self):
        # The property under test is the PENALTY, not significance: whether
        # 0.68 clears the threshold also depends on the sample size, and at
        # 1,000 observations it does not (91.2%). The real registry has 2,967.
        wh = Warehouse(":memory:")
        try:
            reg = ExperimentRegistry(wh)
            record(reg, "a", "0.68")
            summary = family_wise_summary(reg)
            assert summary.n_variants == 1
            assert summary.expected_max_sharpe_under_null == 0.0
            assert summary.excess_over_null == pytest.approx(0.68)
        finally:
            wh.close()

    def test_more_variants_raise_the_bar_the_best_result_must_clear(self):
        wh = Warehouse(":memory:")
        try:
            reg = ExperimentRegistry(wh)
            for i, name in enumerate("abcdefgh"):
                record(reg, name, str(0.2 + i * 0.06))
            summary = family_wise_summary(reg)
            assert summary.n_variants == 8
            assert summary.expected_max_sharpe_under_null > 0.0, (
                "trying eight things must raise the bar above zero"
            )
            assert summary.excess_over_null < summary.best_observed_sharpe
        finally:
            wh.close()

    def test_several_variants_deflate_the_best_result(self, registry):
        reg, _ = registry
        for i, name in enumerate("abcde"):
            record(reg, name, str(0.3 + i * 0.1))
        summary = family_wise_summary(reg)
        assert summary.n_variants == 5
        single = family_wise_summary(reg, n_variants_override=1)
        assert (
            summary.deflated_sharpe.deflated_sharpe
            < single.deflated_sharpe.deflated_sharpe
        ), "trying more things must make the best of them less impressive"

    def test_the_message_names_both_numbers(self, registry):
        reg, _ = registry
        for name in "abcde":
            record(reg, name, "0.5")
        message = family_wise_summary(reg).message
        assert "5" in message

    def test_an_empty_registry_is_reported_not_crashed(self, registry):
        reg, _ = registry
        summary = family_wise_summary(reg)
        assert summary.n_variants == 0
        assert summary.best_observed_sharpe is None
        assert summary.deflated_sharpe is None
        assert "no experiment" in summary.message.lower()

    def test_a_registry_with_no_sharpe_values_is_reported_not_crashed(self, registry):
        reg, wh = registry
        record(reg, "a", "0.5")
        wh.con.execute("UPDATE experiments SET metrics = '{}'")
        summary = family_wise_summary(reg)
        assert summary.best_observed_sharpe is None
        assert "sharpe" in summary.message.lower()

    def test_it_can_be_restricted_to_one_window(self, registry):
        reg, _ = registry
        record(reg, "a", "0.9", window="train")
        record(reg, "b", "0.4", window="test")
        assert float(family_wise_summary(reg, window="test").best_observed_sharpe) == (
            pytest.approx(0.4)
        )
