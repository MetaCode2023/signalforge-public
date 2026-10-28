"""The promotion gate.

A strategy never promotes itself: the gate sees metrics and a named human, never
a strategy object. Approval requires every criterion to pass AND a human; a
human cannot approve past a failed criterion.
"""

import inspect
from decimal import Decimal

import pytest

from signalforge.backtesting.metrics import EvidenceLevel, PerformanceMetrics
from signalforge.config.models import ResearchConfig
from signalforge.core.errors import PromotionError, ValidationError
from signalforge.data.storage import Warehouse
from signalforge.learning.promotion import (
    CheckResult,
    PromotionCriteria,
    PromotionGate,
    PromotionProposal,
    PromotionStatus,
    StrategyEvidence,
    apply_promotion,
)
from signalforge.learning.registry import ExperimentRegistry

from tests.test_learning_registry import ma_spec, record_kwargs

D = Decimal


def make_metrics(sharpe="1.2", max_dd="0.10", total_return="0.30",
                 evidence=EvidenceLevel.SUFFICIENT) -> PerformanceMetrics:
    return PerformanceMetrics(
        observations=600, total_return=D(total_return), cagr=None, annualised_volatility=None,
        sharpe=None if sharpe is None else D(sharpe), sortino=None, max_drawdown=D(max_dd),
        max_drawdown_duration_days=0, max_drawdown_duration_sessions=0, calmar=None,
        trade_count=45, fill_count=90, win_rate=None, profit_factor=None, avg_trade_return=None,
        turnover=None, exposure=None,
        average_capital_deployed=D("0.05"), peak_capital_deployed=D("0.05"),
        total_transaction_costs=D(0), total_slippage=D(0),
        benchmark_relative_return=None, tracking_error=None, information_ratio=None,
        evidence=evidence,
        evidence_note="" if evidence is EvidenceLevel.SUFFICIENT else "closed trade count 4 is below the minimum of 30",
    )


def evidence(name="cand", consistency="0.8", folds=6, experiment_id=None, **metric_kw):
    return StrategyEvidence(
        strategy_name=name, metrics=make_metrics(**metric_kw), experiment_id=experiment_id,
        walkforward_consistency=None if consistency is None else D(consistency),
        walkforward_folds=folds,
    )


BASELINE = lambda: evidence("buy_and_hold", sharpe="0.8", max_dd="0.10", total_return="0.20")  # noqa: E731


@pytest.fixture
def gate():
    return PromotionGate(ResearchConfig())


def failed(proposal):
    return {c.name for c in proposal.checks if not c.passed}


# ------------------------------------------------------------ the happy path
def test_a_clean_candidate_without_an_approver_is_only_proposed(gate):
    proposal = gate.evaluate(evidence(), BASELINE())
    assert proposal.status is PromotionStatus.PROPOSED
    assert proposal.approved_by is None
    assert all(c.passed for c in proposal.checks)
    assert "awaiting" in proposal.reason.lower()


def test_a_clean_candidate_with_a_named_human_is_approved(gate):
    proposal = gate.evaluate(evidence(), BASELINE(), approved_by="austin")
    assert proposal.status is PromotionStatus.APPROVED
    assert proposal.approved_by == "austin"
    assert "austin" in proposal.reason


@pytest.mark.parametrize("approver", [None, "", "   "])
def test_promotion_without_a_human_is_never_approved(gate, approver):
    proposal = gate.evaluate(evidence(), BASELINE(), approved_by=approver)
    assert proposal.status is PromotionStatus.PROPOSED


# --------------------------------------------- strategies cannot promote themselves
def test_the_api_takes_metrics_and_a_human_not_a_strategy():
    params = list(inspect.signature(PromotionGate.evaluate).parameters)
    assert params == ["self", "candidate", "baseline", "approved_by"]


def test_a_strategy_object_is_not_accepted_as_a_candidate(gate):
    strategy = ma_spec().build()
    with pytest.raises(ValidationError):
        gate.evaluate(strategy, BASELINE())
    with pytest.raises(ValidationError):
        gate.evaluate(evidence(), strategy)


def test_a_strategy_object_is_not_accepted_as_the_approver(gate):
    strategy = ma_spec().build()
    with pytest.raises(ValidationError):
        gate.evaluate(evidence(), BASELINE(), approved_by=strategy)


def test_a_candidate_cannot_approve_itself_by_name(gate):
    with pytest.raises(ValidationError, match="itself"):
        gate.evaluate(evidence("ma_trend"), BASELINE(), approved_by="ma_trend")


# ------------------------------------------------------------ apply_promotion
@pytest.fixture
def registry():
    w = Warehouse(":memory:")
    yield ExperimentRegistry(w)
    w.close()


def test_apply_promotion_writes_the_approval_to_the_registry(gate, registry):
    eid = registry.record(**record_kwargs())
    proposal = gate.evaluate(evidence(experiment_id=eid), BASELINE(), approved_by="austin")
    record = apply_promotion(proposal, registry)
    assert record.promotion_decision == "APPROVED"
    assert record.decided_by == "austin"
    assert record.promotion_reason == proposal.reason


def test_apply_promotion_refuses_a_merely_proposed_candidate(gate, registry):
    eid = registry.record(**record_kwargs())
    proposal = gate.evaluate(evidence(experiment_id=eid), BASELINE())
    with pytest.raises(PromotionError):
        apply_promotion(proposal, registry)
    assert registry.get(eid).promotion_decision == "UNDECIDED"


def test_apply_promotion_refuses_a_rejected_candidate_even_with_an_approver(gate, registry):
    eid = registry.record(**record_kwargs())
    proposal = gate.evaluate(evidence(experiment_id=eid, sharpe="0.1"), BASELINE(), approved_by="austin")
    assert proposal.status is PromotionStatus.REJECTED
    with pytest.raises(PromotionError):
        apply_promotion(proposal, registry)
    assert registry.get(eid).promotion_decision == "UNDECIDED"


def test_an_approved_proposal_cannot_be_forged_without_a_human():
    checks = (CheckResult("x", True, "ok", {"a": 1}),)
    with pytest.raises(PromotionError):
        PromotionProposal(
            candidate="c", baseline="b", experiment_id=None, status=PromotionStatus.APPROVED,
            reason="trust me", checks=checks, approved_by=None,
        )


def test_apply_promotion_needs_an_experiment_to_attach_the_decision_to(gate, registry):
    proposal = gate.evaluate(evidence(experiment_id=None), BASELINE(), approved_by="austin")
    with pytest.raises(PromotionError, match="experiment"):
        apply_promotion(proposal, registry)


# ------------------------------------------------------------------ the rules
def test_higher_return_with_much_worse_drawdown_is_rejected_for_drawdown(gate):
    greedy = evidence(sharpe="1.5", total_return="0.90", max_dd="0.35")
    proposal = gate.evaluate(greedy, BASELINE(), approved_by="austin")
    assert proposal.status is PromotionStatus.REJECTED
    assert "max_drawdown" in failed(proposal)
    assert "drawdown" in proposal.reason.lower()
    check = next(c for c in proposal.checks if c.name == "max_drawdown")
    assert check.values["candidate"] == pytest.approx(0.35)
    assert check.values["baseline"] == pytest.approx(0.10)


def test_drawdown_tolerance_boundary(gate):
    allowed = 0.10 * 1.25 + 0.01
    just_in = gate.evaluate(evidence(max_dd=str(round(allowed - 0.001, 4))), BASELINE())
    just_out = gate.evaluate(evidence(max_dd=str(round(allowed + 0.001, 4))), BASELINE())
    assert "max_drawdown" not in failed(just_in)
    assert "max_drawdown" in failed(just_out)


def test_total_return_alone_does_not_win_the_comparison(gate):
    lottery = evidence(sharpe="0.4", total_return="2.00", max_dd="0.10")
    proposal = gate.evaluate(lottery, BASELINE())
    assert proposal.status is PromotionStatus.REJECTED
    assert "risk_adjusted_return" in failed(proposal)
    assert "risk-adjusted" in proposal.reason


def test_a_candidate_must_strictly_beat_the_baseline_sharpe(gate):
    proposal = gate.evaluate(evidence(sharpe="0.8"), BASELINE())
    assert "risk_adjusted_return" in failed(proposal)


def test_required_sharpe_improvement_is_configurable():
    strict = PromotionGate(ResearchConfig(), PromotionCriteria(min_sharpe_improvement=0.5))
    assert "risk_adjusted_return" in failed(strict.evaluate(evidence(sharpe="1.1"), BASELINE()))
    assert "risk_adjusted_return" not in failed(strict.evaluate(evidence(sharpe="1.4"), BASELINE()))


def test_undefined_sharpe_cannot_beat_anything(gate):
    assert "risk_adjusted_return" in failed(gate.evaluate(evidence(sharpe=None), BASELINE()))
    assert "risk_adjusted_return" in failed(
        gate.evaluate(evidence(), evidence("buy_and_hold", sharpe=None))
    )


def test_insufficient_evidence_can_never_be_promoted(gate):
    dazzling = evidence(sharpe="9.0", total_return="5.00", max_dd="0.01",
                        evidence=EvidenceLevel.INSUFFICIENT_EVIDENCE)
    for approver in (None, "austin"):
        proposal = gate.evaluate(dazzling, BASELINE(), approved_by=approver)
        assert proposal.status is PromotionStatus.REJECTED
        assert "evidence" in failed(proposal)
        assert "INSUFFICIENT_EVIDENCE" in proposal.reason


def test_walkforward_consistency_below_the_floor_is_rejected(gate):
    proposal = gate.evaluate(evidence(consistency="0.4"), BASELINE())
    assert proposal.status is PromotionStatus.REJECTED
    assert "walkforward_consistency" in failed(proposal)
    check = next(c for c in proposal.checks if c.name == "walkforward_consistency")
    assert check.values["candidate"] == pytest.approx(0.4) and check.values["floor"] == pytest.approx(0.6)


def test_the_consistency_floor_is_configurable():
    lenient = PromotionGate(ResearchConfig(), PromotionCriteria(min_walkforward_consistency=0.3))
    assert lenient.evaluate(evidence(consistency="0.4"), BASELINE()).status is PromotionStatus.PROPOSED


def test_too_few_walkforward_folds_is_rejected_whatever_the_fraction(gate):
    proposal = gate.evaluate(evidence(consistency="1.0", folds=2), BASELINE())
    assert "walkforward_evidence" in failed(proposal)


def test_missing_walkforward_evidence_is_rejected(gate):
    proposal = gate.evaluate(evidence(consistency=None, folds=None), BASELINE())
    assert proposal.status is PromotionStatus.REJECTED
    assert {"walkforward_evidence", "walkforward_consistency"} <= failed(proposal)


# ------------------------------------------------------ reasons and the audit trail
@pytest.mark.parametrize("candidate,approver", [
    (evidence(), None),
    (evidence(), "austin"),
    (evidence(sharpe="0.1"), None),
    (evidence(max_dd="0.5"), "austin"),
    (evidence(evidence=EvidenceLevel.INSUFFICIENT_EVIDENCE), None),
])
def test_every_proposal_has_a_reason_and_a_full_checklist(gate, candidate, approver):
    proposal = gate.evaluate(candidate, BASELINE(), approved_by=approver)
    assert proposal.reason.strip()
    assert {c.name for c in proposal.checks} == {
        "evidence", "walkforward_evidence", "walkforward_consistency",
        "risk_adjusted_return", "max_drawdown",
    }
    for c in proposal.checks:
        assert isinstance(c.passed, bool) and c.detail.strip() and c.values


def test_a_rejection_names_every_failed_criterion(gate):
    proposal = gate.evaluate(evidence(sharpe="0.1", max_dd="0.5", consistency="0.1"), BASELINE())
    assert proposal.status is PromotionStatus.REJECTED
    for name in ("risk_adjusted_return", "max_drawdown", "walkforward_consistency"):
        assert name in proposal.reason


def test_a_proposal_cannot_be_built_with_a_blank_reason_or_no_checks():
    ok = (CheckResult("x", True, "ok", {"a": 1}),)
    with pytest.raises(ValidationError):
        PromotionProposal("c", "b", None, PromotionStatus.PROPOSED, "  ", ok, None)
    with pytest.raises(ValidationError):
        PromotionProposal("c", "b", None, PromotionStatus.PROPOSED, "fine", (), None)
    with pytest.raises(ValidationError):
        PromotionProposal("c", "b", None, PromotionStatus.REJECTED, "failed", ok, None)
