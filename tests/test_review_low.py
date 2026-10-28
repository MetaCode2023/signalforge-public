"""The LOW review findings that are integrity gaps rather than polish.

L1 `TradeLedger.verify()` detects edits and prefix/middle deletions, but NOT
   truncation of the tail: dropping the last N events leaves a structurally
   valid chain. `status()["ledger_intact"]` therefore meant "no accidental
   corruption", not what "immutable history" implies.

L2 `PromotionProposal.__post_init__` refuses APPROVED with a failing check,
   but never verifies the checklist is the gate's COMPLETE set. A forged
   proposal carrying one passing check constructs and `apply_promotion`
   accepts it. `record_decision` can also write "APPROVED" with no proposal.

L3 `Portfolio.__post_init__` does not derive `_applied_fill_ids` from
   `fills`, so a portfolio constructed with pre-existing fills has no replay
   protection for the fills it already holds.

L4 `validation._hard_failures` ran `seen.add(key)` even for a record that had
   already failed, so a quarantined record claimed its (symbol, date) key and
   a LATER VALID record for that key was quarantined as a duplicate.

L5 `registry.record()` is not wrapped in a transaction, so a failure between
   the experiment insert and the test-evaluation log leaves the hold-out
   reading as unspent while the experiment row exists.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.errors import PromotionError, ValidationError
from signalforge.core.types import Fill, Portfolio, Side
from signalforge.paper.ledger import EventType, TradeLedger

D = Decimal
DAY = date(2020, 1, 2)


# ======================================================================
# L1 — ledger truncation
# ======================================================================
def filled_ledger(n: int = 5) -> TradeLedger:
    ledger = TradeLedger()
    for i in range(n):
        ledger.append(EventType.FILL, DAY, {"order_id": f"o{i}"})
    return ledger


class TestLedgerTruncation:
    def test_an_intact_ledger_verifies(self):
        assert filled_ledger().verify() is True

    def test_dropping_the_last_event_is_detected(self):
        ledger = filled_ledger()
        del ledger._events[-1]
        assert ledger.verify() is False, (
            "truncating the tail left a structurally valid chain, so a ledger "
            "could be silently shortened"
        )

    def test_dropping_several_trailing_events_is_detected(self):
        ledger = filled_ledger(8)
        del ledger._events[5:]
        assert ledger.verify() is False

    def test_an_edit_is_still_detected(self):
        ledger = filled_ledger()
        first = ledger._events[0]
        ledger._events[0] = first.__class__(
            sequence=first.sequence,
            event_type=first.event_type,
            event_date=first.event_date,
            payload={"order_id": "TAMPERED"},
            previous_hash=first.previous_hash,
            event_hash=first.event_hash,
        )
        assert ledger.verify() is False

    def test_appending_normally_keeps_it_valid(self):
        ledger = filled_ledger()
        ledger.append(EventType.SESSION_CLOSED, DAY, {"equity": "1"})
        assert ledger.verify() is True

    def test_an_empty_ledger_verifies(self):
        assert TradeLedger().verify() is True

    def test_the_head_is_exposed_for_an_external_witness(self):
        # Recording the head elsewhere is what makes truncation detectable
        # by someone who does not trust this process.
        ledger = filled_ledger()
        head = ledger.head()
        assert head.length == 5
        assert head.event_hash == ledger.events[-1].event_hash

    def test_verifying_against_a_stale_head_fails(self):
        ledger = filled_ledger()
        witness = ledger.head()
        del ledger._events[-1]
        assert ledger.verify_against(witness) is False

    def test_verifying_against_a_matching_head_succeeds(self):
        ledger = filled_ledger()
        assert ledger.verify_against(ledger.head()) is True


# ======================================================================
# L2 — promotion completeness
# ======================================================================
class TestPromotionCannotBeForged:
    def test_a_proposal_with_an_incomplete_checklist_is_refused(self):
        from signalforge.learning.promotion import (
            CheckResult,
            PromotionProposal,
            PromotionStatus,
            REQUIRED_CHECKS,
        )

        assert len(REQUIRED_CHECKS) > 1
        with pytest.raises(Exception):
            PromotionProposal(
                candidate=None,
                baseline=None,
                experiment_id="e",
                status=PromotionStatus.APPROVED,
                reason="forged",
                checks=(CheckResult("anything", True, "d"),),
                approved_by="me",
            )

    def test_the_gates_own_proposals_carry_every_required_check(self):
        from signalforge.learning.promotion import REQUIRED_CHECKS, PromotionGate
        from signalforge.config.models import ResearchConfig

        from tests.test_gauntlet_round3 import _evidence

        gate = PromotionGate(ResearchConfig())
        proposal = gate.evaluate(_evidence(), _evidence(better=False))
        assert {c.name for c in proposal.checks} == set(REQUIRED_CHECKS)

    def test_record_decision_refuses_an_approval_with_no_proposal_evidence(self):
        from signalforge.data.storage import Warehouse
        from signalforge.learning.registry import ExperimentRegistry

        wh = Warehouse(":memory:")
        try:
            registry = ExperimentRegistry(wh)
            with pytest.raises(Exception):
                registry.record_decision("no-such-experiment", "APPROVED", "r", "austin")
        finally:
            wh.close()


# ======================================================================
# L3 — replay protection for a pre-populated book
# ======================================================================
class TestPrePopulatedPortfolio:
    def test_fills_passed_at_construction_cannot_be_replayed(self):
        existing = Fill(
            order_id="o1",
            fill_id="f1",
            symbol="AAA",
            side=Side.BUY,
            quantity=10,
            price=D("100"),
            fees=D("0"),
            slippage_cost=D("0"),
            fill_date=DAY,
        )
        pf = Portfolio(cash=D("99000"), fills=[existing])
        assert pf.apply_fill(existing) is False, (
            "a portfolio built with existing fills had no replay protection for "
            "them, so re-applying one would double-count it"
        )
        assert pf.cash == D("99000")

    def test_a_new_fill_still_applies(self):
        existing = Fill(
            order_id="o1", fill_id="f1", symbol="AAA", side=Side.BUY, quantity=10,
            price=D("100"), fees=D("0"), slippage_cost=D("0"), fill_date=DAY,
        )
        pf = Portfolio(cash=D("99000"), fills=[existing])
        fresh = Fill(
            order_id="o2", fill_id="f2", symbol="AAA", side=Side.BUY, quantity=1,
            price=D("100"), fees=D("0"), slippage_cost=D("0"), fill_date=DAY,
        )
        assert pf.apply_fill(fresh) is True


# ======================================================================
# L4 — a quarantined record must not claim its key
# ======================================================================
class TestQuarantinedRecordDoesNotClaimItsKey:
    def test_a_valid_record_after_an_invalid_one_is_accepted(self):
        from signalforge.data.validation import validate_bar_records

        bad = {
            "symbol": "AAA", "trade_date": date(2026, 1, 5),
            "open": D("9.82"), "high": D("9.81"), "low": D("9.81"),
            "close": D("9.81"), "volume": 100, "adj_close": D("9.81"),
            "source": "s",
        }
        good = {
            "symbol": "AAA", "trade_date": date(2026, 1, 5),
            "open": D("9.80"), "high": D("9.85"), "low": D("9.75"),
            "close": D("9.81"), "volume": 100, "adj_close": D("9.81"),
            "source": "s",
        }
        report = validate_bar_records([bad, good], as_of=date(2026, 2, 1))
        assert len(report.accepted) == 1, (
            "the corrected record was rejected as a duplicate of the broken one"
        )
        assert report.accepted[0].high == D("9.85")

    def test_a_genuine_duplicate_of_a_valid_record_is_still_quarantined(self):
        from signalforge.data.validation import validate_bar_records

        good = {
            "symbol": "AAA", "trade_date": date(2026, 1, 5),
            "open": D("9.80"), "high": D("9.85"), "low": D("9.75"),
            "close": D("9.81"), "volume": 100, "adj_close": D("9.81"),
            "source": "s",
        }
        report = validate_bar_records([good, dict(good)], as_of=date(2026, 2, 1))
        assert len(report.accepted) == 1
        assert len(report.quarantined) == 1

    def test_two_broken_records_for_one_key_are_both_quarantined(self):
        from signalforge.data.validation import validate_bar_records

        bad = {
            "symbol": "AAA", "trade_date": date(2026, 1, 5),
            "open": D("9.82"), "high": D("9.81"), "low": D("9.81"),
            "close": D("9.81"), "volume": 100, "adj_close": D("9.81"),
            "source": "s",
        }
        report = validate_bar_records([bad, dict(bad)], as_of=date(2026, 2, 1))
        assert report.accepted == []
        assert len(report.quarantined) == 2
