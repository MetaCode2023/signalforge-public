"""The Kronos Trial dashboard tab follows the operator page's rules.

Static, no script, nothing from the network, a caption on every chart,
DATA UNAVAILABLE when there is nothing to show, and motion that switches off
under prefers-reduced-motion.
"""

from __future__ import annotations

import re
from datetime import date

import numpy as np

from signalforge.dashboard.forecast_page import (
    ForecastPageData,
    LivePanel,
    ReplayPanel,
    render_forecast_page,
)
from signalforge.dashboard.render import render_dashboard
from signalforge.forecasting.baselines import BlockBootstrapForecaster, DriftlessRandomWalk
from signalforge.forecasting.paths import ContextWindow
from signalforge.forecasting.trial import run_trial
from signalforge.forecasting.report import trial_record
from tests.conftest import business_days
from tests.test_forecasting import gbm_history


def _page_data():
    history, days = gbm_history(n=1400, seed=3)
    bs, rw = BlockBootstrapForecaster(250), DriftlessRandomWalk(250)
    results = [run_trial(history, ["SYN"], f, start=days[300], end=days[-1], horizon=10, n_paths=60) for f in (rw, bs)]
    record = trial_record(results, null_id="random_walk")
    win = ContextWindow.from_view(history.view(days[-1]), "SYN", 250)
    live = [LivePanel(f.sample_paths(win, 10, 60, 1), win) for f in (bs, rw)]
    o = results[1].outcomes[3]
    i = days.index(o.as_of)
    w2 = ContextWindow.from_view(history.view(o.as_of), "SYN", 250)
    fc = bs.sample_paths(w2, 10, 60, 7)
    closes = [float(b.close) for b in history.bars_for("SYN")]
    from signalforge.forecasting.scoring import score
    rp = ReplayPanel(fc, closes[i - 60:i + 1], closes[i + 1:i + 11], score(fc, closes[i + 1:i + 11]))
    backtest = {"buy_and_hold": {"equity": [1e5, 1.1e5, 1.2e5], "final": 1.2e5, "sharpe": 0.8},
                "cone_block_bootstrap": {"equity": [1e5, 1.02e5, 1.05e5], "final": 1.05e5, "sharpe": 0.4}}
    return ForecastPageData(record=record, live=live, replays=[rp], backtest=backtest)


def test_page_is_static_and_loads_nothing():
    html = render_forecast_page(_page_data())
    assert "<script" not in html.lower()
    assert not re.search(r"(src|href)=\"https?://", html)
    assert "@import" not in html


def test_every_sampled_path_is_drawn_and_animated():
    html = render_forecast_page(_page_data())
    # 2 live cones + 1 replay, 60 paths each
    assert html.count('class="p up"') + html.count('class="p dn"') == 180
    assert 'pathLength="1"' in html and "@keyframes draw" in html


def test_motion_switches_off_for_reduced_motion():
    html = render_forecast_page(_page_data())
    block = html[html.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation:none" in block and "stroke-dashoffset:0" in block


def test_forecaster_and_replay_switching_need_no_script():
    html = render_forecast_page(_page_data())
    assert html.count('type="radio" name="fc"') == 2
    assert html.count('type="radio" name="rp"') == 1
    assert "#fc-1:checked~.pane[data-g='fc-1']{display:block}" in html


def test_every_chart_has_a_caption():
    html = render_forecast_page(_page_data())
    assert html.count("<figure") == html.count("<figcaption>")


def test_no_trial_renders_data_unavailable_not_an_empty_shell():
    html = render_forecast_page(ForecastPageData())
    assert "DATA UNAVAILABLE" in html and "forecast_cli trial" in html
    assert "<svg" not in html


def test_both_pages_carry_the_tab_strip():
    tab = render_forecast_page(ForecastPageData())
    main = render_dashboard()
    for html, here in ((tab, "forecast.html"), (main, "index.html")):
        assert 'href="index.html"' in html and 'href="forecast.html"' in html
        assert f'<a href="{here}" aria-current="page">' in html


def test_untested_kronos_says_so_on_the_page():
    html = render_forecast_page(_page_data())
    assert "Kronos has not been run yet" in html
