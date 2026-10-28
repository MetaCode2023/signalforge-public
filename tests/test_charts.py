"""Inline SVG charts: accessibility, escaping, and honest empty states."""

from __future__ import annotations

import ast
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from signalforge.dashboard import charts

D = Decimal
HOSTILE = "<script>alert(1)</script>"


def pts(values, start=date(2020, 1, 2)):
    return [(start + timedelta(days=i), D(str(v))) for i, v in enumerate(values)]


def test_equity_chart_is_accessible_svg_with_title_and_summary():
    html = charts.equity_chart(pts([100, 110, 90, 120]), title="Equity A")
    assert "<svg" in html and 'role="img"' in html
    assert "<title" in html and "Equity A" in html
    assert "<desc" in html
    assert "<figcaption" in html
    assert "2020-01-02" in html and "2020-01-05" in html
    assert "120" in html and "90" in html


def test_equity_chart_shades_drawdown():
    html = charts.equity_chart(pts([100, 110, 90, 120]), title="t")
    assert 'class="dd-shade"' in html


def test_equity_chart_without_drawdown_has_no_shade_polygon():
    html = charts.equity_chart(pts([100, 101, 102, 103]), title="t")
    assert 'class="dd-shade"' not in html


def test_equity_chart_flat_series_does_not_divide_by_zero():
    html = charts.equity_chart(pts([100, 100, 100]), title="t")
    assert "<svg" in html
    assert "nan" not in html.lower() and "inf" not in html.lower()


def test_charts_with_too_few_points_state_data_unavailable_not_blank():
    for fn in (charts.equity_chart, charts.drawdown_chart):
        html = fn(pts([100]), title="t")
        assert "DATA UNAVAILABLE" in html
        assert "<svg" not in html
        html = fn([], title="t")
        assert "DATA UNAVAILABLE" in html


def test_drawdown_chart_summary_states_the_worst_point():
    html = charts.drawdown_chart(pts([100, 200, 150, 180]), title="Underwater")
    assert "Underwater" in html
    assert "25.00%" in html
    assert "<figcaption" in html


def test_risk_event_chart_text_summary_matches_bars():
    html = charts.risk_event_chart({"max_positions": 3, "breaker_daily_loss": 1}, title="Rules")
    assert "<svg" in html
    assert "max_positions" in html and "breaker_daily_loss" in html
    assert re.search(r"max_positions[^<]*3", html)
    assert html.count('class="bar"') == 2


def test_risk_event_chart_empty_says_it_was_checked():
    html = charts.risk_event_chart({}, title="Rules")
    assert "DATA UNAVAILABLE" in html
    assert "no risk events" in html.lower()
    assert "<svg" not in html


def test_chart_titles_and_labels_are_escaped():
    for html in (
        charts.equity_chart(pts([1, 2, 3]), title=HOSTILE),
        charts.drawdown_chart(pts([3, 2, 3]), title=HOSTILE),
        charts.risk_event_chart({HOSTILE: 2}, title=HOSTILE),
        charts.risk_event_chart({}, title=HOSTILE),
        charts.equity_chart([], title=HOSTILE),
    ):
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


def test_charts_pull_in_nothing_external_and_carry_no_currency_sign():
    for html in (
        charts.equity_chart(pts([100, 110, 90]), title="t"),
        charts.drawdown_chart(pts([100, 110, 90]), title="t"),
        charts.risk_event_chart({"a": 1}, title="t"),
    ):
        assert "http" not in html
        assert "<script" not in html
        assert "$" not in html


def test_equity_chart_uses_the_supplied_value_formatter_in_the_summary():
    html = charts.equity_chart(
        pts([100, 110]), title="t", fmt_value=lambda v: f"<b>FMT{v}</b>"
    )
    assert "<b>FMT110</b>" in html


def test_every_fstring_interpolation_in_charts_is_an_escaper_or_numeric_helper():
    from tests.test_dashboard import interpolation_violations

    path = Path(charts.__file__)
    assert interpolation_violations(path) == []
