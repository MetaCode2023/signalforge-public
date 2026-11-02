"""The Assay tab keeps the dashboard's rules: static, escaped, captioned, motion that can stop."""

from __future__ import annotations

import re
from datetime import date

import pytest

from signalforge.dashboard.assay_page import render_assay_page
from signalforge.dashboard.forecast_page import ForecastPageData, render_forecast_page
from signalforge.dashboard.render import render_dashboard
from signalforge.insiders.report import record
from signalforge.insiders.study import Params, run
from tests.insider_support import world


@pytest.fixture(scope="module")
def rec():
    purchases, panel, _ = world(n_symbols=30, n_owners=160, seed=4)
    result = run(purchases, panel, Params(window_start=date(2008, 1, 1), window_end=panel.days[-1], draws=80))
    result.funnel = [("Form 4 transaction rows read", 1000), ("Copied trades in the window", result.sizes["all"])]
    return record(result)


def test_static_and_loads_nothing(rec):
    html = render_assay_page(rec)
    assert "<script" not in html.lower()
    assert not re.search(r"(src|href)=\"https?://", html)


def test_every_chart_has_a_caption(rec):
    html = render_assay_page(rec)
    assert html.count("<figure") == html.count("<figcaption>") >= 4


def test_hindsight_is_labelled_as_using_the_future(rec):
    html = render_assay_page(rec)
    assert "USES THE FUTURE" in html and "cheating" in html


def test_verdict_follows_the_numbers(rec):
    html = render_assay_page(rec)
    p = rec["nulls"]["random_insiders_vs_smart_past"]["p_value"]
    phrase = "beat random insiders" if p < 0.05 else "did no better than random insiders"
    assert phrase in html


def test_motion_stops_for_reduced_motion(rec):
    html = render_assay_page(rec)
    block = html[html.index("@media (prefers-reduced-motion: reduce){\n.cv"):]
    assert "animation:none" in block and "stroke-dashoffset:0" in block


def test_hostile_text_is_escaped(rec):
    hostile = dict(rec, funnel=[{"step": "<script>alert(1)</script>", "count": 5}])
    html = render_assay_page(hostile, note="<b>x</b>")
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "&lt;b&gt;" in html


def test_no_trial_renders_data_unavailable():
    html = render_assay_page(None)
    assert "DATA UNAVAILABLE" in html and "insider_cli download" in html and "<svg" not in html


def test_all_three_pages_carry_all_three_tabs():
    pages = {
        "index.html": render_dashboard(),
        "forecast.html": render_forecast_page(ForecastPageData()),
        "assay.html": render_assay_page(None),
    }
    for here, html in pages.items():
        for href in pages:
            assert f'href="{href}"' in html
        assert f'<a href="{here}" aria-current="page">' in html
