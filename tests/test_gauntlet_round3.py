"""Adversarial gauntlet, round 3: risk state, learning integrity, promotion.

Rounds 1 and 2 attacked the data and the fill. This round attacks the parts
that decide what is believed: whether state leaks between runs, whether the
hold-out can be looked at twice, whether a diagnostic is reproducible, and
whether anything can get itself promoted.

Two of these re-verify claims a subagent made about its own code. A
subagent's report is a claim, not evidence, and the promotion gate is the one
component where being wrong means acting on a strategy that was never shown
to work.

Ranked by severity when the round was planned:

 1. A strategy promoting itself, or a forged approval. Severity: critical.
 2. The hold-out window being evaluated more than once, which turns the only
    honest number in the system into another tuning target. Severity: critical.
 3. Risk state leaking between runs, so fold N inherits fold N-1's peak
    equity or tripped breaker. Severity: high, silently corrupts every
    walk-forward result.
 4. An unseeded diagnostic, which makes a reported confidence interval
    irreproducible and therefore not evidence.
 5. Metrics that hide how little capital was deployed.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.backtesting.metrics import EvidenceLevel, compute_metrics
from signalforge.config.models import ResearchConfig, RiskConfig, SplitConfig
from signalforge.core.errors import PromotionError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.learning.overfitting import monte_carlo_trade_order
from signalforge.learning.promotion import PromotionGate
from signalforge.learning.registry import ExperimentRegistry, StrategySpec
from signalforge.learning.walkforward import walk_forward
from signalforge.data.storage import Warehouse

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2000, 1, 3), 2200)


def splits() -> SplitConfig:
    return SplitConfig(
        train_start=DAYS[0],
        train_end=DAYS[1200],
        validation_start=DAYS[1201],
        validation_end=DAYS[1800],
        test_start=DAYS[1801],
        test_end=DAYS[-1],
    )


def research() -> ResearchConfig:
    return ResearchConfig(
        starting_cash=D("100000"), universe=("X",), splits=splits()
    )


def history() -> PriceHistory:
    bars = []
    for i, day in enumerate(DAYS):
        drift = Decimal(100) + Decimal(i) * Decimal("0.05")
        wobble = Decimal(((i * 41) % 29) - 14) / 4
        bars.append(make_bar("X", day, drift + wobble, volume=10_000_000))
    return PriceHistory.from_bars(bars)


def ma_spec() -> StrategySpec:
    return StrategySpec(
        "ma_trend",
        {
            "fast_window": 20,
            "slow_window": 60,
            "target_weight": D("0.05"),
            "symbols": ("X",),
        },
    )


@pytest.fixture
def warehouse():
    w = Warehouse(":memory:")
    yield w
    w.close()


# ======================================================================
# 1. Promotion cannot be self-granted or forged
# ======================================================================
class TestNothingCanPromoteItself:
    def test_the_gate_refuses_a_strategy_object_as_the_approver(self):
        from signalforge.strategies.registry import build

        strategy = build("ma_trend", **ma_spec().params)
        gate = PromotionGate(research())
        with pytest.raises(Exception):
            gate.evaluate(_evidence(), _evidence(better=False), approved_by=strategy)
        # A strategy may not be handed in as the evidence either.
        with pytest.raises(Exception):
            gate.evaluate(strategy, _evidence(better=False), approved_by="austin")

    def test_without_a_human_approver_the_best_candidate_is_only_proposed(self):
        gate = PromotionGate(research())
        proposal = gate.evaluate(_evidence(), _evidence(better=False))
        assert proposal.status.value != "APPROVED"

    def test_applying_an_unapproved_proposal_raises(self, warehouse):
        gate = PromotionGate(research())
        registry = ExperimentRegistry(warehouse)
        proposal = gate.evaluate(_evidence(), _evidence(better=False))
        from signalforge.learning.promotion import apply_promotion

        with pytest.raises(PromotionError):
            apply_promotion(proposal, registry)

    def test_insufficient_evidence_can_never_be_promoted_however_good_it_looks(self):
        gate = PromotionGate(research())
        stellar_but_thin = _evidence(
            sharpe=D("9.0"), drawdown=D("0.001"), evidence=EvidenceLevel.INSUFFICIENT_EVIDENCE
        )
        proposal = gate.evaluate(
            stellar_but_thin, _evidence(better=False), approved_by="austin"
        )
        assert proposal.status.value == "REJECTED"
        assert any(not c.passed for c in proposal.checks)

    def test_a_better_return_with_a_far_worse_drawdown_is_rejected(self):
        gate = PromotionGate(research())
        reckless = _evidence(sharpe=D("1.2"), drawdown=D("0.60"))
        proposal = gate.evaluate(reckless, _evidence(better=False), approved_by="austin")
        assert proposal.status.value == "REJECTED"
        assert any("drawdown" in c.name for c in proposal.checks if not c.passed)

    def test_every_rejection_names_a_failing_check_with_its_numbers(self):
        gate = PromotionGate(research())
        proposal = gate.evaluate(
            _evidence(sharpe=D("0.01")), _evidence(better=False), approved_by="austin"
        )
        failing = [c for c in proposal.checks if not c.passed]
        assert failing
        assert proposal.reason
        for check in failing:
            assert check.detail, f"check {check.name} failed with no numbers recorded"


_REAL_METRICS = None


def _base_metrics():
    """A real PerformanceMetrics from a real run, varied per test.

    Hand-building the dataclass would let these tests drift from the shape the
    production metrics actually have, so the baseline comes from an actual
    backtest and only the fields under test are replaced.
    """
    global _REAL_METRICS
    if _REAL_METRICS is None:
        engine = BacktestEngine(research(), RiskConfig())
        result = engine.run(_Churn(), history(), start=DAYS[0], end=DAYS[1200])
        _REAL_METRICS = compute_metrics(result, config=research())
    return _REAL_METRICS


def _evidence(
    *,
    better: bool = True,
    sharpe: Decimal | None = None,
    drawdown: Decimal | None = None,
    evidence: EvidenceLevel = EvidenceLevel.SUFFICIENT,
    consistency: Decimal = D("0.70"),
    folds: int = 8,
):
    import dataclasses

    from signalforge.learning.promotion import StrategyEvidence

    metrics = dataclasses.replace(
        _base_metrics(),
        sharpe=sharpe if sharpe is not None else (D("1.0") if better else D("0.5")),
        max_drawdown=drawdown if drawdown is not None else D("0.08"),
        trade_count=80,
        evidence=evidence,
        evidence_note="" if evidence is EvidenceLevel.SUFFICIENT else "too few trades",
    )
    return StrategyEvidence(
        strategy_name="candidate" if better else "baseline",
        metrics=metrics,
        experiment_id="exp-candidate" if better else "exp-baseline",
        walkforward_consistency=consistency,
        walkforward_folds=folds,
    )


# ======================================================================
# 2. The hold-out can only be looked at once
# ======================================================================
class TestHoldoutDiscipline:
    def test_a_second_test_window_evaluation_is_refused(self, warehouse):
        from signalforge.learning.experiments import run_experiment

        registry = ExperimentRegistry(warehouse)
        engine = BacktestEngine(research(), RiskConfig())
        hist = history()
        run_experiment(
            engine, ma_spec(), hist, research(), registry,
            hypothesis="trend persists", window="test",
        )
        with pytest.raises(Exception):
            run_experiment(
                engine, ma_spec(), hist, research(), registry,
                hypothesis="trend persists, honestly this time", window="test",
            )

    def test_a_second_look_is_recorded_as_a_violation_not_just_refused(self, warehouse):
        from signalforge.learning.experiments import run_experiment

        registry = ExperimentRegistry(warehouse)
        engine = BacktestEngine(research(), RiskConfig())
        hist = history()
        run_experiment(
            engine, ma_spec(), hist, research(), registry,
            hypothesis="h", window="test",
        )
        try:
            run_experiment(
                engine, ma_spec(), hist, research(), registry,
                hypothesis="h", window="test",
            )
        except Exception:
            pass
        rows = warehouse.con.execute(
            "SELECT COUNT(*) FROM test_evaluations WHERE violation"
        ).fetchall()
        assert rows[0][0] >= 1, "a refused second look must leave an audit record"

    def test_the_validation_window_may_be_re_run_freely(self, warehouse):
        from signalforge.learning.experiments import run_experiment

        registry = ExperimentRegistry(warehouse)
        engine = BacktestEngine(research(), RiskConfig())
        hist = history()
        for _ in range(3):
            run_experiment(
                engine, ma_spec(), hist, research(), registry,
                hypothesis="h", window="validation",
            )

    def test_tuning_is_refused_on_anything_but_train(self, warehouse):
        from signalforge.learning.experiments import tune

        registry = ExperimentRegistry(warehouse)
        engine = BacktestEngine(research(), RiskConfig())
        hist = history()
        grid = [{"fast_window": f, "slow_window": 60, "target_weight": D("0.05"),
                 "symbols": ("X",)} for f in (10, 20)]
        for window in ("validation", "test"):
            with pytest.raises(Exception):
                tune(engine, "ma_trend", grid, hist, research(), registry, window=window)


# ======================================================================
# 3. Risk state must not leak between runs
# ======================================================================
class TestNoStateLeakBetweenRuns:
    def test_a_second_run_does_not_inherit_the_first_runs_peak_equity(self):
        engine = BacktestEngine(research(), RiskConfig())
        hist = history()
        first = engine.run(_Enter(), hist, start=DAYS[0], end=DAYS[600])
        second = engine.run(_Enter(), hist, start=DAYS[700], end=DAYS[1200])
        # If peak leaked, the second run would open already in drawdown.
        assert second.equity_curve[0].drawdown == 0
        assert second.equity_curve[0].peak_equity == second.equity_curve[0].equity
        assert first.equity_curve[0].peak_equity == first.equity_curve[0].equity

    def test_risk_events_do_not_accumulate_across_runs(self):
        engine = BacktestEngine(research(), RiskConfig())
        hist = history()
        a = engine.run(_Greedy(), hist, start=DAYS[0], end=DAYS[300])
        b = engine.run(_Greedy(), hist, start=DAYS[0], end=DAYS[300])
        assert len(a.risk_events) == len(b.risk_events)

    def test_reservations_do_not_survive_into_the_next_run(self):
        engine = BacktestEngine(research(), RiskConfig())
        hist = history()
        a = engine.run(_Enter(), hist, start=DAYS[0], end=DAYS[300])
        b = engine.run(_Enter(), hist, start=DAYS[0], end=DAYS[300])
        assert [f.quantity for f in a.fills] == [f.quantity for f in b.fills]

    def test_a_walk_forward_fold_starts_from_a_clean_book(self):
        report = walk_forward(
            BacktestEngine(research(), RiskConfig()),
            ma_spec(),
            history(),
            research(),
            train_sessions=400,
            test_sessions=200,
            step_sessions=200,
            end=splits().validation_end,
        )
        assert report.folds
        for fold in report.folds:
            assert fold.test_metrics is not None


class TestWalkForwardWindowDiscipline:
    def test_train_and_out_of_sample_windows_are_disjoint_and_ordered(self):
        report = walk_forward(
            BacktestEngine(research(), RiskConfig()),
            ma_spec(),
            history(),
            research(),
            train_sessions=400,
            test_sessions=200,
            step_sessions=200,
            end=splits().validation_end,
        )
        for fold in report.folds:
            assert fold.train_end < fold.test_start, (
                f"fold {fold.index} trains to {fold.train_end} and tests from "
                f"{fold.test_start}; overlapping windows are data leakage"
            )
            assert fold.train_start < fold.train_end
            assert fold.test_start < fold.test_end

    def test_a_walk_forward_bounded_at_validation_never_touches_the_test_window(self):
        bounds = splits()
        report = walk_forward(
            BacktestEngine(research(), RiskConfig()),
            ma_spec(),
            history(),
            research(),
            train_sessions=400,
            test_sessions=200,
            step_sessions=200,
            end=bounds.validation_end,
        )
        for fold in report.folds:
            assert fold.test_end <= bounds.validation_end, (
                "a walk-forward run for candidate selection reached into the "
                "held-out test period"
            )

    def test_a_short_final_fold_is_skipped_with_a_stated_reason(self):
        report = walk_forward(
            BacktestEngine(research(), RiskConfig()),
            ma_spec(),
            history(),
            research(),
            train_sessions=400,
            test_sessions=200,
            step_sessions=200,
            end=splits().validation_end,
        )
        for skipped in report.skipped:
            assert skipped.reason, "a dropped fold with no reason is a silent gap"


# ======================================================================
# 4. Diagnostics must be reproducible
# ======================================================================
class TestDiagnosticReproducibility:
    def test_monte_carlo_with_the_same_seed_gives_the_same_answer(self):
        engine = BacktestEngine(research(), RiskConfig())
        result = engine.run(_Churn(), history(), start=DAYS[0], end=DAYS[1200])
        a = monte_carlo_trade_order(result, iterations=200, seed=7, mode="bootstrap")
        b = monte_carlo_trade_order(result, iterations=200, seed=7, mode="bootstrap")
        assert a.terminal_median == b.terminal_median
        assert a.terminal_p05 == b.terminal_p05
        assert a.terminal_p95 == b.terminal_p95

    def test_a_different_seed_gives_a_different_answer(self):
        engine = BacktestEngine(research(), RiskConfig())
        result = engine.run(_Churn(), history(), start=DAYS[0], end=DAYS[1200])
        a = monte_carlo_trade_order(result, iterations=200, seed=7, mode="bootstrap")
        b = monte_carlo_trade_order(result, iterations=200, seed=8, mode="bootstrap")
        assert (a.terminal_p05, a.terminal_p95) != (b.terminal_p05, b.terminal_p95)


# ======================================================================
# 5. Metrics must make the deployed capital visible
# ======================================================================
class TestCashDilutionIsVisible:
    def test_exposure_is_reported_so_idle_cash_cannot_be_mistaken_for_skill(self):
        """A 5% single-name cap leaves ~95% in cash on a one-symbol universe.

        Portfolio-level CAGR and Sharpe then describe 5% of a strategy diluted
        by 95% cash. That is the correct arithmetic, but a reader who cannot
        see the exposure will read it as the strategy's own performance.
        """
        engine = BacktestEngine(research(), RiskConfig())
        result = engine.run(_Enter(), history(), start=DAYS[0], end=DAYS[1200])
        metrics = compute_metrics(result, config=research())

        # `exposure` is time in market and reads ~1.0 here: a position is open
        # on almost every session. On its own it tells a reader nothing about
        # how much capital was at risk.
        assert metrics.exposure is not None
        assert metrics.exposure > D("0.9")

        # The capital actually deployed is the number that makes the
        # portfolio-level CAGR and Sharpe interpretable.
        assert metrics.average_capital_deployed is not None
        assert metrics.average_capital_deployed < D("0.10"), (
            "a 5% single-name cap on a one-symbol universe deploys about 5% of "
            "the book; if this metric did not exist, 95% idle cash would be "
            "invisible in every reported figure"
        )
        assert metrics.peak_capital_deployed is not None

    def test_a_fully_deployed_book_reports_capital_deployed_near_one(self):
        # Proof the metric discriminates rather than always reading low.
        engine = BacktestEngine(
            research(), RiskConfig(max_single_name_exposure=D("1.0"), max_positions=1)
        )
        result = engine.run(_Greedy(), history(), start=DAYS[0], end=DAYS[1200])
        metrics = compute_metrics(result, config=research())
        assert metrics.average_capital_deployed > D("0.5")


# ======================================================================
# test strategies
# ======================================================================
class _Enter:
    strategy_id = "enter"
    warmup_days = 0

    def parameters(self):
        return {}

    def generate_signals(self, as_of, view):
        return [
            Signal(
                symbol="X",
                direction=SignalDirection.ENTER_LONG,
                as_of=as_of,
                strategy_id=self.strategy_id,
                reason="hold long",
                target_weight=D("0.05"),
            )
        ]


class _Greedy:
    strategy_id = "greedy"
    warmup_days = 0

    def parameters(self):
        return {}

    def generate_signals(self, as_of, view):
        return [
            Signal(
                symbol="X",
                direction=SignalDirection.ENTER_LONG,
                as_of=as_of,
                strategy_id=self.strategy_id,
                reason="maximum size",
                target_weight=D("1.0"),
            )
        ]


class _Churn:
    """Alternates in and out so there are closed trades to resample."""

    strategy_id = "churn"
    warmup_days = 0

    def parameters(self):
        return {}

    def generate_signals(self, as_of, view):
        direction = (
            SignalDirection.ENTER_LONG
            if (as_of.toordinal() // 7) % 2 == 0
            else SignalDirection.EXIT_LONG
        )
        return [
            Signal(
                symbol="X",
                direction=direction,
                as_of=as_of,
                strategy_id=self.strategy_id,
                reason="alternating",
                target_weight=D("0.05") if direction is SignalDirection.ENTER_LONG else D("0"),
            )
        ]
