"""Path forecasting and the forecast trial.

The guarantees pinned here: a forecaster never sees a bar after its origin;
the trial never grades with a bar after its window; the scoring rules are
right (checked against brute force and against a forecaster that is
calibrated by construction); the Kronos adapter samples paths rather than
averaging them and never reaches for the network; and the cone strategy goes
through the ordinary engine like any other strategy.
"""

from __future__ import annotations

import ast
import json
import math
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.errors import ConfigError, ValidationError
from signalforge.core.history import PriceHistory
from signalforge.forecasting.baselines import BlockBootstrapForecaster, DriftlessRandomWalk
from signalforge.forecasting.cone import Direction, Regime, read_cone
from signalforge.forecasting.kronos import (
    KronosForecaster,
    future_business_days,
    kronos_inputs,
    time_features,
)
from signalforge.forecasting.paths import (
    ContextWindow,
    InsufficientContext,
    PathForecast,
    derive_seed,
)
from signalforge.forecasting.scoring import crps_skill, sample_crps, score, summarize, wilson
from signalforge.forecasting.strategy import ForecastConeStrategy
from signalforge.forecasting.trial import forecast_latest, run_trial
from tests.conftest import business_days, make_bar

PACKAGE = Path(__file__).resolve().parents[1] / "signalforge"


def gbm_history(symbol="SYN", n=900, sigma=0.01, drift=0.0, seed=7, start=date(2001, 1, 1)):
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(drift, sigma, n)))
    days = business_days(start, n)
    bars = [make_bar(symbol, d, Decimal(str(round(c, 4)))) for d, c in zip(days, closes)]
    return PriceHistory.from_bars(bars), days


class SpyForecaster:
    """Records every window it is handed; forecasts a flat cone."""

    forecaster_id = "spy"

    def __init__(self, context_length=50):
        self.context_length = context_length
        self.windows = []

    def parameters(self):
        return {"context_length": self.context_length}

    def sample_paths(self, window, horizon, n_paths, seed):
        self.windows.append(window)
        rng = np.random.default_rng(seed)
        paths = window.last_close * np.exp(np.cumsum(rng.normal(0, 0.01, (n_paths, horizon)), axis=1))
        return PathForecast(window.symbol, window.as_of, horizon, self.forecaster_id, window.last_close, paths)


class FixedCone:
    """Every path ends `ret` above (or below) today's close."""

    def __init__(self, ret, context_length=30, forecaster_id="fixed"):
        self.ret = ret
        self.context_length = context_length
        self.forecaster_id = forecaster_id

    def parameters(self):
        return {"ret": self.ret}

    def sample_paths(self, window, horizon, n_paths, seed):
        rng = np.random.default_rng(seed)
        noise = rng.normal(0, 1e-4, (n_paths, horizon))
        paths = window.last_close * (1 + self.ret + noise)
        return PathForecast(window.symbol, window.as_of, horizon, self.forecaster_id, window.last_close, paths)


# ---------------------------------------------------------------- windows
class TestContextWindow:
    def test_window_ends_at_the_view_and_never_after(self):
        history, days = gbm_history(n=200)
        w = ContextWindow.from_view(history.view(days[120]), "SYN", 100)
        assert w.dates[-1] == days[120] == w.as_of
        assert len(w.dates) == 100 and w.dates[0] == days[21]

    def test_short_history_is_refused_rather_than_forecast_on_less(self):
        history, days = gbm_history(n=200)
        with pytest.raises(InsufficientContext):
            ContextWindow.from_view(history.view(days[40]), "SYN", 100)

    def test_a_stale_window_is_refused(self):
        history, days = gbm_history(n=200)
        other = PriceHistory.from_bars(
            list(history.bars_for("SYN")[:100])
            + [make_bar("OTHER", d, Decimal("50")) for d in days]
        )
        with pytest.raises(InsufficientContext):
            ContextWindow.from_view(other.view(days[150]), "SYN", 50)

    def test_split_adjusted_and_frozen(self):
        days = business_days(date(2020, 1, 1), 3)
        bars = [
            make_bar("S", days[0], Decimal("200"), adj_close=Decimal("100")),
            make_bar("S", days[1], Decimal("101")),
            make_bar("S", days[2], Decimal("102")),
        ]
        w = ContextWindow.from_view(PriceHistory.from_bars(bars).view(days[2]), "S", 3)
        assert list(w.closes) == [100.0, 101.0, 102.0]
        with pytest.raises(ValueError):
            w.ohlcv[0, 0] = 1.0


# ---------------------------------------------------------------- trial guards
class TestTrialGuards:
    def test_the_forecaster_never_sees_a_bar_after_its_origin(self):
        history, days = gbm_history(n=400)
        spy = SpyForecaster(context_length=50)
        result = run_trial(history, ["SYN"], spy, start=days[60], end=days[399], horizon=10, n_paths=20)
        assert spy.windows
        for w, o in zip(spy.windows, result.outcomes):
            assert w.dates[-1] == w.as_of == o.as_of

    def test_no_outcome_is_graded_with_a_bar_after_the_window(self):
        history, days = gbm_history(n=400)
        end = days[300]
        result = run_trial(history, ["SYN"], SpyForecaster(50), start=days[60], end=end, horizon=10, n_paths=20)
        last_origin = max(o.as_of for o in result.outcomes)
        assert days.index(last_origin) + 10 <= days.index(end)

    def test_stride_defaults_to_the_horizon(self):
        history, days = gbm_history(n=400)
        result = run_trial(history, ["SYN"], SpyForecaster(50), start=days[60], end=days[399], horizon=10, n_paths=20)
        gaps = {days.index(b.as_of) - days.index(a.as_of) for a, b in zip(result.outcomes, result.outcomes[1:])}
        assert gaps == {10}

    def test_rerun_is_bit_identical(self):
        history, days = gbm_history(n=400)
        f = BlockBootstrapForecaster(context_length=100)
        a = run_trial(history, ["SYN"], f, start=days[150], end=days[399], horizon=10, n_paths=50)
        b = run_trial(history, ["SYN"], f, start=days[150], end=days[399], horizon=10, n_paths=50)
        assert [o.crps for o in a.outcomes] == [o.crps for o in b.outcomes]

    def test_forecast_latest_starts_at_the_last_bar(self):
        history, days = gbm_history(n=300)
        fc = forecast_latest(history, "SYN", DriftlessRandomWalk(100), horizon=5, n_paths=10)
        assert fc.as_of == days[-1]


# ---------------------------------------------------------------- scoring
class TestScoring:
    def test_sample_crps_matches_brute_force(self):
        rng = np.random.default_rng(3)
        x, y = rng.normal(size=37), 0.4
        brute = np.mean(np.abs(x - y)) - 0.5 * np.mean(np.abs(x[:, None] - x[None, :]))
        assert sample_crps(x, y) == pytest.approx(brute, rel=1e-12)

    def test_crps_of_a_point_mass_is_the_absolute_error(self):
        assert sample_crps(np.full(10, 3.0), 5.0) == pytest.approx(2.0)

    def test_a_calibrated_forecaster_covers_about_eighty_percent(self):
        # Driftless GBM, forecast by a driftless random walk with the
        # estimated sigma: calibrated by construction, so coverage near 80%,
        # PIT near uniform, and direction no better than a coin.
        history, days = gbm_history(n=4000, sigma=0.01, seed=11)
        result = run_trial(
            history, ["SYN"], DriftlessRandomWalk(250), start=days[300], end=days[-1],
            horizon=10, n_paths=200, stride=10,
        )
        s = result.summary()
        assert s.n > 300
        assert 0.74 <= s.coverage <= 0.86
        assert max(s.pit_histogram) < 0.16 and min(s.pit_histogram) > 0.05
        assert 0.42 <= s.hit_rate <= 0.58

    def test_lift_needs_thirty_calls_on_each_side(self):
        history, days = gbm_history(n=4000, seed=11)
        r = run_trial(history, ["SYN"], DriftlessRandomWalk(250), start=days[300], end=days[-1], horizon=10, n_paths=100)
        s = r.summary()
        trending = next(b for b in s.by_regime if b.label == "TRENDING")
        assert trending.n < 30 and math.isnan(s.confidence_lift)

    def test_an_always_up_cone_hits_exactly_the_base_rate(self):
        history, days = gbm_history(n=600)
        up = run_trial(history, ["SYN"], FixedCone(0.05), start=days[40], end=days[-1], horizon=5, n_paths=20)
        s = up.summary()
        assert s.trending_share == 1.0
        assert all(o.reading.direction is Direction.UP for o in up.outcomes)
        assert s.hit_rate == pytest.approx(s.base_rate_up, abs=0.02)

    def test_buckets_partition_the_called_forecasts(self):
        history, days = gbm_history(n=1200)
        r = run_trial(history, ["SYN"], BlockBootstrapForecaster(200), start=days[250], end=days[-1], horizon=10, n_paths=100)
        s = r.summary()
        called = sum(1 for o in r.outcomes if o.hit is not None)
        assert sum(b.n for b in s.by_agreement) == called
        assert sum(b.n for b in s.by_regime) == called
        assert sum(b.n for b in s.by_spread) == called

    def test_overlap_shrinks_the_independent_count(self):
        history, days = gbm_history(n=600)
        r = run_trial(history, ["SYN"], SpyForecaster(50), start=days[60], end=days[-1], horizon=10, n_paths=20, stride=2)
        s = r.summary()
        assert s.n_independent == math.floor(s.n * 2 / 10)
        assert s.notes

    def test_crps_skill_uses_only_matched_origins(self):
        history, days = gbm_history(n=800)
        a = run_trial(history, ["SYN"], DriftlessRandomWalk(200), start=days[250], end=days[-1], horizon=10, n_paths=50)
        assert crps_skill(a.outcomes, a.outcomes) == pytest.approx(0.0)

    def test_wilson_is_inside_the_unit_interval(self):
        lo, hi = wilson(9, 10)
        assert 0.0 <= lo < 0.9 < hi <= 1.0


# ---------------------------------------------------------------- cone reading
class TestCone:
    def _fc(self, terminal):
        terminal = np.asarray(terminal, dtype=float)
        paths = np.column_stack([np.full_like(terminal, 100.0), terminal])
        return PathForecast("X", date(2020, 1, 2), 2, "t", 100.0, paths)

    def test_71_of_100_up_reads_trending_up(self):
        r = read_cone(self._fc([101.0] * 71 + [99.0] * 29))
        assert (r.paths_up, r.regime, r.direction) == (71, Regime.TRENDING, Direction.UP)

    def test_51_of_100_reads_choppy(self):
        assert read_cone(self._fc([101.0] * 51 + [99.0] * 49)).regime is Regime.CHOPPY

    def test_unanimous_down_is_trending_down(self):
        r = read_cone(self._fc([95.0] * 100))
        assert (r.regime, r.direction) == (Regime.TRENDING, Direction.DOWN)

    def test_spread_is_the_ten_ninety_band_over_today(self):
        r = read_cone(self._fc(np.linspace(90, 110, 1001)))
        assert r.spread_pct == pytest.approx(0.16, abs=1e-9)


# ---------------------------------------------------------------- baselines
class TestBaselines:
    def test_random_walk_has_no_direction(self):
        history, days = gbm_history(n=600, drift=0.003)
        w = ContextWindow.from_view(history.view(days[-1]), "SYN", 512)
        fc = DriftlessRandomWalk(512).sample_paths(w, 24, 4000, 1)
        assert read_cone(fc).up_fraction == pytest.approx(0.5, abs=0.03)

    def test_bootstrap_inherits_the_trailing_drift(self):
        history, days = gbm_history(n=600, drift=0.003)
        w = ContextWindow.from_view(history.view(days[-1]), "SYN", 512)
        fc = BlockBootstrapForecaster(512).sample_paths(w, 24, 2000, 1)
        assert read_cone(fc).up_fraction > 0.75

    def test_seeds_are_stable_and_distinct(self):
        assert derive_seed(1, "SPY", "2010-01-04") == derive_seed(1, "SPY", "2010-01-04")
        assert derive_seed(1, "SPY", "2010-01-04") != derive_seed(1, "QQQ", "2010-01-04")


# ---------------------------------------------------------------- kronos adapter
class StandInPredictor:
    """Same generate() shape as upstream KronosPredictor; no torch needed.

    Returns the last normalised bar plus independent noise per batch row, so
    distinct rows prove the adapter samples paths instead of averaging them.
    """

    def __init__(self):
        self.calls = []

    def generate(self, x, x_stamp, y_stamp, pred_len, T, top_k, top_p, sample_count, verbose):
        self.calls.append((x.shape, x_stamp.shape, y_stamp.shape, pred_len, sample_count))
        rng = np.random.default_rng(len(self.calls))
        last = x[:, -1:, :]
        return np.repeat(last, pred_len, axis=1) + rng.normal(0, 0.05, (x.shape[0], pred_len, x.shape[2]))


class TestKronosAdapter:
    def _window(self, n=512):
        history, days = gbm_history(n=n + 10)
        return ContextWindow.from_view(history.view(days[-1]), "SYN", n)

    def test_samples_one_path_per_batch_row(self):
        stand_in = StandInPredictor()
        f = KronosForecaster("unused", "unused", predictor=stand_in, batch_size=40)
        fc = f.sample_paths(self._window(), 24, 100, seed=5)
        assert fc.paths.shape == (100, 24)
        assert [c[0][0] for c in stand_in.calls] == [40, 40, 20]
        assert all(c[4] == 1 for c in stand_in.calls), "sample_count must be 1, or upstream averages"
        assert all(c[1] == (c[0][0], 512, 5) and c[2] == (c[0][0], 24, 5) for c in stand_in.calls)
        assert len({tuple(p) for p in fc.paths}) == 100

    def test_denormalisation_puts_paths_back_in_price_units(self):
        w = self._window()
        fc = KronosForecaster("u", "u", predictor=StandInPredictor()).sample_paths(w, 5, 30, 1)
        assert np.median(fc.paths[:, 0]) == pytest.approx(w.last_close, rel=0.05)

    def test_sampled_candles_are_repaired_into_valid_bars(self):
        fc = KronosForecaster("u", "u", predictor=StandInPredictor()).sample_paths(self._window(), 5, 30, 1)
        o, h, low, c = (fc.ohlc_paths[..., i] for i in range(4))
        assert np.all(h >= np.maximum(o, c)) and np.all(low <= np.minimum(o, c))

    def test_inputs_follow_upstream_layout(self):
        w = self._window(64)
        z, mean, std = kronos_inputs(w, clip=5.0)
        assert z.shape == (64, 6) and z.dtype == np.float32
        assert np.all(np.abs(z) <= 5.0)
        amount = w.ohlcv[:, 4] * w.ohlcv[:, :4].mean(axis=1)
        assert mean[5] == pytest.approx(amount.mean())

    def test_unknown_volume_becomes_zero_not_invented(self):
        days = business_days(date(2020, 1, 1), 40)
        w = ContextWindow.from_closes("IDX", days, np.linspace(100, 110, 40))
        _, mean, _ = kronos_inputs(w, clip=5.0)
        assert mean[4] == 0.0 and mean[5] == 0.0

    def test_time_features_and_future_days(self):
        fut = future_business_days(date(2026, 10, 30), 3)
        assert fut == (date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4))
        assert time_features(fut)[0].tolist() == [0, 0, 0, 2, 11]

    def test_missing_weights_refuse_without_touching_the_network(self, tmp_path, monkeypatch):
        import socket

        def explode(*a, **k):
            raise AssertionError("network attempted")

        monkeypatch.setattr(socket, "socket", explode)
        f = KronosForecaster(tmp_path / "nope", tmp_path / "nope2")
        with pytest.raises(ConfigError, match="loaded from disk only"):
            f.sample_paths(self._window(64), 5, 10, 1)

    def test_context_above_512_is_refused(self):
        with pytest.raises(ValidationError):
            KronosForecaster("u", "u", context_length=1024)


# ---------------------------------------------------------------- strategy
class TestConeStrategy:
    def _run(self, forecaster):
        history, days = gbm_history(n=200, drift=0.001)
        research = ResearchConfig(universe=("SYN",))
        strategy = ForecastConeStrategy(forecaster, ["SYN"], Decimal("0.05"), horizon=5)
        return BacktestEngine(research, RiskConfig()).run(strategy, history)

    def test_confident_up_cone_goes_long_through_the_engine(self):
        result = self._run(FixedCone(0.05))
        assert any(f.side.value == "BUY" for f in result.fills)

    def test_choppy_cone_never_trades(self):
        class Coin(FixedCone):
            def sample_paths(self, window, horizon, n_paths, seed):
                half = np.r_[np.full(n_paths // 2, 1.01), np.full(n_paths - n_paths // 2, 0.99)]
                paths = window.last_close * np.repeat(half[:, None], horizon, axis=1)
                return PathForecast(window.symbol, window.as_of, horizon, "coin", window.last_close, paths)

        assert self._run(Coin(0.0)).fills == []

    def test_strategy_reports_every_parameter(self):
        s = ForecastConeStrategy(FixedCone(0.01), ["SYN"], Decimal("0.05"))
        p = s.parameters()
        assert {"forecaster", "horizon", "n_paths", "trend_agreement", "seed", "rebalance_every"} <= set(p)
        assert s.strategy_id == "cone_fixed" and s.warmup_days == 30


# ---------------------------------------------------------------- structure
NETWORK = {"socket", "requests", "urllib", "urllib3", "http", "httpx", "aiohttp", "huggingface_hub"}


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_the_forecasting_package_imports_no_network_library():
    offenders = {
        p.name: sorted(_imports(p) & NETWORK)
        for p in sorted((PACKAGE / "forecasting").rglob("*.py"))
        if _imports(p) & NETWORK
    }
    assert offenders == {}


def test_paper_and_strategy_layers_do_not_import_forecasting():
    # The cone strategy is research-only until a trial earns it a promotion.
    for root in ("paper", "strategies", "risk"):
        for p in (PACKAGE / root).rglob("*.py"):
            assert "signalforge.forecasting" not in p.read_text(encoding="utf-8"), p


def test_the_cli_refuses_the_held_out_window(capsys):
    from signalforge.forecast_cli import main

    assert main(["trial", "--symbols", "SPY", "--window", "test"]) == 2
    assert "run-holdout" in capsys.readouterr().err


def test_the_cli_refuses_a_missing_warehouse_without_a_traceback(tmp_path, capsys):
    from signalforge.forecast_cli import main

    assert main(["cone", "--symbol", "SPY", "--db", str(tmp_path / "none.duckdb")]) == 2
    assert "no warehouse" in capsys.readouterr().err


def test_cli_trial_and_cone_run_end_to_end_on_a_real_warehouse(tmp_path, capsys, monkeypatch):
    """A small DuckDB warehouse, the real config windows, the real CLI path."""
    from signalforge.data.storage import Warehouse
    from signalforge.forecast_cli import main

    rng = np.random.default_rng(5)
    days = business_days(date(2002, 1, 1), 1100)  # covers 512 bars of context before 2005
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(days))))
    db = tmp_path / "wh.duckdb"
    w = Warehouse(str(db))
    w.upsert_bars([
        make_bar("SYN", d, Decimal(str(round(c, 4))), source="tiingo") for d, c in zip(days, closes)
    ])
    w.close()
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    code = main([
        "trial", "--symbols", "SYN", "--window", "validation", "--db", str(db),
        "--config-dir", str(PACKAGE.parent / "config"), "--source", "tiingo",
        "--forecaster", "bootstrap,random-walk", "--paths", "50", "--out", str(out),
    ])
    assert code == 0
    record = json.loads(next(out.glob("*.json")).read_text())
    assert {f["forecaster"] for f in record["forecasters"]} == {"block_bootstrap", "random_walk"}
    assert all(f["summary"]["n"] > 0 for f in record["forecasters"])
    assert "crps_skill_vs_random_walk" in record
    # per-forecast detail lands under data/, which is gitignored
    assert list((tmp_path / "data" / "forecast_trials").glob("*.jsonl"))

    page = tmp_path / "dashboard" / "build" / "forecast.html"
    assert page.exists() and "SYN, the next 24 sessions" in page.read_text()

    capsys.readouterr()
    assert main([
        "trial", "--symbols", "SYN", "--window", "validation", "--db", str(db),
        "--config-dir", str(PACKAGE.parent / "config"), "--source", "tiingo", "--forecaster", "bootstrap",
        "--paths", "50", "--out", str(out), "--no-dashboard", "--report-dry-run",
    ]) == 0
    printed = capsys.readouterr().out
    assert "dry run: Crucible report valid, nothing sent" in printed
    report = json.loads(next(line for line in printed.splitlines() if line.startswith("{")))
    assert report["event_type"] == "forecast_trial" and "cone" in report["payload"]

    assert main(["cone", "--symbol", "SYN", "--db", str(db), "--paths", "40"]) == 0
    assert "paths up" in capsys.readouterr().out
