"""Walk-forward analysis.

The contract: folds are cut from the sessions the history really has, a fold's
test window is strictly after its train window, nothing silently disappears,
and the stitched out-of-sample curve covers each test session exactly once.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.metrics import EvidenceLevel
from signalforge.config.models import ResearchConfig
from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory
from signalforge.learning.walkforward import MIN_FOLDS, walk_forward

from tests.conftest import business_days, make_bar
from tests.test_learning_registry import ma_spec, make_engine, noisy_history

D = Decimal


@pytest.fixture(scope="module")
def history():
    return noisy_history(900)


@pytest.fixture(scope="module")
def config():
    return ResearchConfig()


def run_wf(history, config, **kw):
    kw.setdefault("train_sessions", 300)
    kw.setdefault("test_sessions", 100)
    kw.setdefault("step_sessions", 100)
    return walk_forward(make_engine(config), ma_spec(), history, config, **kw)


def test_fold_count_and_windows_come_from_actual_trading_days(history, config):
    report = run_wf(history, config)
    days = list(history.trading_days)
    assert len(report.folds) == 6
    for k, fold in enumerate(report.folds):
        assert fold.train_sessions == 300 and fold.test_sessions == 100
        assert fold.train_start == days[100 * k]
        assert fold.train_end == days[100 * k + 299]
        assert fold.test_start == days[100 * k + 300]
        assert fold.test_end == days[100 * k + 399]


def test_windows_use_real_sessions_not_calendar_days():
    days = [d for d in business_days(date(2020, 1, 1), 500) if d.day not in (3, 4, 5, 6, 7)]
    bars = [make_bar("SYN", d, D(100 + (i % 17))) for i, d in enumerate(days)]
    gappy = PriceHistory.from_bars(bars)
    report = run_wf(gappy, ResearchConfig(), train_sessions=120, test_sessions=40, step_sessions=40)
    for fold in report.folds:
        assert fold.test_sessions == 40
        assert fold.test_start in set(gappy.trading_days)
        assert fold.test_end in set(gappy.trading_days)
        in_window = [d for d in gappy.trading_days if fold.test_start <= d <= fold.test_end]
        assert len(in_window) == 40


@pytest.mark.parametrize("anchored", [False, True])
def test_train_and_test_are_disjoint_and_ordered_in_every_fold(history, config, anchored):
    report = run_wf(history, config, anchored=anchored)
    assert report.folds
    for fold in report.folds:
        assert fold.train_start <= fold.train_end < fold.test_start <= fold.test_end
    for a, b in zip(report.folds, report.folds[1:]):
        assert b.test_start > a.test_start


def test_rolling_train_window_has_constant_length_and_slides(history, config):
    report = run_wf(history, config, anchored=False)
    assert {f.train_sessions for f in report.folds} == {300}
    assert len({f.train_start for f in report.folds}) == len(report.folds)


def test_anchored_train_window_expands_from_the_first_session(history, config):
    report = run_wf(history, config, anchored=True)
    assert {f.train_start for f in report.folds} == {history.trading_days[0]}
    sizes = [f.train_sessions for f in report.folds]
    assert sizes == sorted(sizes) and sizes[0] == 300 and sizes[-1] == 800


def test_each_fold_reports_train_and_out_of_sample_metrics(history, config):
    report = run_wf(history, config)
    for fold in report.folds:
        assert fold.train_metrics.observations == 300
        assert fold.test_metrics.observations == 100
        assert fold.oos_return == fold.test_metrics.total_return


def test_fold_oos_result_equals_running_that_window_directly(history, config):
    report = run_wf(history, config)
    fold = report.folds[2]
    engine = make_engine(config)
    direct = engine.run(ma_spec().build(), history, start=fold.test_start, end=fold.test_end)
    assert direct.final_equity / direct.equity_curve[0].equity - 1 == fold.oos_return


def test_too_short_final_fold_is_skipped_with_a_reason_not_dropped():
    report = run_wf(noisy_history(850), ResearchConfig())
    assert len(report.folds) == 5
    assert len(report.skipped) == 1
    skipped = report.skipped[0]
    assert "too few sessions" in skipped.reason
    assert "50" in skipped.reason and "100" in skipped.reason
    assert skipped.test_start is not None


def test_history_shorter_than_one_train_window_yields_no_folds_and_says_why():
    report = run_wf(noisy_history(250), ResearchConfig())
    assert report.folds == []
    assert report.skipped and "train" in report.skipped[0].reason
    assert report.stitched_curve == []
    assert report.aggregate_metrics is None
    assert report.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE


def test_stitched_curve_covers_each_test_session_once_in_order(history, config):
    report = run_wf(history, config)
    dates = [p.valuation_date for p in report.stitched_curve]
    assert dates == sorted(set(dates))
    assert len(dates) == sum(f.test_sessions for f in report.folds) == 600
    assert dates[0] == report.folds[0].test_start and dates[-1] == report.folds[-1].test_end


def test_stitched_equity_compounds_the_fold_returns(history, config):
    report = run_wf(history, config)
    expected = config.starting_cash
    for fold in report.folds:
        expected *= 1 + fold.oos_return
    # Each fold's first-day move is not measured (the book is re-seeded on the
    # fold's first valuation), so equality is with the in-fold compounding.
    assert report.stitched_curve[-1].equity == pytest.approx(expected, rel=D("0.01"))
    assert report.stitched_curve[0].equity == config.starting_cash


def test_stitched_curve_drawdown_fields_are_consistent(history, config):
    report = run_wf(history, config)
    peak = Decimal(0)
    for p in report.stitched_curve:
        peak = max(peak, p.equity)
        assert p.peak_equity == peak
        assert p.drawdown == (peak - p.equity) / peak


def test_overlapping_test_windows_do_not_double_count_sessions(history, config):
    report = run_wf(history, config, test_sessions=100, step_sessions=50)
    dates = [p.valuation_date for p in report.stitched_curve]
    assert dates == sorted(set(dates))


def test_aggregate_metrics_are_computed_on_the_stitched_curve(history, config):
    report = run_wf(history, config)
    assert report.aggregate_metrics.observations == len(report.stitched_curve)


def test_consistency_is_the_fraction_of_positive_oos_folds(history, config):
    report = run_wf(history, config)
    positives = sum(1 for f in report.folds if f.oos_return is not None and f.oos_return > 0)
    c = report.consistency
    assert c.fold_count == len(report.folds)
    assert c.positive_folds == positives
    assert c.fraction == Decimal(positives) / Decimal(len(report.folds))


def test_fewer_than_three_folds_is_flagged_insufficient_evidence(history, config):
    report = run_wf(history, config, train_sessions=300, test_sessions=300, step_sessions=300)
    assert len(report.folds) == 2 < MIN_FOLDS
    assert report.consistency.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE
    assert report.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE
    assert "2" in report.consistency.note and str(MIN_FOLDS) in report.consistency.note


def test_three_or_more_folds_is_not_flagged(history, config):
    report = run_wf(history, config)
    assert report.consistency.evidence is EvidenceLevel.SUFFICIENT
    assert report.evidence is EvidenceLevel.SUFFICIENT


def test_no_folds_gives_no_fraction(config):
    report = run_wf(noisy_history(250), config)
    assert report.consistency.fraction is None
    assert report.consistency.fold_count == 0


def test_walk_forward_is_deterministic(history, config):
    a = run_wf(history, config)
    b = run_wf(history, config)
    assert [f.oos_return for f in a.folds] == [f.oos_return for f in b.folds]


def test_restricting_the_range_keeps_folds_inside_it(history, config):
    days = history.trading_days
    report = run_wf(history, config, start=days[0], end=days[599])
    assert report.folds[-1].test_end <= days[599]


@pytest.mark.parametrize("kw", [
    {"train_sessions": 0}, {"test_sessions": 0}, {"step_sessions": 0}, {"train_sessions": -5},
])
def test_non_positive_session_counts_are_rejected(history, config, kw):
    with pytest.raises(ValidationError):
        run_wf(history, config, **kw)
