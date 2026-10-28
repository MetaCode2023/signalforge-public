"""F-12 area, noted in RISK_GAUNTLET round 5: `record()` was not atomic.

Recording a test-window experiment is two writes: the experiment row, and the
`test_evaluations` row that spends the hold-out. They ran as two separate
statements. If the first succeeded and the second failed, the experiment
existed while the hold-out read as unspent, so the same strategy on the same
data could be evaluated against the held-out window a second time and the
registry would permit it. That is the one thing the registry exists to
prevent, and a failure between two statements was enough to defeat it.

The naive fix defeats something else. `guard_test_window` logs a refused
second look as a violation and then raises; wrapping the whole of `record()`
in a transaction would roll that log back on the way out, so a blocked
attempt would leave no trace. The audit record of a refused attempt is
exactly the kind of thing that must survive. So the guard runs before the
transaction opens, and only the (experiment, evaluation) pair is atomic.

Both halves are tested here, because fixing one by breaking the other is the
likely mistake.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.metrics import EvidenceLevel
from signalforge.config.models import ResearchConfig, SplitConfig
from signalforge.data.storage import Warehouse
from signalforge.learning.registry import ExperimentRegistry, HoldoutViolation

D = Decimal


class InjectedFailure(RuntimeError):
    """A write failing partway through, which is what this file is about."""


class FailingOn:
    """A connection proxy that raises on one SQL fragment and passes the rest.

    DuckDB's connection does not allow its `execute` to be replaced, so the
    fault is injected one level out, around the whole connection.
    """

    def __init__(self, connection, fragment: str):
        self._connection = connection
        self._fragment = fragment

    def execute(self, sql, *args, **kwargs):
        if self._fragment in sql:
            raise InjectedFailure(f"injected failure on {self._fragment!r}")
        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def splits():
    return ResearchConfig(
        splits=SplitConfig(
            train_start=date(2020, 1, 1), train_end=date(2021, 12, 31),
            validation_start=date(2022, 1, 1), validation_end=date(2022, 12, 31),
            test_start=date(2023, 1, 1), test_end=date(2023, 12, 31),
        )
    ).splits


def record_kwargs(**overrides):
    base = dict(
        strategy_name="ma_trend",
        strategy_version="1",
        parameters={"fast_window": 5, "slow_window": 20},
        dataset_fingerprint="fp-atomic",
        dataset_description="synthetic walk",
        window="test",
        splits=splits(),
        transaction_cost_assumptions={"slippage_bps": "5"},
        engine_version="0.1.0",
        random_seed=1,
        hypothesis="trend persists",
        metrics={"sharpe": "0.8", "observations": 400},
        max_drawdown=D("0.1"),
        trade_count=12,
        evidence_level=EvidenceLevel.INSUFFICIENT_EVIDENCE,
        git_commit="deadbeef",
    )
    base.update(overrides)
    return base


@pytest.fixture
def warehouse():
    w = Warehouse(":memory:")
    yield w
    w.close()


@pytest.fixture
def registry(warehouse):
    return ExperimentRegistry(warehouse)


def counts(warehouse):
    experiments = warehouse.con.execute("SELECT count(*) FROM experiments").fetchone()[0]
    evaluations = warehouse.con.execute(
        "SELECT count(*) FROM test_evaluations"
    ).fetchone()[0]
    violations = warehouse.con.execute(
        "SELECT count(*) FROM test_evaluations WHERE violation"
    ).fetchone()[0]
    return experiments, evaluations, violations


class TestTheSuccessfulPathIsUnchanged:
    def test_a_test_window_record_writes_both_rows(self, registry, warehouse):
        experiment_id = registry.record(**record_kwargs())
        assert counts(warehouse) == (1, 1, 0)
        assert registry.get(experiment_id).evaluated_window == "test"
        assert registry.holdout_spent(
            strategy_name="ma_trend",
            parameters={"fast_window": 5, "slow_window": 20},
            dataset_fingerprint="fp-atomic",
        )

    def test_a_non_test_window_record_writes_only_the_experiment(self, registry, warehouse):
        registry.record(**record_kwargs(window="train"))
        assert counts(warehouse) == (1, 0, 0)

    def test_recording_a_train_experiment_twice_is_idempotent(self, registry, warehouse):
        first = registry.record(**record_kwargs(window="train"))
        second = registry.record(**record_kwargs(window="train"))
        assert first == second
        assert counts(warehouse) == (1, 0, 0)


class TestAFailureBetweenTheTwoWritesLeavesNeither:
    def test_a_failing_evaluation_log_rolls_the_experiment_back(
        self, registry, warehouse, monkeypatch
    ):
        """The defect: the experiment survived while the hold-out stayed unspent."""

        def explode(*args, **kwargs):
            raise InjectedFailure("the test_evaluations write failed")

        monkeypatch.setattr(registry, "_log_test_evaluation", explode)
        with pytest.raises(InjectedFailure):
            registry.record(**record_kwargs())

        assert counts(warehouse) == (0, 0, 0), (
            "the experiment row outlived the hold-out log that was supposed to "
            "spend the window"
        )

    def test_and_the_holdout_is_still_unspent_afterwards(
        self, registry, warehouse, monkeypatch
    ):
        """Unspent AND no experiment: the two must agree either way.

        Before the fix they disagreed, which is worse than either state: the
        registry said the window was available while a row claimed it had
        already been evaluated.
        """

        def explode(*args, **kwargs):
            raise InjectedFailure("the test_evaluations write failed")

        monkeypatch.setattr(registry, "_log_test_evaluation", explode)
        with pytest.raises(InjectedFailure):
            registry.record(**record_kwargs())

        assert not registry.holdout_spent(
            strategy_name="ma_trend",
            parameters={"fast_window": 5, "slow_window": 20},
            dataset_fingerprint="fp-atomic",
        )
        assert registry.count() == 0

    def test_a_retry_after_a_rolled_back_failure_succeeds_cleanly(
        self, registry, warehouse, monkeypatch
    ):
        """The point of rolling back: the operation can be repeated."""
        calls = {"n": 0}
        real = registry._log_test_evaluation

        def fail_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise InjectedFailure("transient")
            return real(*args, **kwargs)

        monkeypatch.setattr(registry, "_log_test_evaluation", fail_once)
        with pytest.raises(InjectedFailure):
            registry.record(**record_kwargs())
        assert counts(warehouse) == (0, 0, 0)

        experiment_id = registry.record(**record_kwargs())
        assert counts(warehouse) == (1, 1, 0)
        assert registry.get(experiment_id).evaluated_window == "test"

    @pytest.mark.parametrize("window", ["test", "train"])
    def test_a_failing_experiment_insert_leaves_nothing_behind(
        self, registry, warehouse, monkeypatch, window
    ):
        monkeypatch.setattr(
            registry, "_con", FailingOn(registry._con, "INSERT INTO experiments")
        )
        with pytest.raises(InjectedFailure):
            registry.record(**record_kwargs(window=window))
        monkeypatch.undo()
        assert counts(warehouse) == (0, 0, 0)


class TestARefusedSecondLookIsStillRecorded:
    """What the naive fix would have broken.

    `guard_test_window` logs a violation and raises. If that log were inside
    the rolled-back transaction, a blocked attempt would leave no trace, and
    "someone asked to re-open the hold-out" would stop being visible.
    """

    def test_the_violation_survives_the_raise(self, registry, warehouse):
        registry.record(**record_kwargs())
        assert counts(warehouse) == (1, 1, 0)

        with pytest.raises(HoldoutViolation):
            registry.record(**record_kwargs(random_seed=99))

        experiments, evaluations, violations = counts(warehouse)
        assert violations == 1, "the refused second look left no audit record"
        assert evaluations == 2
        assert experiments == 1, "the refused attempt must not add an experiment"

    def test_a_third_attempt_is_logged_too(self, registry, warehouse):
        registry.record(**record_kwargs())
        for _ in range(2):
            with pytest.raises(HoldoutViolation):
                registry.record(**record_kwargs(random_seed=99))
        _, _, violations = counts(warehouse)
        assert violations == 2

    def test_the_read_only_probe_still_logs_nothing(self, registry, warehouse):
        registry.record(**record_kwargs())
        before = counts(warehouse)
        for _ in range(5):
            assert registry.holdout_spent(
                strategy_name="ma_trend",
                parameters={"fast_window": 5, "slow_window": 20},
                dataset_fingerprint="fp-atomic",
            )
        assert counts(warehouse) == before

    def test_the_violation_rows_name_the_strategy_and_the_dataset(self, registry, warehouse):
        experiment_id = registry.record(**record_kwargs())
        with pytest.raises(HoldoutViolation):
            registry.record(**record_kwargs(random_seed=99))
        rows = registry.test_evaluations_for(experiment_id)
        assert len(rows) == 2
        assert [r["violation"] for r in rows] == [False, True]
        assert all(r["strategy_name"] == "ma_trend" for r in rows)
        assert all(r["dataset_fingerprint"] == "fp-atomic" for r in rows)


class TestTheWarehouseTransactionHelperIsPublic:
    """The registry does not reach into a private method to get atomicity.

    F-13 was the same shape: `closed_trades` was private and imported across
    modules anyway. It was made public with the private name aliased, and the
    same is done here rather than calling `warehouse._tx` from the learning
    layer.
    """

    def test_transaction_commits_on_success(self, warehouse):
        warehouse.con.execute("CREATE TABLE t (x INTEGER)")
        warehouse.transaction(lambda: warehouse.con.execute("INSERT INTO t VALUES (1)"))
        assert warehouse.con.execute("SELECT count(*) FROM t").fetchone()[0] == 1

    def test_transaction_rolls_back_on_failure(self, warehouse):
        warehouse.con.execute("CREATE TABLE t (x INTEGER)")

        def body():
            warehouse.con.execute("INSERT INTO t VALUES (1)")
            raise InjectedFailure("boom")

        with pytest.raises(InjectedFailure):
            warehouse.transaction(body)
        assert warehouse.con.execute("SELECT count(*) FROM t").fetchone()[0] == 0

    def test_the_private_alias_still_works_for_existing_callers(self, warehouse):
        # `is` on two bound methods is False even for the same function, so
        # this compares the underlying functions. The first version of this
        # test asserted identity of the bound methods and failed for that
        # reason rather than because the alias was missing.
        assert warehouse._tx.__func__ is warehouse.transaction.__func__
        warehouse.con.execute("CREATE TABLE aliased (x INTEGER)")
        warehouse._tx(lambda: warehouse.con.execute("INSERT INTO aliased VALUES (1)"))
        assert warehouse.con.execute("SELECT count(*) FROM aliased").fetchone()[0] == 1
