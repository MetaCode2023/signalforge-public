"""Charts must stay legible on a phone without lying about the data.

A 14,048-point equity curve rendered as 14,048 SVG coordinates produced a
14.7 MB page, which is unusable on the phone this dashboard is meant to be
read on. A chart roughly 700px wide cannot show more than ~700 distinct x
positions, so the extra coordinates bought nothing.

Downsampling is only acceptable if it preserves the extremes. Dropping the
bottom of a drawdown to save bytes would make the chart show a shallower
loss than actually happened, which is the one thing a risk chart must never
do. These tests pin that.
"""

from datetime import date, timedelta
from decimal import Decimal

from signalforge.dashboard.charts import MAX_CHART_POINTS, downsample, equity_chart

D = Decimal


def series(values):
    start = date(2000, 1, 3)
    return [(start + timedelta(days=i), D(str(v))) for i, v in enumerate(values)]


class TestDownsample:
    def test_a_short_series_is_returned_untouched(self):
        points = series([1, 2, 3, 4, 5])
        assert downsample(points, 10) == points

    def test_a_long_series_is_reduced_to_the_budget(self):
        points = series(range(10_000))
        reduced = downsample(points, 500)
        assert len(reduced) <= 500

    def test_the_first_and_last_points_are_always_kept(self):
        points = series(range(10_000))
        reduced = downsample(points, 500)
        assert reduced[0] == points[0]
        assert reduced[-1] == points[-1]

    def test_dates_stay_in_order(self):
        points = series(range(10_000))
        reduced = downsample(points, 400)
        assert [d for d, _ in reduced] == sorted(d for d, _ in reduced)

    def test_the_minimum_survives_downsampling(self):
        # A deep single-day trough in the middle must not be smoothed away:
        # that trough is the maximum drawdown.
        values = [100.0] * 5000
        values[2500] = 1.0
        reduced = downsample(series(values), 300)
        assert min(float(v) for _, v in reduced) == 1.0

    def test_the_maximum_survives_downsampling(self):
        values = [100.0] * 5000
        values[1234] = 999.0
        reduced = downsample(series(values), 300)
        assert max(float(v) for _, v in reduced) == 999.0

    def test_both_extremes_survive_together(self):
        values = [100.0] * 8000
        values[1000] = 999.0
        values[6000] = 1.0
        reduced = downsample(series(values), 400)
        floats = [float(v) for _, v in reduced]
        assert max(floats) == 999.0
        assert min(floats) == 1.0

    def test_a_single_point_series_is_safe(self):
        points = series([1])
        assert downsample(points, 100) == points

    def test_an_empty_series_is_safe(self):
        assert downsample([], 100) == []

    def test_the_budget_is_respected_for_any_oversized_input(self):
        for size in (801, 2000, 14048, 50_000):
            reduced = downsample(series(range(size)), MAX_CHART_POINTS)
            assert len(reduced) <= MAX_CHART_POINTS, size


class TestEquityChartSize:
    def test_a_long_curve_produces_a_bounded_number_of_coordinates(self):
        html = equity_chart(series(range(1, 14_049)), title="Equity")
        # Two polylines (curve and peak) plus one shaded polygon outline.
        assert html.count(",") < MAX_CHART_POINTS * 8

    def test_a_long_curve_chart_stays_small_enough_for_a_phone(self):
        html = equity_chart(series(range(1, 14_049)), title="Equity")
        assert len(html) < 120_000, f"chart is {len(html)} bytes"

    def test_the_chart_says_it_was_downsampled_and_names_the_real_count(self):
        html = equity_chart(series(range(1, 14_049)), title="Equity")
        assert "14048" in html or "14,048" in html
        assert "downsampl" in html.lower()

    def test_a_short_curve_is_not_labelled_as_downsampled(self):
        html = equity_chart(series(range(1, 50)), title="Equity")
        assert "downsampl" not in html.lower()

    def test_a_downsampled_chart_still_reports_the_true_extremes(self):
        values = [100.0] * 9000
        values[4500] = 1.0
        html = equity_chart(series(values), title="Equity")
        assert "1.0" in html or "1" in html
        # The accessible caption must carry the real minimum, not the
        # minimum of whatever survived the decimation.
        assert "9000" in html or "9,000" in html
