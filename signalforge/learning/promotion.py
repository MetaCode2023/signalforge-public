"""The promotion gate.

Promotion is a human decision that a gate can only make harder to get wrong.

  * The gate takes evidence (metrics) and a named approver, never a strategy
    object, so strategy code has no path to it and cannot promote itself.
  * Every criterion is evaluated and recorded with the numbers behind it. One
    failure rejects; a human approver cannot approve past a failed criterion.
  * All criteria passing yields PROPOSED. Only all criteria plus a named human
    yields APPROVED, and only an APPROVED proposal can be applied.
  * The comparison is risk-adjusted (Sharpe) with a drawdown guard. Total return
    is reported but never decides anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from signalforge.backtesting.metrics import EvidenceLevel, PerformanceMetrics
from signalforge.core.errors import PromotionError, ValidationError


class PromotionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class PromotionCriteria:
    min_walkforward_consistency: float = 0.6
    min_walkforward_folds: int = 3
    min_sharpe_improvement: float = 0.0
    drawdown_relative_tolerance: float = 0.25
    drawdown_absolute_slack: float = 0.01


@dataclass(frozen=True)
class StrategyEvidence:
    """What the gate sees of a strategy: results, never the strategy itself."""

    strategy_name: str
    metrics: PerformanceMetrics
    experiment_id: str | None = None
    walkforward_consistency: Decimal | float | None = None
    walkforward_folds: int | None = None


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    values: dict = field(default_factory=dict)


REQUIRED_CHECKS = (
    "evidence",
    "walkforward_evidence",
    "walkforward_consistency",
    "risk_adjusted_return",
    "max_drawdown",
)
"""Every criterion the gate evaluates.

An APPROVED proposal must carry all of them. Validating only that no check
FAILED left a forged proposal carrying a single passing check acceptable to
apply_promotion: the absent checks read as if they had passed.
"""


@dataclass(frozen=True)
class PromotionProposal:
    candidate: str
    baseline: str
    experiment_id: str | None
    status: PromotionStatus
    reason: str
    checks: tuple
    approved_by: str | None = None

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise ValidationError("a promotion proposal must carry a human-readable reason")
        if not self.checks:
            raise ValidationError("a promotion proposal must carry its checklist")
        all_passed = all(c.passed for c in self.checks)
        if self.status is PromotionStatus.REJECTED and all_passed:
            raise ValidationError("a rejection must point at a failed check")
        if self.status is not PromotionStatus.REJECTED and not all_passed:
            raise PromotionError("a proposal with a failed check cannot be proposed or approved")
        if self.status is PromotionStatus.APPROVED and not _named(self.approved_by):
            raise PromotionError("an approved proposal must name the human who approved it")
        if self.status is not PromotionStatus.REJECTED:
            present = {c.name for c in self.checks}
            missing = set(REQUIRED_CHECKS) - present
            if missing:
                raise PromotionError(
                    "a proposal cannot be advanced with an incomplete checklist; "
                    f"missing {sorted(missing)}. An absent check is not a passing "
                    "one, and treating it as one is how a forged approval gets in."
                )


def _named(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _f(value) -> float | None:
    return None if value is None else float(value)


class PromotionGate:
    def __init__(self, config, criteria: PromotionCriteria | None = None) -> None:
        self.config = config
        self.criteria = criteria or PromotionCriteria()

    def evaluate(self, candidate: StrategyEvidence, baseline: StrategyEvidence,
                 approved_by: str | None = None) -> PromotionProposal:
        for label, item in (("candidate", candidate), ("baseline", baseline)):
            if not isinstance(item, StrategyEvidence):
                raise ValidationError(
                    f"{label} must be StrategyEvidence (metrics), not {type(item).__name__}: "
                    "the gate never receives a strategy"
                )
        if approved_by is not None and not isinstance(approved_by, str):
            raise ValidationError(
                f"approved_by must be a human's name, not {type(approved_by).__name__}"
            )
        approver = approved_by.strip() if approved_by and approved_by.strip() else None
        if approver and approver.lower() == candidate.strategy_name.strip().lower():
            raise ValidationError(
                f"{approver!r} is the candidate strategy: a strategy cannot approve itself"
            )

        checks = tuple(self._checks(candidate, baseline))
        failures = [c for c in checks if not c.passed]
        if failures:
            status = PromotionStatus.REJECTED
            reason = "Rejected: " + "; ".join(f"{c.name}: {c.detail}" for c in failures)
        elif approver:
            status = PromotionStatus.APPROVED
            reason = (f"Approved by {approver}: {candidate.strategy_name} meets all "
                      f"{len(checks)} criteria against {baseline.strategy_name}.")
        else:
            status = PromotionStatus.PROPOSED
            reason = (f"Meets all {len(checks)} criteria against {baseline.strategy_name}; "
                      "awaiting human approval.")
        return PromotionProposal(
            candidate=candidate.strategy_name, baseline=baseline.strategy_name,
            experiment_id=candidate.experiment_id, status=status, reason=reason,
            checks=checks, approved_by=approver if status is PromotionStatus.APPROVED else None,
        )

    def _checks(self, cand: StrategyEvidence, base: StrategyEvidence):
        c = self.criteria
        cm, bm = cand.metrics, base.metrics

        sufficient = cm.evidence is EvidenceLevel.SUFFICIENT
        yield CheckResult(
            "evidence", sufficient,
            "evidence is SUFFICIENT" if sufficient else
            f"candidate is {cm.evidence.value} ({cm.evidence_note}); thin evidence is never "
            "promotable, however good the numbers look",
            {"trade_count": cm.trade_count, "observations": cm.observations,
             "level": cm.evidence.value},
        )

        folds = cand.walkforward_folds
        have_folds = folds is not None and folds >= c.min_walkforward_folds
        yield CheckResult(
            "walkforward_evidence", have_folds,
            f"{folds} walk-forward fold(s), minimum {c.min_walkforward_folds}" if folds is not None
            else "no walk-forward evidence supplied",
            {"folds": folds, "minimum": c.min_walkforward_folds},
        )

        consistency = _f(cand.walkforward_consistency)
        consistent = consistency is not None and consistency >= c.min_walkforward_consistency
        yield CheckResult(
            "walkforward_consistency", consistent,
            f"out-of-sample consistency {consistency:.2f} vs floor {c.min_walkforward_consistency:.2f}"
            if consistency is not None else "out-of-sample consistency is not available",
            {"candidate": consistency, "floor": c.min_walkforward_consistency},
        )

        cs, bs = _f(cm.sharpe), _f(bm.sharpe)
        beats = cs is not None and bs is not None and cs > bs + c.min_sharpe_improvement
        if cs is None or bs is None:
            detail = ("risk-adjusted comparison impossible: Sharpe is undefined for "
                      f"{'the candidate' if cs is None else 'the baseline'}")
        elif beats:
            detail = f"risk-adjusted: Sharpe {cs:.2f} beats baseline {bs:.2f} by more than {c.min_sharpe_improvement:.2f}"
        else:
            detail = (f"risk-adjusted: Sharpe {cs:.2f} does not beat baseline {bs:.2f} by more than "
                      f"{c.min_sharpe_improvement:.2f} (total return {_f(cm.total_return)} vs "
                      f"{_f(bm.total_return)} does not decide this)")
        yield CheckResult(
            "risk_adjusted_return", beats, detail,
            {"candidate": cs, "baseline": bs, "required_improvement": c.min_sharpe_improvement,
             "candidate_total_return": _f(cm.total_return), "baseline_total_return": _f(bm.total_return)},
        )

        cd, bd = float(cm.max_drawdown), float(bm.max_drawdown)
        allowed = bd * (1 + c.drawdown_relative_tolerance) + c.drawdown_absolute_slack
        within = cd <= allowed
        yield CheckResult(
            "max_drawdown", within,
            f"max drawdown {cd:.3f} vs allowed {allowed:.3f} (baseline {bd:.3f})" if within else
            f"max drawdown {cd:.3f} is materially worse than baseline {bd:.3f} (allowed {allowed:.3f})",
            {"candidate": cd, "baseline": bd, "allowed": allowed},
        )


def apply_promotion(proposal: PromotionProposal, registry):
    """Record an approval. Raises PromotionError unless a human approved a passing proposal."""
    if not isinstance(proposal, PromotionProposal):
        raise PromotionError("apply_promotion needs a PromotionProposal")
    if proposal.status is not PromotionStatus.APPROVED:
        raise PromotionError(
            f"proposal is {proposal.status.value}, not APPROVED: promotion needs an explicit human approval"
        )
    if not _named(proposal.approved_by) or not all(c.passed for c in proposal.checks):
        raise PromotionError("proposal does not carry a human approval over a fully passing checklist")
    if not proposal.experiment_id:
        raise PromotionError("proposal names no experiment to attach the decision to")
    registry.record_decision(proposal.experiment_id, "APPROVED", proposal.reason, proposal.approved_by)
    return registry.get(proposal.experiment_id)
