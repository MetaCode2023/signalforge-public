"""The hold-out must not be *shown* a second time, not merely un-recorded.

Found by code review, with measured reproduction. `run_research` ran the test
window, appended the result to `output.runs`, and only then tried to record
it. The registry correctly refused and logged a violation — but `main` still
printed the table and the dashboard still rendered the figures. The
researcher saw the exact out-of-sample numbers the guard exists to protect.

For a system that stakes its credibility on one unspent hold-out number,
"the registry refuses a second look" overstated what the code did. The guard
has to be consulted BEFORE the backtest, and a spent window has to be absent
from the output rather than present with a caveat.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.config.models import ResearchConfig, SplitConfig
from signalforge.data.storage import Warehouse
from signalforge.learning.registry import ExperimentRegistry
from signalforge.research_cli import format_table, run_research

from tests.conftest import business_days

D = Decimal
DAYS = business_days(date(2000, 1, 3), 1800)


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "wh.duckdb"))
    w.upsert_series(
        [
            {
                "series_id": "NASDAQCOM",
                "observation_date": d,
                "value": D(1000) + D(i) * D("0.5"),
                "source": "fred",
                "retrieved_at": None,
            }
            for i, d in enumerate(DAYS)
        ]
    )
    yield w
    w.close()


def config() -> ResearchConfig:
    return ResearchConfig(
        starting_cash=D("100000"),
        universe=("NASDAQCOM",),
        benchmark_symbol="NASDAQCOM",
        splits=SplitConfig(
            train_start=DAYS[0],
            train_end=DAYS[900],
            validation_start=DAYS[901],
            validation_end=DAYS[1400],
            test_start=DAYS[1401],
            test_end=DAYS[-1],
        ),
    )


class TestASpentHoldoutIsNotShownAgain:
    def test_the_first_look_returns_test_results(self, warehouse):
        registry = ExperimentRegistry(warehouse)
        first = run_research(warehouse, config(), run_holdout=True, registry=registry)
        assert first.run_for("test") is not None
        assert first.run_for("test").outcomes

    def test_the_second_look_returns_no_test_window_at_all(self, warehouse):
        registry = ExperimentRegistry(warehouse)
        run_research(warehouse, config(), run_holdout=True, registry=registry)
        second = run_research(warehouse, config(), run_holdout=True, registry=registry)
        assert second.run_for("test") is None, (
            "the spent hold-out was returned again; a caveat is not enough, the "
            "numbers must not be handed back"
        )
        assert {r.window for r in second.runs} == {"train", "validation"}

    def test_the_second_look_says_why_the_window_is_absent(self, warehouse):
        registry = ExperimentRegistry(warehouse)
        run_research(warehouse, config(), run_holdout=True, registry=registry)
        second = run_research(warehouse, config(), run_holdout=True, registry=registry)
        joined = " ".join(second.notes).lower()
        assert "already" in joined or "spent" in joined
        assert "test" in joined

    def test_the_printed_table_cannot_contain_a_spent_test_window(self, warehouse):
        registry = ExperimentRegistry(warehouse)
        run_research(warehouse, config(), run_holdout=True, registry=registry)
        second = run_research(warehouse, config(), run_holdout=True, registry=registry)
        assert "TEST" not in format_table(second)

    def test_the_refusal_is_still_recorded_as_a_violation(self, warehouse):
        registry = ExperimentRegistry(warehouse)
        run_research(warehouse, config(), run_holdout=True, registry=registry)
        run_research(warehouse, config(), run_holdout=True, registry=registry)
        rows = warehouse.con.execute(
            "SELECT count(*) FROM test_evaluations WHERE violation"
        ).fetchall()
        assert rows[0][0] >= 1, "an attempted second look must leave an audit record"

    def test_the_train_and_validation_windows_are_unaffected(self, warehouse):
        registry = ExperimentRegistry(warehouse)
        run_research(warehouse, config(), run_holdout=True, registry=registry)
        second = run_research(warehouse, config(), run_holdout=True, registry=registry)
        for window in ("train", "validation"):
            run = second.run_for(window)
            assert run is not None and run.outcomes

    def test_a_probe_reports_spent_status_without_logging_a_violation(self, warehouse):
        """The pre-check must not itself count as a look.

        A probe that logged a violation every time the CLI started would fill
        the audit trail with false positives and make the real signal useless.
        """
        registry = ExperimentRegistry(warehouse)
        run_research(warehouse, config(), run_holdout=True, registry=registry)
        before = warehouse.con.execute(
            "SELECT count(*) FROM test_evaluations"
        ).fetchall()[0][0]
        for _ in range(3):
            registry.holdout_spent(
                strategy_name="ma_trend",
                parameters={"fast_window": 50},
                dataset_fingerprint="abc",
            )
        after = warehouse.con.execute(
            "SELECT count(*) FROM test_evaluations"
        ).fetchall()[0][0]
        assert after == before

    def test_the_probe_is_true_for_a_spent_combination(self, warehouse):
        registry = ExperimentRegistry(warehouse)
        output = run_research(warehouse, config(), run_holdout=True, registry=registry)
        test_run = output.run_for("test")
        outcome = test_run.outcomes[0]
        from signalforge.research_cli import _stringify

        assert registry.holdout_spent(
            strategy_name=outcome.strategy_id,
            parameters=_stringify(outcome.result.parameters),
            dataset_fingerprint=test_run.dataset_fingerprint,
        )

    def test_the_probe_is_false_for_an_untouched_combination(self, warehouse):
        registry = ExperimentRegistry(warehouse)
        assert not registry.holdout_spent(
            strategy_name="never_run",
            parameters={"x": 1},
            dataset_fingerprint="unseen",
        )
