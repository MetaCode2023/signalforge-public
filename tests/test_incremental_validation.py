"""Cross-batch continuity: the blind spot an incremental pull would open.

The jump, stale-price and adjustment rules compare each bar with the previous
bar IN THE SAME BATCH, and skip the first bar because it has no predecessor.
That is harmless while all data arrives in one archive ingest. It becomes a
real hole the moment a daily incremental pull starts: a split-sized jump
between yesterday's stored close and today's new bar lands exactly on the
first bar of the batch and is never flagged.

The validator therefore has to be told what the last stored bar was.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.data.validation import (
    INCONSISTENT_ADJUSTMENT,
    STALE_PRICE,
    SUSPICIOUS_PRICE_JUMP,
    validate_bar_records,
)
from signalforge.data.storage import Warehouse

from tests.conftest import make_bar

D = Decimal
AS_OF = date(2026, 1, 30)


def record(day: date, close: str, *, symbol="AAA", volume=1_000_000,
           adj=None, source="s"):
    c = D(close)
    return {
        "symbol": symbol,
        "trade_date": day,
        "open": c,
        "high": c,
        "low": c,
        "close": c,
        "volume": volume,
        "adj_close": D(adj) if adj is not None else c,
        "source": source,
        "retrieved_at": None,
        "source_reference": "test",
    }


def rules_fired(report) -> set[str]:
    return {f.rule for f in report.flags}


class TestJumpAcrossBatches:
    def test_a_jump_from_the_last_stored_bar_is_flagged(self):
        prior = make_bar("AAA", date(2026, 1, 5), D("100"), volume=1_000_000, source="s")
        batch = [record(date(2026, 1, 6), "20")]  # 80% drop overnight
        report = validate_bar_records(
            batch, as_of=AS_OF, prior_bars={"AAA": prior}
        )
        assert SUSPICIOUS_PRICE_JUMP in rules_fired(report)

    def test_the_flag_names_the_stored_prior_close_and_its_date(self):
        prior = make_bar("AAA", date(2026, 1, 5), D("100"), volume=1_000_000, source="s")
        report = validate_bar_records(
            [record(date(2026, 1, 6), "20")], as_of=AS_OF, prior_bars={"AAA": prior}
        )
        detail = next(f.detail for f in report.flags if f.rule == SUSPICIOUS_PRICE_JUMP)
        assert "100" in detail
        assert "2026-01-05" in detail

    def test_without_prior_bars_the_same_jump_goes_unflagged(self):
        # This is the bug being fixed, pinned so it cannot come back silently.
        report = validate_bar_records([record(date(2026, 1, 6), "20")], as_of=AS_OF)
        assert SUSPICIOUS_PRICE_JUMP not in rules_fired(report)

    def test_a_normal_overnight_move_is_not_flagged(self):
        prior = make_bar("AAA", date(2026, 1, 5), D("100"), volume=1_000_000, source="s")
        report = validate_bar_records(
            [record(date(2026, 1, 6), "101")], as_of=AS_OF, prior_bars={"AAA": prior}
        )
        assert SUSPICIOUS_PRICE_JUMP not in rules_fired(report)

    def test_a_prior_bar_for_another_symbol_is_not_applied(self):
        prior = make_bar("ZZZ", date(2026, 1, 5), D("100"), volume=1_000_000, source="s")
        report = validate_bar_records(
            [record(date(2026, 1, 6), "20")], as_of=AS_OF, prior_bars={"ZZZ": prior}
        )
        assert SUSPICIOUS_PRICE_JUMP not in rules_fired(report)

    def test_a_prior_bar_dated_after_the_batch_is_ignored(self):
        # Defensive: a stale cache must not be used as a "previous" bar.
        prior = make_bar("AAA", date(2026, 2, 1), D("100"), volume=1_000_000, source="s")
        report = validate_bar_records(
            [record(date(2026, 1, 6), "20")], as_of=AS_OF, prior_bars={"AAA": prior}
        )
        assert SUSPICIOUS_PRICE_JUMP not in rules_fired(report)

    def test_the_prior_bar_is_not_added_to_the_accepted_output(self):
        prior = make_bar("AAA", date(2026, 1, 5), D("100"), volume=1_000_000, source="s")
        report = validate_bar_records(
            [record(date(2026, 1, 6), "101")], as_of=AS_OF, prior_bars={"AAA": prior}
        )
        assert [b.trade_date for b in report.accepted] == [date(2026, 1, 6)]


class TestStaleRunAcrossBatches:
    def test_a_stale_run_continuing_from_storage_is_flagged(self):
        # Four identical closes already stored, one more arrives: that is 5.
        prior = make_bar("AAA", date(2026, 1, 5), D("50"), volume=1_000_000, source="s")
        report = validate_bar_records(
            [record(date(2026, 1, 6), "50")],
            as_of=AS_OF,
            prior_bars={"AAA": prior},
            prior_stale_runs={"AAA": 4},
        )
        assert STALE_PRICE in rules_fired(report)

    def test_a_broken_run_resets_rather_than_accumulating(self):
        prior = make_bar("AAA", date(2026, 1, 5), D("50"), volume=1_000_000, source="s")
        report = validate_bar_records(
            [record(date(2026, 1, 6), "51")],
            as_of=AS_OF,
            prior_bars={"AAA": prior},
            prior_stale_runs={"AAA": 4},
        )
        assert STALE_PRICE not in rules_fired(report)

    def test_a_short_carried_run_does_not_trip_early(self):
        prior = make_bar("AAA", date(2026, 1, 5), D("50"), volume=1_000_000, source="s")
        report = validate_bar_records(
            [record(date(2026, 1, 6), "50")],
            as_of=AS_OF,
            prior_bars={"AAA": prior},
            prior_stale_runs={"AAA": 1},
        )
        assert STALE_PRICE not in rules_fired(report)


class TestAdjustmentAcrossBatches:
    def test_an_adjustment_ratio_shift_from_storage_is_flagged(self):
        prior = make_bar(
            "AAA", date(2026, 1, 5), D("100"), volume=1_000_000, adj_close=D("100"), source="s"
        )
        batch = [record(date(2026, 1, 6), "100", adj="80")]  # ratio 1.00 -> 0.80
        report = validate_bar_records(
            batch, as_of=AS_OF, prior_bars={"AAA": prior}
        )
        assert INCONSISTENT_ADJUSTMENT in rules_fired(report)

    def test_a_recorded_corporate_action_suppresses_the_flag(self):
        prior = make_bar(
            "AAA", date(2026, 1, 5), D("100"), volume=1_000_000, adj_close=D("100"), source="s"
        )
        report = validate_bar_records(
            [record(date(2026, 1, 6), "100", adj="80")],
            as_of=AS_OF,
            prior_bars={"AAA": prior},
            corporate_action_dates={("AAA", date(2026, 1, 6))},
        )
        assert INCONSISTENT_ADJUSTMENT not in rules_fired(report)


class TestMissingObservationsAcrossBatches:
    def test_a_gap_between_storage_and_the_batch_is_counted(self):
        prior = make_bar("AAA", date(2026, 1, 5), D("100"), volume=1_000_000, source="s")
        # Next bar is two weeks later; the weekdays between are missing.
        report = validate_bar_records(
            [record(date(2026, 1, 19), "100")], as_of=AS_OF, prior_bars={"AAA": prior}
        )
        assert report.missing.get("AAA", 0) > 0


class TestWarehouseSuppliesThePriorBars:
    def test_latest_bars_returns_the_most_recent_stored_bar_per_symbol(self):
        wh = Warehouse(":memory:")
        try:
            wh.upsert_bars(
                [
                    make_bar("AAA", date(2026, 1, 2), D("10"), volume=1, source="s"),
                    make_bar("AAA", date(2026, 1, 5), D("12"), volume=1, source="s"),
                    make_bar("BBB", date(2026, 1, 3), D("20"), volume=1, source="s"),
                ]
            )
            latest = wh.latest_bars(["AAA", "BBB"])
            assert latest["AAA"].trade_date == date(2026, 1, 5)
            assert latest["AAA"].close == D("12")
            assert latest["BBB"].trade_date == date(2026, 1, 3)
        finally:
            wh.close()

    def test_latest_bars_omits_a_symbol_with_no_stored_bars(self):
        wh = Warehouse(":memory:")
        try:
            assert wh.latest_bars(["NOPE"]) == {}
        finally:
            wh.close()

    def test_latest_bars_with_no_symbols_returns_every_symbol(self):
        wh = Warehouse(":memory:")
        try:
            wh.upsert_bars(
                [make_bar("AAA", date(2026, 1, 2), D("10"), volume=1, source="s")]
            )
            assert set(wh.latest_bars()) == {"AAA"}
        finally:
            wh.close()

    def test_stale_runs_counts_the_trailing_identical_closes(self):
        wh = Warehouse(":memory:")
        try:
            wh.upsert_bars(
                [
                    make_bar("AAA", date(2026, 1, 2), D("9"), volume=1, source="s"),
                    make_bar("AAA", date(2026, 1, 5), D("10"), volume=1, source="s"),
                    make_bar("AAA", date(2026, 1, 6), D("10"), volume=1, source="s"),
                    make_bar("AAA", date(2026, 1, 7), D("10"), volume=1, source="s"),
                ]
            )
            assert wh.stale_runs(["AAA"])["AAA"] == 3
        finally:
            wh.close()
