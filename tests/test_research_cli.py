"""The research entry point.

This is the command that produces every number a human reads, so the tests
here are mostly about what it refuses to do: consume the hold-out window
without being told to, and render a dashboard from anything other than the
run it just performed.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from signalforge.config.models import ResearchConfig, SplitConfig
from signalforge.core.history import PriceHistory
from signalforge.data.storage import Warehouse
from signalforge.research_cli import ResearchOutput, main, parse_args, run_research

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2000, 1, 3), 1800)


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "wh.duckdb"))
    series = [(d, Decimal(1000) + Decimal(i) * Decimal("0.5")) for i, d in enumerate(DAYS)]
    w.upsert_series(
        [
            {
                "series_id": "NASDAQCOM",
                "observation_date": d,
                "value": v,
                "source": "fred",
                "retrieved_at": None,
            }
            for d, v in series
        ]
    )
    yield w
    w.close()


def split_config() -> SplitConfig:
    return SplitConfig(
        train_start=DAYS[0],
        train_end=DAYS[900],
        validation_start=DAYS[901],
        validation_end=DAYS[1400],
        test_start=DAYS[1401],
        test_end=DAYS[-1],
    )


class TestArgumentParsing:
    def test_the_holdout_is_off_by_default(self):
        assert parse_args([]).run_holdout is False

    def test_the_holdout_can_be_requested_explicitly(self):
        assert parse_args(["--run-holdout"]).run_holdout is True

    def test_the_dashboard_is_written_by_default(self):
        assert parse_args([]).dashboard is True

    def test_the_dashboard_can_be_skipped(self):
        assert parse_args(["--no-dashboard"]).dashboard is False


class TestHoldoutGating:
    def test_without_the_flag_the_test_window_is_never_run(self, warehouse):
        output = run_research(warehouse, _config(), run_holdout=False)
        assert {r.window for r in output.runs} == {"train", "validation"}

    def test_the_output_states_that_the_holdout_was_not_consumed(self, warehouse):
        output = run_research(warehouse, _config(), run_holdout=False)
        assert any("held-out test window was not run" in n.lower() for n in output.notes)

    def test_with_the_flag_the_test_window_runs_once(self, warehouse):
        output = run_research(warehouse, _config(), run_holdout=True)
        assert "test" in {r.window for r in output.runs}
        assert len([r for r in output.runs if r.window == "test"]) == 1


class TestRealResults:
    def test_every_run_carries_real_outcomes(self, warehouse):
        output = run_research(warehouse, _config(), run_holdout=False)
        for run in output.runs:
            assert run.outcomes, f"{run.window} produced no outcomes"
            for outcome in run.outcomes:
                assert outcome.result.equity_curve
                assert outcome.metrics.observations > 0

    def test_the_benchmark_actually_takes_a_position(self, warehouse):
        # Guards the defect where buy-and-hold silently stayed in cash.
        output = run_research(warehouse, _config(), run_holdout=False)
        for run in output.runs:
            benchmark = run.by_id("buy_and_hold")
            assert benchmark is not None
            assert benchmark.result.fills, (
                f"the benchmark never bought in the {run.window} window, so every "
                "comparison against it is meaningless"
            )

    def test_capital_deployed_is_reported_for_every_outcome(self, warehouse):
        output = run_research(warehouse, _config(), run_holdout=False)
        for run in output.runs:
            for outcome in run.outcomes:
                assert outcome.metrics.average_capital_deployed is not None

    def test_limitations_travel_with_every_run(self, warehouse):
        output = run_research(warehouse, _config(), run_holdout=False)
        for run in output.runs:
            joined = " ".join(run.limitations).lower()
            assert "proxy study" in joined
            assert "close-only" in joined

    def test_experiments_are_recorded_in_the_registry(self, warehouse):
        run_research(warehouse, _config(), run_holdout=False)
        count = warehouse.con.execute("SELECT COUNT(*) FROM experiments").fetchall()[0][0]
        assert count > 0


class TestDashboardWiring:
    def test_the_dashboard_is_written_from_the_run_just_performed(self, warehouse, tmp_path):
        out = tmp_path / "index.html"
        output = run_research(warehouse, _config(), run_holdout=False)
        from signalforge.research_cli import write_dashboard_for

        written = write_dashboard_for(output, warehouse, _config_bundle(), out)
        html = written.read_text(encoding="utf-8")
        assert written.exists() and written.stat().st_size > 0
        assert "HISTORICAL BACKTEST" in html
        assert "SIMULATED" in html

    def test_the_dashboard_surfaces_insufficient_evidence_when_present(
        self, warehouse, tmp_path
    ):
        out = tmp_path / "index.html"
        output = run_research(warehouse, _config(), run_holdout=False)
        from signalforge.research_cli import write_dashboard_for

        html = write_dashboard_for(
            output, warehouse, _config_bundle(), out
        ).read_text(encoding="utf-8")
        flagged = [
            o
            for r in output.runs
            for o in r.outcomes
            if o.metrics.evidence.value == "INSUFFICIENT_EVIDENCE"
        ]
        if flagged:
            assert "INSUFFICIENT_EVIDENCE" in html


class TestMainIsSafe:
    def test_main_refuses_a_missing_warehouse_rather_than_creating_one(self, tmp_path):
        missing = tmp_path / "nope.duckdb"
        code = main(["--db", str(missing), "--no-dashboard"])
        assert code != 0
        assert not missing.exists()


def _config() -> ResearchConfig:
    return ResearchConfig(
        starting_cash=D("100000"),
        universe=("NASDAQCOM",),
        benchmark_symbol="NASDAQCOM",
        splits=split_config(),
    )


def _config_bundle():
    from signalforge.config.models import ConfigBundle, RiskConfig, load_data_sources

    return ConfigBundle(
        research=_config(), risk=RiskConfig(), data_sources=load_data_sources()
    )


class TestUniverseDashboard:
    def test_dashboard_renders_a_universe_run(self, tmp_path):
        from signalforge.config.models import ConfigBundle, RiskConfig, load_data_sources
        from signalforge.research_cli import write_dashboard_for
        from tests.test_ohlcv_research import research, seed_universe

        w = Warehouse(str(tmp_path / "u.duckdb"))
        try:
            seed_universe(w)
            output = run_research(
                w, research(), symbols=["AAA", "BBB", "CCC"], benchmark="SPY",
                source="fixture",
            )
            bundle = ConfigBundle(
                research=research(), risk=RiskConfig(), data_sources=load_data_sources()
            )
            html = write_dashboard_for(output, w, bundle, tmp_path / "u.html").read_text(
                encoding="utf-8"
            )
        finally:
            w.close()
        assert "AAA, BBB, CCC" in html
        assert "SPY" in html
        assert "SURVIVORSHIP BIAS" in html
        assert "PROXY STUDY" not in html
