"""Re-verification of the diagnostics that were taken on a delegated agent's word.

`STRATEGY_RESULTS.md` carried cost sensitivity, parameter sensitivity and
Monte Carlo figures from an agent's run. Only the walk-forward and the seed
reproducibility had been checked independently. Everything was re-run through
the current code, on the honest `series_history` path (`volume_known=False`,
no fabricated volume), and two things came out of it.

**ma_trend reproduces exactly. zscore does not.** Cost sensitivity, Monte
Carlo and the walk-forward all reproduce to the decimal for both ma_trend
variants and for buy-and-hold. All three disagree for zscore mean reversion:

| figure | reported | re-run |
|---|---|---|
| cost sweep, 0 bps | -2.55% | -2.8768% |
| cost sweep, 100 bps | -7.90% | -8.0983% |
| Monte Carlo closed trades | 65 | 63 |
| Monte Carlo terminal median | 97,333.98 | 96,939.62 |
| walk-forward positive folds | 21/38 | 17/38 |
| walk-forward aggregate return | -3.8% | -4.0116% |
| walk-forward trades | 132 | 121 |

The verdicts are unchanged -- zscore has no edge at any cost level and loses
money out of sample either way -- but the figures are not the current code's.
zscore is the only strategy that trades often enough for the round 5 risk
fixes (the martingale size pinning, the order-id reservations, the
fill-time reconciliation) to change its order flow, and the report's train
table already says 63 closed trades where its own Monte Carlo row says 65.
The most likely explanation is that the zscore diagnostics predate those
fixes and were not re-run, which is the thing this file exists to stop
happening again: the numbers below are pinned so a silent drift fails a test
rather than surviving into a report.

**Anchored and rolling walk-forward are identical out of sample.** Measured
on all four strategies: byte-identical aggregate return, Sharpe, max
drawdown, trade count, fill count and per-fold out-of-sample return. Only
`train_start` and `train_sessions` differ (1,260 fixed against 1,260
expanding to 10,584).

That is not a bug, but it does change what the walk-forward means. Nothing is
fitted per fold: `walk_forward` rebuilds the same fixed `StrategySpec` every
time and the out-of-sample run reads only the test window, so the training
window influences `train_metrics` and nothing else. The consistency and
aggregate figures are a time-stability measure on fixed parameters, not
evidence that re-selected parameters hold up. `anchored=True` also costs
about 4x the wall clock (39.6s against 10.4s for ma_trend 50/200, 402.6s
against 248.1s for zscore) for identical out-of-sample output.

Also run here for the first time: `bootstrap_metric` on real returns. On the
8,559-session train window the choice of resampling scheme changes a verdict.
ma_trend 20/100's annualised Sharpe is 0.4267 with an iid 95% interval of
[0.0823, 0.7929], which excludes zero; under the stationary block bootstrap
that keeps serial dependence it is [-0.0100, 0.9245], which does not. The iid
interval over-claims. Buy-and-hold's own Sharpe interval contains zero under
both schemes.
"""

import os
from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import load_all
from signalforge.core.history import PriceHistory
from signalforge.data.archive import load_benchmark_series
from signalforge.data.providers import DEFAULT_ARCHIVE_DIR, SERIES_FILES
from signalforge.learning.overfitting import (
    bootstrap_metric,
    cost_sensitivity,
    monte_carlo_trade_order,
    parameter_sensitivity,
)
from signalforge.learning.registry import StrategySpec
from signalforge.learning.walkforward import walk_forward
from signalforge.pipeline import series_history

from tests.conftest import business_days, make_bar

D = Decimal
SEED = 20261026
TRAIN_FINGERPRINT = "eda78f442b95"

SLIPPAGE_LEVELS = [D("0"), D("5"), D("10"), D("25"), D("50"), D("100")]


def spec(strategy_id, **params) -> StrategySpec:
    return StrategySpec(strategy_id, params)


@pytest.fixture(scope="module")
def index_history() -> PriceHistory:
    """NASDAQCOM from the archive, which fingerprints identically to the warehouse.

    Reading the CSV rather than the DuckDB file keeps the test independent of
    whether the warehouse has been built, and the fingerprint assertion below
    proves the two paths agree.
    """
    path = os.path.join(str(DEFAULT_ARCHIVE_DIR), f"{SERIES_FILES[0]}.csv")
    if not os.path.isfile(path):
        pytest.skip(f"{path} is licensed data and is not in the repo")
    return series_history(load_benchmark_series("NASDAQCOM"), symbol="NASDAQCOM").history


@pytest.fixture(scope="module")
def setup(index_history):
    cfg = load_all()
    return cfg.research, BacktestEngine(cfg.research, cfg.risk), index_history


MA_50_200 = {"fast_window": 50, "slow_window": 200,
             "target_weight": D("0.05"), "symbols": ("NASDAQCOM",)}
MA_20_100 = {"fast_window": 20, "slow_window": 100,
             "target_weight": D("0.05"), "symbols": ("NASDAQCOM",)}
ZSCORE = {"window": 20, "entry_z": D("2"), "exit_z": D("0"),
          "target_weight": D("0.05"), "max_holding_days": 20,
          "symbols": ("NASDAQCOM",)}


class TestTheDataIsTheDataTheReportsUsed:
    def test_the_archive_path_fingerprints_like_the_warehouse(self, setup):
        research, engine, history = setup
        assert len(history) == 14_048
        assert all(not b.volume_known for b in history.bars_for("NASDAQCOM"))
        fingerprint = engine.fingerprint(
            history, research.splits.train_start, research.splits.train_end
        )
        assert fingerprint.startswith(TRAIN_FINGERPRINT)


class TestCostSensitivityReproduces:
    """ma_trend both variants reproduce the reported sweep to two decimals."""

    @pytest.mark.parametrize(
        "params,first,last",
        [
            (MA_50_200, D("0.130869"), D("0.106665")),
            (MA_20_100, D("0.095051"), D("0.059151")),
        ],
    )
    def test_the_trend_sweep_reproduces(self, setup, params, first, last):
        research, engine, history = setup
        report = cost_sensitivity(
            engine, spec("ma_trend", **params), history, research,
            SLIPPAGE_LEVELS, [research.execution.commission_per_share],
        )
        rows = {r.slippage_bps: D(str(r.total_return)) for r in report.rows}
        assert rows[D("0")].quantize(D("0.000001")) == first
        assert rows[D("100")].quantize(D("0.000001")) == last
        assert report.breakeven_slippage_bps is None
        assert report.edge_survives_all_levels is True
        assert report.robust_to_10bps is True

    def test_the_sweep_is_monotone_in_cost(self, setup):
        """Higher slippage cannot improve a return. Nothing asserted this."""
        research, engine, history = setup
        report = cost_sensitivity(
            engine, spec("ma_trend", **MA_50_200), history, research,
            SLIPPAGE_LEVELS, [research.execution.commission_per_share],
        )
        rows = sorted(report.rows, key=lambda r: r.slippage_bps)
        returns = [r.total_return for r in rows]
        assert returns == sorted(returns, reverse=True)


class TestParameterSensitivityReproducesItsVerdict:
    """The verdict reproduces. The grid behind it was never recorded.

    `STRATEGY_RESULTS.md` said both ma_trend variants came back
    `fragile=False`, which is true under the grid below, but the report does
    not say which grid was used -- so the claim was not reproducible from the
    report alone. The grid is now in the test.
    """

    def test_ma_trend_50_200_is_not_fragile_on_a_one_notch_grid(self, setup):
        research, engine, history = setup
        report = parameter_sensitivity(
            engine, spec("ma_trend", **MA_50_200), history, research,
            {"fast_window": [20, 50, 100], "slow_window": [100, 200, 300]},
        )
        assert report.fragile is False
        assert report.fragile_points == []
        assert D(str(report.base_return)).quantize(D("0.000001")) == D("0.129852")
        assert len(report.points) == 5
        assert all(p.total_return > 0 for p in report.points)

    def test_zscore_has_no_edge_so_fragility_does_not_arise(self, setup):
        """And one neighbour is positive, which is a trial nobody counted.

        window=10 returns +0.7588% where the base returns -3.177%. It is the
        only positive zscore variant found anywhere in the project, and
        finding it is another trial against the family-wise denominator.
        """
        research, engine, history = setup
        report = parameter_sensitivity(
            engine, spec("zscore_mean_reversion", **ZSCORE), history, research,
            {"window": [10, 20, 40]},
        )
        assert report.fragile is False
        assert "no positive base return" in report.rule
        positive = [p for p in report.points if p.total_return > 0]
        assert [p.value for p in positive] == [10]


class TestMonteCarloReproduces:
    @pytest.mark.parametrize(
        "params,trades,median,p05,p95",
        [
            (MA_50_200, 18, 112392.89, 103146.98, 123849.12),
            (MA_20_100, 35, 109010.13, 101424.49, 118201.75),
        ],
    )
    def test_the_trend_figures_reproduce(self, setup, params, trades, median, p05, p95):
        research, engine, history = setup
        result = engine.run(
            spec("ma_trend", **params).build(), history,
            start=research.splits.train_start, end=research.splits.train_end,
        )
        mc = monte_carlo_trade_order(result, iterations=5000, seed=SEED, mode="bootstrap")
        assert mc.n_trades == trades
        assert round(mc.terminal_median, 2) == median
        assert round(mc.terminal_p05, 2) == p05
        assert round(mc.terminal_p95, 2) == p95

    def test_shuffling_cannot_move_terminal_equity(self, setup):
        """Stated in the docstring and in the report; never asserted on real data."""
        research, engine, history = setup
        result = engine.run(
            spec("ma_trend", **MA_50_200).build(), history,
            start=research.splits.train_start, end=research.splits.train_end,
        )
        mc = monte_carlo_trade_order(result, iterations=500, seed=SEED, mode="shuffle")
        assert mc.terminal_median == mc.terminal_p05 == mc.terminal_p95
        assert round(mc.terminal_median, 2) == round(mc.actual_terminal, 2)
        assert mc.drawdown_p05 < mc.drawdown_p95, "the PATH still varies"


class TestTheWalkForwardTableIsPinned:
    """ma_trend 50/200's rolling figures, so the table cannot drift unnoticed.

    zscore's are not pinned here: its rolling walk-forward takes a measured
    248 seconds and the anchored one 403, which is more than this suite
    should spend on one row. Its re-run figures are in the module docstring
    and in `STRATEGY_RESULTS.md` instead.
    """

    def test_ma_trend_50_200_rolling(self, setup):
        research, engine, history = setup
        report = walk_forward(
            engine, spec("ma_trend", **MA_50_200), history, research,
            train_sessions=1260, test_sessions=252, step_sessions=252,
            end=research.splits.validation_end,
        )
        assert report.mode == "rolling"
        assert len(report.folds) == 38
        assert len(report.skipped) == 1
        assert report.consistency.positive_folds == 24
        assert report.consistency.fold_count == 38
        aggregate = report.aggregate_metrics
        assert aggregate.total_return.quantize(D("0.000001")) == D("0.170750")
        assert aggregate.sharpe.quantize(D("0.0001")) == D("0.6163")
        assert aggregate.max_drawdown.quantize(D("0.000001")) == D("0.026951")
        assert aggregate.trade_count == 22
        assert aggregate.fill_count == 70

    def test_the_skipped_fold_says_why(self, setup):
        research, engine, history = setup
        report = walk_forward(
            engine, spec("ma_trend", **MA_50_200), history, research,
            train_sessions=1260, test_sessions=252, step_sessions=252,
            end=research.splits.validation_end,
        )
        reason = report.skipped[0].reason
        assert "241 remain" in reason
        assert "needs 252" in reason


class TestAnchoredAndRollingAgreeOutOfSample:
    """The structural finding, pinned so a change to it has to be deliberate.

    Nothing is fitted per fold, so the training window cannot influence the
    out-of-sample result. If a future version re-selects parameters per fold,
    this test fails -- and that failure is the signal to update every claim
    built on the walk-forward table, because those figures would then mean
    something different.
    """

    def _both_modes(self, engine, strategy, history, research, **kw):
        return {
            mode: walk_forward(
                engine, strategy, history, research,
                train_sessions=252, test_sessions=126, step_sessions=126,
                anchored=(mode == "anchored"), **kw,
            )
            for mode in ("rolling", "anchored")
        }

    def test_the_modes_differ_only_in_the_training_window(self, synthetic):
        cfg = load_all()
        engine = BacktestEngine(cfg.research, cfg.risk)
        reports = self._both_modes(
            engine,
            spec("ma_trend", fast_window=20, slow_window=60,
                 target_weight=D("0.05"), symbols=("SYN",)),
            synthetic, cfg.research,
        )
        rolling, anchored = reports["rolling"], reports["anchored"]
        assert len(rolling.folds) == len(anchored.folds) >= 3

        # The training windows really are different.
        assert [f.train_start for f in rolling.folds] != [
            f.train_start for f in anchored.folds
        ]
        assert {f.train_sessions for f in rolling.folds} == {252}
        assert max(f.train_sessions for f in anchored.folds) > 252

        # The out-of-sample results are not.
        assert [(f.test_start, f.test_end, f.test_metrics.total_return)
                for f in rolling.folds] == [
            (f.test_start, f.test_end, f.test_metrics.total_return)
            for f in anchored.folds
        ]
        assert (
            rolling.aggregate_metrics.total_return
            == anchored.aggregate_metrics.total_return
        )
        assert rolling.aggregate_metrics.sharpe == anchored.aggregate_metrics.sharpe
        assert (
            rolling.consistency.positive_folds == anchored.consistency.positive_folds
        )

    def test_on_the_real_index_too(self, setup):
        research, engine, history = setup
        reports = {
            mode: walk_forward(
                engine, spec("ma_trend", **MA_50_200), history, research,
                train_sessions=1260, test_sessions=252, step_sessions=252,
                anchored=(mode == "anchored"), end=research.splits.validation_end,
            )
            for mode in ("rolling", "anchored")
        }
        rolling, anchored = reports["rolling"], reports["anchored"]
        for field in ("total_return", "sharpe", "max_drawdown", "trade_count",
                      "fill_count"):
            assert getattr(rolling.aggregate_metrics, field) == getattr(
                anchored.aggregate_metrics, field
            ), field
        assert rolling.folds[-1].train_sessions == 1260
        assert anchored.folds[-1].train_sessions == 10_584


@pytest.fixture(scope="module")
def synthetic() -> PriceHistory:
    """A hermetic 2,000-session series with enough shape to trade."""
    days = business_days(date(2000, 1, 3), 2000)
    return PriceHistory.from_bars(
        [
            make_bar("SYN", day,
                     D(100) + D(index % 97) - D(index % 31) + D(index) / D(50),
                     volume=1_000_000)
            for index, day in enumerate(days)
        ]
    )


def annualised_sharpe(returns) -> float:
    values = np.asarray(returns, dtype=float)
    if values.size < 2:
        return float("nan")
    deviation = values.std(ddof=1)
    if deviation <= 0:
        return float("nan")
    return float(values.mean() / deviation * np.sqrt(252.0))


def daily_returns(curve) -> list[float]:
    return [
        float(curve[i].equity / curve[i - 1].equity - 1)
        for i in range(1, len(curve))
        if curve[i - 1].equity > 0
    ]


class TestBootstrapMetricOnRealReturns:
    """Never run on real data before. The resampling scheme changes a verdict."""

    def _returns(self, engine, research, history, params):
        result = engine.run(
            spec("ma_trend", **params).build(), history,
            start=research.splits.train_start, end=research.splits.train_end,
        )
        return daily_returns(result.equity_curve)

    def test_the_train_window_has_the_session_count_the_report_claims(self, setup):
        research, engine, history = setup
        assert len(self._returns(engine, research, history, MA_50_200)) == 8559

    def test_the_point_estimate_is_the_metric_not_a_resample(self, setup):
        research, engine, history = setup
        returns = self._returns(engine, research, history, MA_50_200)
        interval = bootstrap_metric(returns, annualised_sharpe, iterations=2000,
                                    seed=SEED, confidence=0.95)
        assert round(interval.point, 8) == 0.44652159
        assert interval.lower < interval.point < interval.upper
        assert interval.n == 8559

    def test_the_iid_interval_excludes_zero_and_the_block_one_does_not(self, setup):
        """The over-claim, on a real strategy.

        Daily equity returns are serially dependent, so an iid resample
        destroys exactly the structure that widens the interval. For
        ma_trend 20/100 that difference decides whether zero is inside it.
        """
        research, engine, history = setup
        returns = self._returns(engine, research, history, MA_20_100)
        iid = bootstrap_metric(returns, annualised_sharpe, iterations=2000,
                               seed=SEED, confidence=0.95)
        block = bootstrap_metric(returns, annualised_sharpe, iterations=2000,
                                 seed=SEED, confidence=0.95, block_length=20)
        assert round(iid.point, 8) == round(block.point, 8) == 0.42667827
        assert iid.lower > 0, iid.lower
        assert block.lower < 0, block.lower
        assert block.upper > iid.upper
        assert (block.upper - block.lower) > (iid.upper - iid.lower)

    def test_the_benchmarks_own_sharpe_interval_contains_zero(self, setup):
        research, engine, history = setup
        result = engine.run(
            spec("buy_and_hold", symbols=("NASDAQCOM",),
                 target_weight=D("0.05")).build(),
            history, start=research.splits.train_start, end=research.splits.train_end,
        )
        returns = daily_returns(result.equity_curve)
        for block in (None, 20):
            interval = bootstrap_metric(returns, annualised_sharpe, iterations=2000,
                                        seed=SEED, confidence=0.95, block_length=block)
            assert round(interval.point, 8) == 0.28446269
            assert interval.lower < 0 < interval.upper, block

    def test_it_is_seed_reproducible_and_seed_sensitive_on_real_data(self, setup):
        research, engine, history = setup
        returns = self._returns(engine, research, history, MA_50_200)
        first = bootstrap_metric(returns, annualised_sharpe, iterations=2000,
                                 seed=SEED, confidence=0.95)
        again = bootstrap_metric(returns, annualised_sharpe, iterations=2000,
                                 seed=SEED, confidence=0.95)
        other = bootstrap_metric(returns, annualised_sharpe, iterations=2000,
                                 seed=1, confidence=0.95)
        assert (first.lower, first.upper) == (again.lower, again.upper)
        assert (first.lower, first.upper) != (other.lower, other.upper)
        assert first.point == other.point, "the point estimate is not resampled"

    def test_a_short_real_window_hits_the_floor_instead_of_inventing_a_band(self, setup):
        research, engine, history = setup
        result = engine.run(
            spec("ma_trend", **MA_50_200).build(), history,
            start=research.splits.train_start, end=history.trading_days[14],
        )
        interval = bootstrap_metric(daily_returns(result.equity_curve),
                                    annualised_sharpe, iterations=2000,
                                    seed=SEED, confidence=0.95)
        assert interval.n == 14
        assert interval.lower is None and interval.upper is None
        assert "below the bootstrap floor of 20" in interval.note
