"""Real-OHLCV research: adjusted basis, universe specs, and the hold-out refusal.

The series path trades an index level as a proxy. These tests pin the other
path: per-symbol OHLCV from the warehouse, run on split- and dividend-adjusted
prices, over a universe, benchmarked against a buy-and-hold ETF. Bars are
synthetic and written to a temporary warehouse.
"""

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig, SplitConfig
from signalforge.core.errors import SignalForgeError, ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.types import Bar
from signalforge.data.storage import Warehouse
from signalforge.learning.registry import ExperimentRegistry
from signalforge.pipeline import (
    SURVIVORSHIP_NOTE,
    baseline_specs,
    coverage_notes,
    ohlcv_history,
)
from signalforge.research_cli import main, parse_args, run_research
from signalforge.strategies.registry import build
from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2018, 1, 1), 900)


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "wh.duckdb"))
    yield w
    w.close()


def splits() -> SplitConfig:
    return SplitConfig(
        train_start=DAYS[0],
        train_end=DAYS[500],
        validation_start=DAYS[501],
        validation_end=DAYS[700],
        test_start=DAYS[701],
        test_end=DAYS[-1],
    )


def research() -> ResearchConfig:
    return ResearchConfig(starting_cash=D("100000"), universe=("AAA",), splits=splits())


def trending(symbol, slope, *, start=0, source="fixture", base=100):
    return [
        make_bar(
            symbol,
            day,
            D(base) + D(slope) * D(i),
            volume=5_000_000,
            source=source,
            adj_close=D(base) + D(slope) * D(i),
        )
        for i, day in enumerate(DAYS[start:], start=start)
    ]


def seed_universe(warehouse, source="fixture"):
    bars = []
    for symbol, slope in (("AAA", "0.30"), ("BBB", "0.05"), ("CCC", "-0.02"), ("SPY", "0.10")):
        bars += trending(symbol, slope, source=source)
    warehouse.upsert_bars(bars)


def bar(close, adj, *, open_=None, high=None, low=None, volume=1000, symbol="AAA", day=None):
    o = D(open_ if open_ is not None else close)
    c = D(close)
    return Bar(
        symbol=symbol,
        trade_date=day or DAYS[0],
        open=o,
        high=D(high) if high is not None else max(o, c),
        low=D(low) if low is not None else min(o, c),
        close=c,
        volume=volume,
        adj_close=None if adj is None else D(adj),
        source="fixture",
    )


class TestAdjustedBasis:
    def test_adjusted_basis_scales_every_price_by_the_adj_ratio(self, warehouse):
        warehouse.upsert_bars(
            [
                bar("100", "50", open_="100", high="105", low="95", volume=1000, day=DAYS[0]),
                bar("3", "1", open_="3.3", high="3.6", low="2.7", volume=900, day=DAYS[1]),
            ]
        )
        history = ohlcv_history(warehouse, ["AAA"], price_basis="adjusted").history
        first, second = history.bars_for("AAA")
        assert (first.open, first.high, first.low, first.close) == (
            D("50"), D("52.5"), D("47.5"), D("50"),
        )
        assert first.adj_close == first.close
        assert first.volume == 2000 and isinstance(first.volume, int)
        # 3 * (1/3) is 0.999...; quantized to six places it must land on 1.
        assert (second.open, second.high, second.low, second.close) == (
            D("1.1"), D("1.2"), D("0.9"), D("1"),
        )
        assert second.volume == 2700

    def test_raw_basis_remains_the_default(self, warehouse):
        warehouse.upsert_bars([bar("100", "50", day=DAYS[0])])
        bundle = ohlcv_history(warehouse, ["AAA"])
        only = bundle.history.bars_for("AAA")[0]
        assert only.close == D("100") and only.adj_close == D("50") and only.volume == 1000
        assert not any("ADJUSTED" in n for n in bundle.limitations)

    def test_adjusted_basis_is_disclosed_with_its_caveats(self, warehouse):
        warehouse.upsert_bars([bar("100", "50", day=DAYS[0])])
        joined = " ".join(ohlcv_history(warehouse, ["AAA"], price_basis="adjusted").limitations)
        assert "ADJUSTED PRICE BASIS" in joined
        assert "not historical" in joined
        assert "commission" in joined

    def test_bars_without_adj_close_are_named_not_scaled(self, warehouse):
        warehouse.upsert_bars(
            [bar("100", "50", day=DAYS[0]), bar("80", None, symbol="NOADJ", day=DAYS[0])]
        )
        bundle = ohlcv_history(warehouse, ["AAA", "NOADJ"], price_basis="adjusted")
        assert bundle.history.bars_for("NOADJ")[0].close == D("80")
        assert bundle.history.bars_for("AAA")[0].close == D("50")
        named = [n for n in bundle.limitations if "no adj_close" in n]
        assert named and "NOADJ" in named[0] and "AAA" not in named[0]

    def test_an_unknown_price_basis_is_refused(self, warehouse):
        warehouse.upsert_bars([bar("100", "50", day=DAYS[0])])
        with pytest.raises(ValidationError):
            ohlcv_history(warehouse, ["AAA"], price_basis="nominal")

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        close=st.decimals(min_value=D("0.50"), max_value=D("900"), places=4),
        adj=st.decimals(min_value=D("0.01"), max_value=D("900"), places=6),
        lo_pct=st.integers(min_value=0, max_value=20),
        hi_pct=st.integers(min_value=0, max_value=20),
        open_pct=st.integers(min_value=-20, max_value=20),
    )
    def test_ohlc_ordering_survives_rounding(self, close, adj, lo_pct, hi_pct, open_pct):
        o = (close * (D(100) + D(open_pct)) / D(100)).quantize(D("0.0001"))
        low = min(close, o) * (D(100) - D(lo_pct)) / D(100)
        high = max(close, o) * (D(100) + D(hi_pct)) / D(100)
        w = Warehouse(":memory:")
        try:
            w.upsert_bars(
                [bar(close, adj, open_=o, high=high.quantize(D("0.0001")),
                     low=low.quantize(D("0.0001")), day=DAYS[0])]
            )
            out = ohlcv_history(w, ["AAA"], price_basis="adjusted").history.bars_for("AAA")[0]
        finally:
            w.close()
        assert out.low <= out.open <= out.high
        assert out.low <= out.close <= out.high


class TestSeriesFingerprintsArePinned:
    def test_series_dataset_fingerprint_is_pinned(self):
        from signalforge.pipeline import series_history

        obs = [(d, D(1000) + D(i) / D(4)) for i, d in enumerate(DAYS[:50])]
        history = series_history(obs, "NASDAQCOM").history
        digest = BacktestEngine.fingerprint(history, DAYS[0], DAYS[49])
        assert digest == PINNED_SERIES_FINGERPRINT


PINNED_SERIES_FINGERPRINT = "90a16c9d643d892a4f6068fc9f6b269a5d9ddfa476af85f186cdd847dc80f8cb"


class TestSplitsDoNotCrashAHeldPosition:
    def seed_split(self, warehouse):
        split_at = 100
        bars = []
        for i, day in enumerate(DAYS[:300]):
            raw = D("100") if i < split_at else D("50")
            adj = D("50")
            bars.append(
                Bar(symbol="SPLT", trade_date=day, open=raw, high=raw, low=raw, close=raw,
                    volume=5_000_000, adj_close=adj, source="fixture")
            )
        warehouse.upsert_bars(bars)

    def max_drawdown(self, warehouse, basis):
        history = ohlcv_history(warehouse, ["SPLT"], price_basis=basis).history
        engine = BacktestEngine(
            ResearchConfig(starting_cash=D("100000"), universe=("SPLT",)), RiskConfig()
        )
        result = engine.run(
            build("buy_and_hold", symbols=("SPLT",), target_weight=D("0.05")),
            history,
            run_id="split",
        )
        peak, worst = D("0"), D("0")
        for point in result.equity_curve:
            equity = point[1] if isinstance(point, tuple) else point.equity
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
        return worst

    def test_a_two_for_one_split_does_not_crash_a_held_position(self, warehouse):
        self.seed_split(warehouse)
        assert self.max_drawdown(warehouse, "adjusted") < D("0.01")
        assert self.max_drawdown(warehouse, "raw") >= D("0.01")


class TestBaselineSpecs:
    SERIES_SPECS = [
        (
            "MA trend 50/200",
            "ma_trend",
            {"fast_window": 50, "slow_window": 200, "target_weight": D("0.05"),
             "symbols": ("NASDAQCOM",)},
        ),
        (
            "Cross-sectional momentum 126/21",
            "xs_momentum",
            {"lookback_days": 126, "skip_days": 21, "top_n": 1,
             "target_weight_total": D("0.05"), "min_history_days": 150},
        ),
        (
            "Mean reversion z=2 / 20d",
            "zscore_mean_reversion",
            {"window": 20, "entry_z": D("2"), "exit_z": D("0"), "target_weight": D("0.05"),
             "max_holding_days": 20, "symbols": ("NASDAQCOM",)},
        ),
        (
            "Vol-targeted trend 10% ann.",
            "vol_targeted_trend",
            {"fast_window": 50, "slow_window": 200, "target_volatility": D("0.10"),
             "vol_window": 20, "max_weight": D("0.05"), "annualisation_factor": 252,
             "symbols": ("NASDAQCOM",)},
        ),
        (
            "Buy and hold (benchmark)",
            "buy_and_hold",
            {"symbols": ("NASDAQCOM",), "target_weight": D("0.05")},
        ),
    ]

    def test_series_baseline_specs_are_byte_identical(self):
        assert repr(baseline_specs("NASDAQCOM")) == repr(self.SERIES_SPECS)
        assert repr(baseline_specs("NASDAQCOM", universe=None, benchmark=None)) == repr(
            self.SERIES_SPECS
        )

    def test_universe_specs_trade_universe_and_benchmark_benchmark(self):
        universe = ("AAA", "BBB")
        specs = {sid: params for _, sid, params in baseline_specs(
            "ignored", universe=universe, benchmark="SPY")}
        for sid in ("ma_trend", "zscore_mean_reversion", "vol_targeted_trend"):
            assert specs[sid]["symbols"] == universe
        assert specs["buy_and_hold"]["symbols"] == ("SPY",)
        assert specs["buy_and_hold"]["target_weight"] == D("0.05")
        assert specs["xs_momentum"]["symbols"] == universe
        series = {sid: p for _, sid, p in baseline_specs("X")}
        assert "symbols" not in series["xs_momentum"]


class TestSpecsAreBuiltOnce:
    def test_holdout_guard_and_run_use_identical_specs(self, warehouse, monkeypatch):
        series = [(d, D(1000) + D(i)) for i, d in enumerate(DAYS)]
        warehouse.upsert_series(
            [{"series_id": "NASDAQCOM", "observation_date": d, "value": v,
              "source": "fred", "retrieved_at": None} for d, v in series]
        )
        seen_run: list = []
        seen_guard: list = []

        import signalforge.research_cli as cli

        real_run_window = cli.run_window

        def spy_run_window(bundle, config, window, **kw):
            if window == "test":
                seen_run.append(kw["specs"])
            return real_run_window(bundle, config, window, **kw)

        monkeypatch.setattr(cli, "run_window", spy_run_window)

        class Spy(ExperimentRegistry):
            def holdout_spent(self, *, strategy_name, parameters, dataset_fingerprint):
                seen_guard.append((strategy_name, parameters))
                return False

        run_research(warehouse, research(), run_holdout=True, registry=Spy(warehouse))
        assert seen_run and seen_guard
        assert seen_guard == [(sid, cli._stringify(p)) for _, sid, p in seen_run[0]]


class TestArguments:
    def test_series_and_symbols_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            parse_args(["--series", "NASDAQCOM", "--symbols", "A,B"])
        with pytest.raises(SystemExit):
            parse_args(["--series", "NASDAQCOM", "--universe"])
        with pytest.raises(SystemExit):
            parse_args(["--symbols", "A,B", "--universe"])

    def test_defaults(self):
        args = parse_args([])
        assert args.series == "NASDAQCOM" and args.benchmark == "SPY"
        assert args.source == "tiingo" and not args.universe and args.symbols is None
        assert parse_args(["--symbols", "a,b"]).symbols == ["A", "B"]


class TestOhlcvRun:
    def run(self, warehouse, **kw):
        seed_universe(warehouse)
        return run_research(
            warehouse, research(), symbols=["AAA", "BBB", "CCC"], benchmark="SPY",
            source="fixture", **kw,
        )

    def test_ohlcv_run_carries_no_proxy_note_but_carries_survivorship(self, warehouse):
        output = self.run(warehouse)
        for r in output.runs:
            joined = " ".join(r.limitations)
            assert "PROXY STUDY" not in joined
            assert SURVIVORSHIP_NOTE in r.limitations
            assert "ADJUSTED PRICE BASIS" in joined
            assert "COVERAGE" in joined
        assert {r.window for r in output.runs} == {"train", "validation"}

    def test_benchmark_is_the_benchmark_symbol(self, warehouse):
        output = self.run(warehouse)
        bench = output.run_for("validation").by_id("buy_and_hold")
        assert {f.symbol for f in bench.result.fills} == {"SPY"}

    def test_momentum_trades_on_a_universe(self, warehouse):
        bars = []
        for symbol, slope in (("AAA", "0.05"), ("BBB", "0.02"), ("SPY", "0.50")):
            bars += trending(symbol, slope)
        warehouse.upsert_bars(bars)
        output = run_research(warehouse, research(), symbols=["AAA", "BBB"],
                              benchmark="SPY", source="fixture")
        for window in ("train", "validation"):
            fills = output.run_for(window).by_id("xs_momentum").result.fills
            assert fills
            assert {f.symbol for f in fills} <= {"AAA", "BBB"}
            assert "SPY" not in {f.symbol for f in fills}

    def test_momentum_symbols_restrict_the_ranked_set_and_are_optional(self):
        plain = build("xs_momentum", lookback_days=126, skip_days=21, top_n=1,
                      target_weight_total=D("0.05"), min_history_days=150)
        assert "symbols" not in plain.parameters()
        scoped = build("xs_momentum", lookback_days=126, skip_days=21, top_n=1,
                       target_weight_total=D("0.05"), min_history_days=150,
                       symbols=("aaa",))
        assert scoped.parameters()["symbols"] == ("AAA",)

    def test_an_empty_symbols_list_is_refused(self, warehouse, tmp_path):
        seed_universe(warehouse)
        with pytest.raises(ValidationError, match="symbols"):
            run_research(warehouse, research(), symbols=[], benchmark="SPY",
                         source="fixture")
        with pytest.raises(ValidationError, match="symbols"):
            run_research(warehouse, research(), symbols=[], benchmark="SPY",
                         source="fixture", run_holdout=True)

    def test_empty_symbols_flag_cannot_fall_back_to_the_series_holdout(self, tmp_path):
        db = tmp_path / "e2.duckdb"
        w = Warehouse(str(db))
        w.upsert_series(
            [{"series_id": "NASDAQCOM", "observation_date": d, "value": D(1000) + D(i),
              "source": "fred", "retrieved_at": None} for i, d in enumerate(DAYS)]
        )
        w.close()
        for flag in (",", ""):
            code = main(["--symbols", flag, "--run-holdout", "--no-dashboard",
                         "--db", str(db)])
            assert code != 0
        w = Warehouse(str(db))
        try:
            assert ExperimentRegistry(w).count() == 0
            assert w.con.execute("SELECT COUNT(*) FROM test_evaluations").fetchone()[0] == 0
        finally:
            w.close()

    def test_a_partly_adjusted_symbol_is_refused(self, warehouse):
        warehouse.upsert_bars(
            [bar("100", "50", day=DAYS[0]), bar("100", None, day=DAYS[1])]
        )
        with pytest.raises(ValidationError, match="AAA"):
            ohlcv_history(warehouse, ["AAA"], price_basis="adjusted")

    def test_exposure_mismatch_is_disclosed(self, warehouse):
        output = self.run(warehouse)
        for r in output.runs:
            joined = " ".join(r.limitations)
            assert "EXPOSURE MISMATCH" in joined and "capital-deployed" in joined

    def test_adjusted_runs_supersede_the_engine_split_warning(self, warehouse):
        output = self.run(warehouse)
        result = output.run_for("train").by_id("buy_and_hold").result
        joined = " ".join(result.data_limitations)
        assert "supersedes" in joined


    def test_missing_benchmark_is_refused(self, warehouse):
        seed_universe(warehouse)
        with pytest.raises(ValidationError, match="NOPE"):
            run_research(warehouse, research(), symbols=["AAA"], benchmark="NOPE",
                         source="fixture")

    def test_mixed_sources_require_explicit_source(self, warehouse):
        seed_universe(warehouse)
        warehouse.upsert_bars(trending("AAA", "0.30", source="sample"))
        with pytest.raises(ValidationError):
            ohlcv_history(warehouse, ["AAA", "SPY"])
        with pytest.raises(ValidationError, match="source"):
            run_research(warehouse, research(), symbols=["AAA"], benchmark="SPY")
        output = run_research(warehouse, research(), symbols=["AAA"], benchmark="SPY",
                              source="fixture")
        assert output.runs

    def test_holdout_is_refused_for_ohlcv_runs(self, warehouse, tmp_path):
        seed_universe(warehouse)
        before = ExperimentRegistry(warehouse).count()
        with pytest.raises(SignalForgeError, match="hold-out"):
            run_research(warehouse, research(), symbols=["AAA"], benchmark="SPY",
                         source="fixture", run_holdout=True)
        assert ExperimentRegistry(warehouse).count() == before

    def test_holdout_is_refused_by_the_command(self, tmp_path, capsys):
        db = tmp_path / "cli.duckdb"
        w = Warehouse(str(db))
        seed_universe(w)
        w.close()
        code = main(["--db", str(db), "--symbols", "AAA", "--source", "fixture",
                     "--run-holdout", "--no-dashboard"])
        assert code != 0
        assert "hold-out" in capsys.readouterr().out
        w = Warehouse(str(db))
        try:
            assert ExperimentRegistry(w).count() == 0
        finally:
            w.close()


class TestEmptyUniverse:
    def test_universe_flag_with_an_empty_configured_universe_is_refused(
        self, tmp_path, capsys, monkeypatch
    ):
        import signalforge.research_cli as cli

        db = tmp_path / "e.duckdb"
        Warehouse(str(db)).close()
        real = cli.load_all()
        monkeypatch.setattr(
            cli, "load_all",
            lambda: real.model_copy(update={"research": real.research.model_copy(
                update={"universe": ()})}),
        )
        assert main(["--db", str(db), "--universe", "--no-dashboard"]) != 0
        assert "universe" in capsys.readouterr().out


class TestCoverageNotes:
    def history(self):
        bars = trending("AAA", "0.1") + trending("LATE", "0.1", start=300) + trending(
            "SPY", "0.1", start=50
        )
        return PriceHistory.from_bars(bars)

    def test_coverage_note_names_a_mid_window_listing(self):
        notes = coverage_notes(self.history(), splits(), "SPY")
        train = next(n for n in notes if "TRAIN" in n)
        assert "LATE" in train and DAYS[300].isoformat() in train
        assert "indicative" in train
        assert f"SPY from {DAYS[50].isoformat()}" in train

    def test_coverage_note_names_a_symbol_with_no_bars_in_the_window(self):
        bars = trending("AAA", "0.1") + trending("LATE", "0.1", start=600) + trending("SPY", "0.1")
        notes = coverage_notes(PriceHistory.from_bars(bars), splits(), "SPY")
        train = next(n for n in notes if "TRAIN" in n)
        validation = next(n for n in notes if "VALIDATION" in n)
        assert "No bars" in train and "LATE" in train
        assert "LATE" in validation

    def test_a_universe_name_absent_from_the_warehouse_is_named(self):
        notes = coverage_notes(self.history(), splits(), "SPY", universe=("AAA", "GHOST"))
        assert "GHOST" in next(n for n in notes if "TRAIN" in n)
