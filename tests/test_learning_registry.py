"""Experiment registry and experiment runner.

The registry is the memory of what was tried. Two things are pinned hardest:
an experiment id is derived from its content (so it cannot be recorded twice
under two names), and the held-out test window can be evaluated once per
(strategy, parameters, dataset) - a second look is refused and logged.
"""

import random
import subprocess
from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.backtesting.metrics import EvidenceLevel
from signalforge.config.models import ResearchConfig, RiskConfig, SplitConfig
from signalforge.core.errors import PromotionError, ValidationError
from signalforge.core.history import PriceHistory
from signalforge.data.storage import Warehouse
from signalforge.learning import registry as registry_module
from signalforge.learning.experiments import ExperimentRun, run_experiment, tune
from signalforge.learning.registry import (
    ExperimentRegistry,
    HoldoutViolation,
    StrategySpec,
)

from tests.conftest import business_days, make_bar

D = Decimal

COLUMNS = {
    "experiment_id", "strategy_name", "strategy_version", "git_commit", "parameters",
    "dataset_fingerprint", "dataset_description", "train_start", "train_end",
    "validation_start", "validation_end", "test_start", "test_end",
    "transaction_cost_assumptions", "engine_version", "random_seed", "created_at",
    "hypothesis", "metrics", "max_drawdown", "trade_count", "evidence_level",
    "promotion_decision", "promotion_reason", "rejection_reason", "decided_by",
    "decided_at",
}


def noisy_history(sessions: int = 900, seed: int = 7, symbol: str = "SYN") -> PriceHistory:
    """Deterministic random walk with a mild drift. Same seed, same bars."""
    rng = random.Random(seed)
    price = 100.0
    bars = []
    for day in business_days(date(2020, 1, 1), sessions):
        price = max(5.0, price * (1 + rng.gauss(0.0004, 0.012)))
        bars.append(make_bar(symbol, day, D(f"{price:.4f}")))
    return PriceHistory.from_bars(bars)


def split_config() -> ResearchConfig:
    return ResearchConfig(
        splits=SplitConfig(
            train_start=date(2020, 1, 1), train_end=date(2021, 12, 31),
            validation_start=date(2022, 1, 1), validation_end=date(2022, 12, 31),
            test_start=date(2023, 1, 1), test_end=date(2023, 12, 31),
        )
    )


def ma_spec(fast: int = 5, slow: int = 20, symbol: str = "SYN") -> StrategySpec:
    return StrategySpec(
        "ma_trend",
        {"fast_window": fast, "slow_window": slow, "target_weight": D("0.05"), "symbols": (symbol,)},
    )


def make_engine(config: ResearchConfig) -> BacktestEngine:
    return BacktestEngine(config, RiskConfig())


@pytest.fixture
def warehouse():
    w = Warehouse(":memory:")
    yield w
    w.close()


@pytest.fixture
def registry(warehouse):
    return ExperimentRegistry(warehouse)


def record_kwargs(**overrides):
    base = dict(
        strategy_name="ma_trend",
        strategy_version="1",
        parameters={"fast_window": 5, "slow_window": 20},
        dataset_fingerprint="fp-1",
        dataset_description="synthetic walk",
        window="train",
        splits=split_config().splits,
        transaction_cost_assumptions={"slippage_bps": "5"},
        engine_version="0.1.0",
        random_seed=1,
        hypothesis="trend persists",
        metrics={"sharpe": "0.8", "observations": 400},
        max_drawdown=D("0.1"),
        trade_count=12,
        evidence_level=EvidenceLevel.INSUFFICIENT_EVIDENCE,
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------------- schema
def test_schema_records_every_reproducibility_field(warehouse, registry):
    cols = {r[1] for r in warehouse.con.execute("PRAGMA table_info('experiments')").fetchall()}
    assert COLUMNS <= cols


def test_record_roundtrips_every_field(registry):
    eid = registry.record(**record_kwargs(git_commit="abc123"))
    rec = registry.get(eid)
    assert rec.experiment_id == eid
    assert rec.strategy_name == "ma_trend"
    assert rec.git_commit == "abc123"
    assert rec.parameters == {"fast_window": 5, "slow_window": 20}
    assert rec.dataset_fingerprint == "fp-1"
    assert rec.train_start == date(2020, 1, 1) and rec.test_end == date(2023, 12, 31)
    assert rec.transaction_cost_assumptions == {"slippage_bps": "5"}
    assert rec.random_seed == 1
    assert rec.hypothesis == "trend persists"
    assert rec.metrics["observations"] == 400
    assert rec.max_drawdown == pytest.approx(0.1)
    assert rec.trade_count == 12
    assert rec.evidence_level == "INSUFFICIENT_EVIDENCE"
    assert rec.promotion_decision == "UNDECIDED"
    assert rec.decided_by is None and rec.decided_at is None
    assert rec.created_at is not None


def test_get_unknown_experiment_raises(registry):
    with pytest.raises(ValidationError):
        registry.get("exp_missing")


def test_decimal_and_tuple_parameters_are_serialisable(registry):
    eid = registry.record(**record_kwargs(parameters={"w": D("0.05"), "symbols": ("A", "B")}))
    assert registry.get(eid).parameters == {"w": "0.05", "symbols": ["A", "B"]}


# ------------------------------------------------------------------- git commit
def test_git_commit_is_read_when_not_supplied(registry):
    eid = registry.record(**record_kwargs())
    assert registry.get(eid).git_commit  # a sha or the fallback, never empty


def test_git_commit_falls_back_when_git_is_unavailable(registry, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(registry_module.subprocess, "run", boom)
    assert registry_module.current_git_commit() == registry_module.GIT_UNAVAILABLE
    eid = registry.record(**record_kwargs())
    assert registry.get(eid).git_commit == registry_module.GIT_UNAVAILABLE


def test_git_commit_falls_back_on_nonzero_exit(monkeypatch):
    def fail(*a, **k):
        return subprocess.CompletedProcess(a, 128, stdout="", stderr="not a repo")

    monkeypatch.setattr(registry_module.subprocess, "run", fail)
    assert registry_module.current_git_commit() == registry_module.GIT_UNAVAILABLE


# --------------------------------------------------------- deterministic ids
def test_same_experiment_gets_same_id_and_is_stored_once(registry):
    a = registry.record(**record_kwargs())
    b = registry.record(**record_kwargs(hypothesis="reworded", random_seed=99))
    assert a == b
    assert registry.count() == 1


def test_different_content_gets_different_ids(registry):
    base = registry.record(**record_kwargs())
    assert registry.record(**record_kwargs(parameters={"fast_window": 6, "slow_window": 20})) != base
    assert registry.record(**record_kwargs(dataset_fingerprint="fp-2")) != base
    assert registry.record(**record_kwargs(transaction_cost_assumptions={"slippage_bps": "9"})) != base
    assert registry.record(**record_kwargs(window="validation")) != base
    assert registry.record(**record_kwargs(strategy_name="other")) != base


def test_parameter_key_order_does_not_change_the_id(registry):
    a = registry.record(**record_kwargs(parameters={"a": 1, "b": 2}))
    b = registry.record(**record_kwargs(parameters={"b": 2, "a": 1}))
    assert a == b and registry.count() == 1


# ------------------------------------------------------------------ listing
def test_list_and_count_filters(registry):
    a = registry.record(**record_kwargs())
    registry.record(**record_kwargs(strategy_name="other", parameters={"x": 1}))
    registry.record_decision(a, "REJECTED", "no edge", "austin")
    assert registry.count() == 2
    assert registry.count(strategy_name="ma_trend") == 1
    assert [r.experiment_id for r in registry.list_experiments(strategy_name="other")] != [a]
    assert [r.experiment_id for r in registry.list_experiments(decision="REJECTED")] == [a]
    assert len(registry.list_experiments()) == 2


def test_rejection_is_recorded_with_reason(registry):
    a = registry.record(**record_kwargs())
    registry.record_decision(a, "REJECTED", "worse drawdown", "austin")
    rec = registry.get(a)
    assert rec.promotion_decision == "REJECTED"
    assert rec.rejection_reason == "worse drawdown"
    assert rec.decided_by == "austin" and rec.decided_at is not None


def test_approval_without_a_named_human_is_refused_by_the_registry(registry):
    a = registry.record(**record_kwargs())
    with pytest.raises(PromotionError):
        registry.record_decision(a, "APPROVED", "looks good", "")
    with pytest.raises(PromotionError):
        registry.record_decision(a, "APPROVED", "looks good", None)


# --------------------------------------------------------- hold-out discipline
def test_first_test_evaluation_is_allowed(registry):
    eid = registry.record(**record_kwargs(window="test"))
    evals = registry.test_evaluations_for(eid)
    assert len(evals) == 1 and evals[0]["violation"] is False


def test_second_test_evaluation_is_refused_and_flagged(registry):
    eid = registry.record(**record_kwargs(window="test"))
    with pytest.raises(HoldoutViolation):
        registry.record(**record_kwargs(window="test"))
    evals = registry.test_evaluations_for(eid)
    assert [e["violation"] for e in evals] == [False, True]
    assert registry.count() == 1  # the refused evaluation is not an experiment


def test_retuned_costs_do_not_buy_a_second_look(registry):
    registry.record(**record_kwargs(window="test"))
    with pytest.raises(HoldoutViolation):
        registry.record(**record_kwargs(window="test", transaction_cost_assumptions={"slippage_bps": "1"}))


def test_a_different_parameter_set_has_its_own_single_test_look(registry):
    registry.record(**record_kwargs(window="test"))
    registry.record(**record_kwargs(window="test", parameters={"fast_window": 9, "slow_window": 30}))
    assert registry.count() == 2


def test_train_and_validation_can_be_evaluated_repeatedly(registry):
    registry.record(**record_kwargs(window="train"))
    registry.record(**record_kwargs(window="train", transaction_cost_assumptions={"slippage_bps": "7"}))
    registry.record(**record_kwargs(window="validation"))
    registry.record(**record_kwargs(window="validation", transaction_cost_assumptions={"slippage_bps": "7"}))
    assert registry.count() == 4


def test_unknown_window_is_rejected(registry):
    with pytest.raises(ValidationError):
        registry.record(**record_kwargs(window="future"))


# ----------------------------------------------------------------- experiments
def test_run_experiment_records_costs_actually_used(registry):
    config = split_config()
    engine = make_engine(config)
    run = run_experiment(
        engine, ma_spec(), noisy_history(), config, registry,
        hypothesis="5/20 crossover earns a premium", window="train",
    )
    assert isinstance(run, ExperimentRun)
    rec = registry.get(run.experiment_id)
    assert rec.transaction_cost_assumptions["slippage_bps"] == str(config.execution.slippage_bps)
    assert rec.transaction_cost_assumptions["commission_per_share"] == str(config.execution.commission_per_share)
    assert rec.transaction_cost_assumptions["fill_delay_days"] == config.execution.fill_delay_days
    assert rec.dataset_fingerprint == run.result.dataset_fingerprint
    assert rec.hypothesis == "5/20 crossover earns a premium"
    assert rec.trade_count == run.metrics.trade_count
    assert rec.max_drawdown == pytest.approx(float(run.metrics.max_drawdown))
    assert rec.evidence_level == run.metrics.evidence.value
    assert run.result.end_date <= config.splits.train_end
    assert run.result.start_date >= config.splits.train_start


def test_run_experiment_uses_the_windows_bounds(registry):
    config = split_config()
    run = run_experiment(
        make_engine(config), ma_spec(), noisy_history(), config, registry,
        hypothesis="h", window="validation",
    )
    assert run.result.start_date >= date(2022, 1, 1)
    assert run.result.end_date <= date(2022, 12, 31)


def test_run_experiment_refuses_when_splits_are_undefined(registry):
    config = ResearchConfig()
    with pytest.raises(ValidationError):
        run_experiment(make_engine(config), ma_spec(), noisy_history(), config, registry,
                       hypothesis="h", window="train")
    assert registry.count() == 0


def test_run_experiment_refuses_an_unknown_window(registry):
    config = split_config()
    with pytest.raises(ValidationError):
        run_experiment(make_engine(config), ma_spec(), noisy_history(), config, registry,
                       hypothesis="h", window="holdout")


def test_test_window_can_only_be_run_once(registry):
    config = split_config()
    engine, history = make_engine(config), noisy_history()
    run_experiment(engine, ma_spec(), history, config, registry, hypothesis="h", window="test")
    with pytest.raises(HoldoutViolation):
        run_experiment(engine, ma_spec(), history, config, registry, hypothesis="h", window="test")
    assert registry.count() == 1


def test_rerunning_a_train_experiment_does_not_duplicate_it(registry):
    config = split_config()
    engine, history = make_engine(config), noisy_history()
    a = run_experiment(engine, ma_spec(), history, config, registry, hypothesis="h", window="train")
    b = run_experiment(engine, ma_spec(), history, config, registry, hypothesis="h", window="train")
    assert a.experiment_id == b.experiment_id and registry.count() == 1


# ---------------------------------------------------------------------- tune
def test_tune_sweeps_the_grid_on_train(registry):
    config = split_config()
    out = tune(
        make_engine(config), ma_spec(), noisy_history(), config, registry,
        grid={"fast_window": [3, 5], "slow_window": [20, 30]}, hypothesis="grid",
    )
    assert len(out.trials) == 4
    assert {t.parameters["fast_window"] for t in out.trials} == {3, 5}
    assert registry.count() == 4
    assert out.best in out.trials
    assert all(registry.get(t.experiment_id).promotion_decision == "UNDECIDED" for t in out.trials)


@pytest.mark.parametrize("window", ["validation", "test"])
def test_tune_refuses_anything_but_train(registry, window):
    config = split_config()
    with pytest.raises(ValidationError, match="train"):
        tune(make_engine(config), ma_spec(), noisy_history(), config, registry,
             grid={"fast_window": [3, 5]}, hypothesis="grid", window=window)
    assert registry.count() == 0


def test_tune_rejects_an_empty_grid(registry):
    config = split_config()
    with pytest.raises(ValidationError):
        tune(make_engine(config), ma_spec(), noisy_history(), config, registry,
             grid={}, hypothesis="grid")
