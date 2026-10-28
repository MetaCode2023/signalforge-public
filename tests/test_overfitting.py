"""Overfitting diagnostics.

Every diagnostic here exists to answer one question: is this edge real, or is it
what you get from looking at enough variants? Rules that decide a verdict are
pinned with stubbed scores so the verdict cannot drift; the same functions are
also run against the real engine so the plumbing is exercised end to end.
"""

import math
from datetime import date
from decimal import Decimal
from statistics import NormalDist

import pytest

from signalforge.backtesting.metrics import EvidenceLevel
from signalforge.config.models import ResearchConfig
from signalforge.core.errors import ValidationError
from signalforge.data.storage import Warehouse
from signalforge.learning import overfitting
from signalforge.learning.experiments import tune
from signalforge.learning.overfitting import (
    MIN_BOOTSTRAP_SAMPLE,
    Score,
    bootstrap_metric,
    cost_sensitivity,
    deflated_sharpe,
    monte_carlo_trade_order,
    multiple_testing_summary,
    parameter_sensitivity,
)
from signalforge.learning.registry import ExperimentRegistry

from tests.test_learning_registry import (
    ma_spec,
    make_engine,
    noisy_history,
    record_kwargs,
    split_config,
)

D = Decimal


@pytest.fixture(scope="module")
def history():
    return noisy_history(900)


# ------------------------------------------------------- parameter sensitivity
def stub_by_fast(monkeypatch, returns_by_fast, calls=None):
    def fake(engine, spec, history, config, start, end, execution=None):
        if calls is not None:
            calls.append((spec.params["fast_window"], start, end))
        return Score(total_return=returns_by_fast[spec.params["fast_window"]], sharpe=None)

    monkeypatch.setattr(overfitting, "_score", fake)


def sens(history, grid, config=None):
    config = config or ResearchConfig()
    return parameter_sensitivity(make_engine(config), ma_spec(), history, config, grid)


def test_stable_neighbourhood_is_not_fragile(monkeypatch, history):
    stub_by_fast(monkeypatch, {3: 0.09, 5: 0.10, 7: 0.11})
    report = sens(history, {"fast_window": [3, 5, 7]})
    assert report.fragile is False
    assert "stable" in report.rule.lower()
    assert report.base_return == pytest.approx(0.10)


def test_a_neighbour_that_flips_sign_is_fragile_and_the_rule_says_so(monkeypatch, history):
    stub_by_fast(monkeypatch, {3: -0.02, 5: 0.10, 7: 0.11})
    report = sens(history, {"fast_window": [3, 5, 7]})
    assert report.fragile is True
    assert "fast_window=3" in report.rule and "non-positive" in report.rule
    assert [p.value for p in report.fragile_points] == [3]


def test_a_neighbour_that_loses_over_half_the_return_is_fragile(monkeypatch, history):
    stub_by_fast(monkeypatch, {3: 0.10, 5: 0.10, 7: 0.04})
    report = sens(history, {"fast_window": [3, 5, 7]})
    assert report.fragile is True
    assert "50%" in report.rule


def test_a_base_without_positive_return_has_no_edge_to_be_fragile(monkeypatch, history):
    stub_by_fast(monkeypatch, {3: 0.05, 5: -0.05, 7: -0.01})
    report = sens(history, {"fast_window": [3, 5, 7]})
    assert report.fragile is False
    assert "no positive base return" in report.rule


def test_only_one_notch_neighbours_are_run(monkeypatch, history):
    calls = []
    stub_by_fast(monkeypatch, {3: 0.1, 5: 0.1, 7: 0.1, 9: 0.1}, calls)
    sens(history, {"fast_window": [3, 5, 7, 9]})
    assert sorted(c[0] for c in calls) == [3, 5, 7]


def test_dispersion_is_reported(monkeypatch, history):
    stub_by_fast(monkeypatch, {3: 0.08, 5: 0.10, 7: 0.12})
    report = sens(history, {"fast_window": [3, 5, 7]})
    assert report.mean_return == pytest.approx(0.10)
    assert report.min_return == pytest.approx(0.08) and report.max_return == pytest.approx(0.12)
    assert report.std_return == pytest.approx(0.02)


def test_base_value_must_be_on_the_grid(monkeypatch, history):
    stub_by_fast(monkeypatch, {3: 0.1, 5: 0.1, 7: 0.1, 4: 0.1})
    with pytest.raises(ValidationError, match="fast_window"):
        sens(history, {"fast_window": [3, 4, 7]})


def test_sensitivity_defaults_to_the_train_window(monkeypatch, history):
    calls = []
    stub_by_fast(monkeypatch, {3: 0.1, 5: 0.1, 7: 0.1}, calls)
    config = split_config()
    sens(history, {"fast_window": [3, 5, 7]}, config)
    assert {(c[1], c[2]) for c in calls} == {(config.splits.train_start, config.splits.train_end)}


def test_diagnostics_refuse_to_read_the_test_window(monkeypatch, history):
    stub_by_fast(monkeypatch, {3: 0.1, 5: 0.1, 7: 0.1})
    config = split_config()
    with pytest.raises(ValidationError, match="test"):
        parameter_sensitivity(
            make_engine(config), ma_spec(), history, config, {"fast_window": [3, 5, 7]},
            start=date(2022, 6, 1), end=date(2023, 3, 1),
        )


def test_parameter_sensitivity_against_the_real_engine(history):
    report = sens(history, {"fast_window": [3, 5, 7], "slow_window": [15, 20, 30]})
    assert len(report.points) == 1 + 2 + 2
    assert isinstance(report.fragile, bool) and report.rule


# ---------------------------------------------------------- cost sensitivity
def stub_cost(monkeypatch, fn):
    def fake(engine, spec, history, config, start, end, execution=None):
        ex = execution or {}
        return Score(total_return=fn(float(ex.get("slippage_bps", 0)), float(ex.get("commission_per_share", 0))),
                     sharpe=None)

    monkeypatch.setattr(overfitting, "_score", fake)


def costs(history, slip, comm=(D("0.005"),)):
    config = ResearchConfig()
    return cost_sensitivity(make_engine(config), ma_spec(), history, config, slip, comm)


def test_breakeven_slippage_is_interpolated_between_the_last_winner_and_first_loser(monkeypatch, history):
    stub_cost(monkeypatch, lambda s, c: 0.06 - 0.008 * s)
    report = costs(history, [D(0), D(5), D(10), D(20)])
    assert report.breakeven_slippage_bps == pytest.approx(7.5)
    assert report.edge_survives_all_levels is False
    assert report.robust_to_10bps is False


def test_edge_that_survives_every_tested_level_has_no_breakeven(monkeypatch, history):
    stub_cost(monkeypatch, lambda s, c: 0.5 - 0.001 * s)
    report = costs(history, [D(0), D(5), D(10), D(20)])
    assert report.breakeven_slippage_bps is None
    assert report.edge_survives_all_levels is True
    assert report.robust_to_10bps is True
    assert "survives" in report.note


def test_an_edge_that_is_already_dead_at_the_lowest_cost_is_called_out(monkeypatch, history):
    stub_cost(monkeypatch, lambda s, c: -0.01 - 0.001 * s)
    report = costs(history, [D(1), D(5)])
    assert report.edge_at_lowest_cost is False
    assert report.breakeven_slippage_bps == pytest.approx(1)
    assert "no edge" in report.note.lower()


def test_edge_dying_at_exactly_ten_bps_is_not_robust(monkeypatch, history):
    stub_cost(monkeypatch, lambda s, c: 0.01 - 0.001 * s)
    report = costs(history, [D(0), D(5), D(10), D(20)])
    assert report.breakeven_slippage_bps == pytest.approx(10)
    assert report.robust_to_10bps is False


def test_every_cost_combination_is_a_row(monkeypatch, history):
    stub_cost(monkeypatch, lambda s, c: 0.1 - 0.001 * s - c)
    report = costs(history, [D(0), D(5)], comm=(D("0.005"), D("0.02")))
    assert len(report.rows) == 4
    assert set(report.breakeven_by_commission) == {"0.005", "0.02"}


def test_higher_slippage_never_improves_the_real_engine_result(history):
    config = ResearchConfig()
    report = cost_sensitivity(
        make_engine(config), ma_spec(), history, config, [D(0), D(5), D(25), D(100)], (D("0.005"),)
    )
    returns = [r.total_return for r in report.rows]
    assert returns == sorted(returns, reverse=True)


def test_cost_levels_must_be_non_empty_and_non_negative(history):
    config = ResearchConfig()
    with pytest.raises(ValidationError):
        cost_sensitivity(make_engine(config), ma_spec(), history, config, [], (D("0.005"),))
    with pytest.raises(ValidationError):
        cost_sensitivity(make_engine(config), ma_spec(), history, config, [D(-1)], (D("0.005"),))


# ------------------------------------------------------------- monte carlo
class FakeResult:
    starting_cash = D("100000")
    fills = []


def with_pnls(monkeypatch, pnls):
    monkeypatch.setattr(overfitting, "_trade_pnls", lambda result: [float(p) for p in pnls])
    return FakeResult()


def real_result(history):
    config = ResearchConfig()
    return make_engine(config).run(ma_spec(3, 12).build(), history)


def test_monte_carlo_is_reproducible_for_a_seed(history):
    result = real_result(history)
    a = monte_carlo_trade_order(result, 500, seed=11, mode="bootstrap")
    b = monte_carlo_trade_order(result, 500, seed=11, mode="bootstrap")
    c = monte_carlo_trade_order(result, 500, seed=12, mode="bootstrap")
    assert a == b
    assert a != c


def test_shuffle_changes_the_path_but_never_the_terminal_equity(monkeypatch):
    result = with_pnls(monkeypatch, [500, -300, 200, -100, 400, -250, 150, -50, 300, -400])
    mc = monte_carlo_trade_order(result, 300, seed=1)
    expected = 100000 + sum([500, -300, 200, -100, 400, -250, 150, -50, 300, -400])
    assert mc.terminal_p05 == pytest.approx(expected)
    assert mc.terminal_median == pytest.approx(expected)
    assert mc.terminal_p95 == pytest.approx(expected)
    assert mc.actual_terminal == pytest.approx(expected)
    assert mc.drawdown_p05 <= mc.drawdown_median <= mc.drawdown_p95
    assert mc.drawdown_p95 > mc.drawdown_p05
    assert "does not change the terminal" in mc.note


def test_bootstrap_gives_a_terminal_distribution(monkeypatch):
    result = with_pnls(monkeypatch, [500, -300, 200, -100, 400, -250, 150, -50, 300, -400])
    mc = monte_carlo_trade_order(result, 1000, seed=3, mode="bootstrap")
    assert mc.terminal_p05 < mc.terminal_median < mc.terminal_p95
    assert mc.n_trades == 10 and mc.iterations == 1000 and mc.seed == 3
    assert 0.0 <= mc.probability_of_loss <= 1.0


def test_all_winning_trades_never_lose_or_draw_down(monkeypatch):
    result = with_pnls(monkeypatch, [100] * 25)
    mc = monte_carlo_trade_order(result, 200, seed=1, mode="bootstrap")
    assert mc.probability_of_loss == 0.0
    assert mc.drawdown_p95 == 0.0
    assert mc.terminal_median == pytest.approx(100000 + 2500)


def test_all_losing_trades_always_lose(monkeypatch):
    result = with_pnls(monkeypatch, [-100] * 25)
    mc = monte_carlo_trade_order(result, 200, seed=1)
    assert mc.probability_of_loss == 1.0


def test_too_few_trades_gives_no_distribution_rather_than_a_fake_one(monkeypatch):
    result = with_pnls(monkeypatch, [100])
    mc = monte_carlo_trade_order(result, 200, seed=1)
    assert mc.terminal_median is None and mc.drawdown_p95 is None
    assert "trade" in mc.note and mc.n_trades == 1


def test_monte_carlo_validates_its_inputs(monkeypatch):
    result = with_pnls(monkeypatch, [1, 2, 3])
    with pytest.raises(ValidationError):
        monte_carlo_trade_order(result, 0, seed=1)
    with pytest.raises(ValidationError):
        monte_carlo_trade_order(result, 10, seed=1, mode="teleport")


def test_monte_carlo_reads_real_closed_trades(history):
    result = real_result(history)
    mc = monte_carlo_trade_order(result, 200, seed=5)
    assert mc.n_trades > 5
    assert mc.actual_terminal == pytest.approx(
        float(result.starting_cash) + sum(overfitting._trade_pnls(result))
    )
    assert mc.terminal_median == pytest.approx(mc.actual_terminal)


# ---------------------------------------------------------------- bootstrap
def mean(x):
    return float(sum(x) / len(x))


def sample(n=200):
    import random

    rng = random.Random(4)
    return [rng.gauss(0.1, 1.0) for _ in range(n)]


def test_bootstrap_interval_brackets_the_point_estimate():
    ci = bootstrap_metric(sample(), mean, iterations=500, seed=1, confidence=0.95)
    assert ci.lower < ci.point < ci.upper
    assert ci.n == 200 and ci.confidence == 0.95


def test_bootstrap_is_reproducible_for_a_seed():
    a = bootstrap_metric(sample(), mean, iterations=300, seed=9, confidence=0.9)
    b = bootstrap_metric(sample(), mean, iterations=300, seed=9, confidence=0.9)
    assert a == b


def test_higher_confidence_gives_a_wider_interval():
    narrow = bootstrap_metric(sample(), mean, iterations=800, seed=2, confidence=0.80)
    wide = bootstrap_metric(sample(), mean, iterations=800, seed=2, confidence=0.99)
    assert (wide.upper - wide.lower) > (narrow.upper - narrow.lower)


def test_a_sample_below_the_floor_returns_none_bounds_not_an_interval():
    ci = bootstrap_metric(sample(MIN_BOOTSTRAP_SAMPLE - 1), mean, iterations=300, seed=1, confidence=0.95)
    assert ci.lower is None and ci.upper is None
    assert str(MIN_BOOTSTRAP_SAMPLE) in ci.note
    assert ci.point is not None


def test_a_sample_at_the_floor_is_allowed():
    ci = bootstrap_metric(sample(MIN_BOOTSTRAP_SAMPLE), mean, iterations=300, seed=1, confidence=0.95)
    assert ci.lower is not None


def test_constant_returns_give_a_degenerate_interval():
    ci = bootstrap_metric([0.01] * 50, mean, iterations=200, seed=1, confidence=0.95)
    assert ci.lower == ci.upper == pytest.approx(0.01)


def test_stationary_bootstrap_is_reproducible_and_brackets():
    a = bootstrap_metric(sample(), mean, iterations=300, seed=5, confidence=0.95, block_length=5)
    b = bootstrap_metric(sample(), mean, iterations=300, seed=5, confidence=0.95, block_length=5)
    assert a == b
    assert a.lower < a.point < a.upper


def test_a_metric_that_is_mostly_undefined_gives_none_bounds():
    ci = bootstrap_metric(sample(), lambda x: float("nan"), iterations=100, seed=1, confidence=0.95)
    assert ci.lower is None and ci.upper is None
    assert "undefined" in ci.note


@pytest.mark.parametrize("bad", [0, 1, -0.2, 1.5])
def test_confidence_must_be_strictly_between_zero_and_one(bad):
    with pytest.raises(ValidationError):
        bootstrap_metric(sample(), mean, iterations=100, seed=1, confidence=bad)


# -------------------------------------------------------------- deflated sharpe
def psr(sr_annual, t, ppy=252):
    sr = sr_annual / math.sqrt(ppy)
    return NormalDist().cdf(sr * math.sqrt(t - 1) / math.sqrt(1 + sr * sr / 2))


def test_one_trial_reduces_to_the_probabilistic_sharpe_ratio():
    out = deflated_sharpe(2.0, 1, 504)
    assert out.expected_max_sharpe == 0.0
    assert out.deflated_sharpe == pytest.approx(psr(2.0, 504))
    assert out.significant is True


def test_fifty_variants_turns_a_headline_sharpe_into_noise():
    one = deflated_sharpe(2.0, 1, 504)
    fifty = deflated_sharpe(2.0, 50, 504)
    assert fifty.deflated_sharpe < one.deflated_sharpe
    assert fifty.significant is False
    assert fifty.expected_max_sharpe > 1.0
    assert "50" in fifty.message and "not a discovery" in fifty.message


def test_expected_max_sharpe_rises_with_trials():
    values = [deflated_sharpe(1.5, n, 504).expected_max_sharpe for n in (1, 2, 10, 100, 1000)]
    assert values == sorted(values) and len(set(values)) == 5


def test_deflated_sharpe_falls_as_trials_rise():
    values = [deflated_sharpe(1.5, n, 504).deflated_sharpe for n in (1, 5, 50, 500)]
    assert values == sorted(values, reverse=True)


def test_more_observations_raise_confidence():
    assert deflated_sharpe(1.0, 10, 2000).deflated_sharpe > deflated_sharpe(1.0, 10, 200).deflated_sharpe


def test_periods_per_year_is_a_pure_unit_conversion():
    annual = deflated_sharpe(1.5, 20, 504)
    per_period = deflated_sharpe(1.5 / math.sqrt(252), 20, 504, periods_per_year=1)
    assert per_period.deflated_sharpe == pytest.approx(annual.deflated_sharpe)


def test_negative_skew_and_fat_tails_lower_the_deflated_sharpe():
    normal = deflated_sharpe(1.5, 10, 504)
    ugly = deflated_sharpe(1.5, 10, 504, skewness=-1.5, kurtosis=9)
    assert ugly.deflated_sharpe < normal.deflated_sharpe


def test_a_negative_sharpe_is_never_significant():
    assert deflated_sharpe(-0.5, 5, 504).significant is False


@pytest.mark.parametrize("trials,obs", [(0, 100), (-1, 100), (5, 1), (5, 0)])
def test_deflated_sharpe_validates_inputs(trials, obs):
    with pytest.raises(ValidationError):
        deflated_sharpe(1.0, trials, obs)


# -------------------------------------------------- multiple testing summary
@pytest.fixture
def registry():
    w = Warehouse(":memory:")
    yield ExperimentRegistry(w)
    w.close()


def add(registry, params, sharpe, window="train", costs="5", observations=504, name="ma_trend"):
    return registry.record(**record_kwargs(
        strategy_name=name, parameters=params, window=window,
        transaction_cost_assumptions={"slippage_bps": costs},
        metrics={"sharpe": None if sharpe is None else str(sharpe), "observations": observations},
    ))


def test_summary_counts_the_variants_actually_in_the_registry(registry):
    for fast in (3, 5, 7, 9):
        add(registry, {"fast_window": fast}, 0.5 + fast / 20)
    s = multiple_testing_summary(registry, "ma_trend")
    assert s.n_variants == 4 and s.n_experiments == 4
    assert s.best_observed_sharpe == pytest.approx(0.95)
    assert s.expected_max_sharpe_under_null > 0
    assert s.excess_over_null == pytest.approx(s.best_observed_sharpe - s.expected_max_sharpe_under_null)


def test_rerunning_the_same_variant_does_not_inflate_the_count(registry):
    add(registry, {"fast_window": 3}, 0.8)
    add(registry, {"fast_window": 3}, 0.8, costs="9")
    add(registry, {"fast_window": 3}, 0.8, window="validation")
    s = multiple_testing_summary(registry, "ma_trend")
    assert s.n_variants == 1 and s.n_experiments == 3


def test_other_strategies_are_not_counted(registry):
    add(registry, {"a": 1}, 0.8)
    add(registry, {"a": 1}, 0.8, name="other")
    assert multiple_testing_summary(registry, "other").n_variants == 1
    assert multiple_testing_summary(registry, "ma_trend").n_variants == 1


def test_more_variants_imply_more_selection_bias(registry):
    add(registry, {"fast_window": 1}, 1.0)
    few = multiple_testing_summary(registry, "ma_trend")
    for fast in range(2, 40):
        add(registry, {"fast_window": fast}, 0.2)
    many = multiple_testing_summary(registry, "ma_trend")
    assert many.expected_max_sharpe_under_null > few.expected_max_sharpe_under_null
    assert many.deflated_sharpe.deflated_sharpe < few.deflated_sharpe.deflated_sharpe
    assert "39" in many.message


def test_summary_with_nothing_recorded_says_so(registry):
    s = multiple_testing_summary(registry, "ma_trend")
    assert s.n_variants == 0 and s.best_observed_sharpe is None and s.deflated_sharpe is None
    assert "no experiments" in s.message.lower()


def test_summary_survives_experiments_with_undefined_sharpe(registry):
    add(registry, {"fast_window": 3}, None)
    s = multiple_testing_summary(registry, "ma_trend")
    assert s.n_variants == 1 and s.best_observed_sharpe is None
    assert "undefined" in s.message.lower()


def test_summary_reads_the_count_made_by_a_real_tune(registry, history):
    config = split_config()
    tune(make_engine(config), ma_spec(), history, config, registry,
         grid={"fast_window": [3, 5], "slow_window": [20, 30]}, hypothesis="grid")
    assert multiple_testing_summary(registry, "ma_trend").n_variants == 4
