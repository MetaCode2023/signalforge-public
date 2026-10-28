from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from signalforge.core.types import QualityStatus
from signalforge.data import validation as v
from signalforge.data.validation import validate_bar_records, validate_series_records
from tests.conftest import business_days

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
AS_OF = date(2026, 10, 26)


def rec(symbol="AAA", d=date(2024, 1, 2), close="100", **over):
    base = dict(
        symbol=symbol, trade_date=d, open=close, high=close, low=close, close=close,
        volume=1000, adj_close=close, source="t", source_reference="mem",
        retrieved_at=NOW, adjustment_method="none",
    )
    base.update(over)
    return base


def closes_series(closes, symbol="AAA", start=date(2024, 1, 2), **over):
    days = business_days(start, len(closes))
    return [rec(symbol, d, str(c), **over) for d, c in zip(days, closes)]


def run(records, **kw):
    kw.setdefault("as_of", AS_OF)
    return validate_bar_records(records, **kw)


def test_clean_records_are_accepted_ok():
    report = run(closes_series([100, 101, 102]))
    assert len(report.accepted) == 3
    assert report.quarantined == [] and report.flags == []
    assert all(report.status_of(b) is QualityStatus.OK for b in report.accepted)
    assert report.accepted[0].close == Decimal("100")


def test_duplicate_symbol_date_quarantines_later_copy():
    a, b = rec(), rec(close="101", high="101")
    report = run([a, b])
    assert len(report.accepted) == 1
    assert report.accepted[0].close == Decimal("100")
    assert [q.rule for q in report.quarantined] == [v.DUPLICATE_SYMBOL_DATE]


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_negative_or_zero_price(bad):
    report = run([rec(close=bad, open=bad, high=bad, low=bad)])
    assert report.accepted == []
    assert report.quarantined[0].rule == v.NEGATIVE_OR_ZERO_PRICE


@pytest.mark.parametrize(
    "over",
    [
        dict(high="90", low="95", open="92", close="93"),
        dict(high="100", low="90", open="95", close="101"),
        dict(high="100", low="90", open="85", close="95"),
    ],
)
def test_impossible_ohlc(over):
    report = run([rec(**over)])
    assert report.accepted == []
    assert report.quarantined[0].rule == v.IMPOSSIBLE_OHLC


def test_negative_volume():
    report = run([rec(volume=-1)])
    assert report.quarantined[0].rule == v.NEGATIVE_VOLUME


def test_zero_volume_is_suspect_not_quarantined():
    report = run([rec(volume=0)])
    assert report.quarantined == []
    assert len(report.accepted) == 1
    assert [f.rule for f in report.flags] == [v.ZERO_VOLUME]
    assert report.status_of(report.accepted[0]) is QualityStatus.SUSPECT


def test_non_trading_day_weekend_quarantined():
    report = run([rec(d=date(2024, 1, 6))])
    assert report.accepted == []
    assert report.quarantined[0].rule == v.NON_TRADING_DAY


def test_future_dated_quarantined():
    report = run([rec(d=date(2026, 10, 27))])
    assert report.accepted == []
    assert report.quarantined[0].rule == v.FUTURE_DATED


def test_as_of_day_itself_is_allowed():
    assert len(run([rec(d=date(2026, 10, 23))], as_of=date(2026, 10, 23)).accepted) == 1


def test_malformed_record_quarantined_with_reason():
    report = run([rec(close="abc"), rec(d=date(2024, 1, 3), volume=None)])
    assert report.accepted == []
    assert [q.rule for q in report.quarantined] == [v.MALFORMED_RECORD] * 2


def test_suspicious_price_jump_flagged_with_prior_close():
    report = run(closes_series([100, 101, 300]))
    assert len(report.accepted) == 3
    flag = [f for f in report.flags if f.rule == v.SUSPICIOUS_PRICE_JUMP]
    assert len(flag) == 1
    assert "101" in flag[0].detail and "300" in flag[0].detail
    assert report.status_of(report.accepted[2]) is QualityStatus.SUSPECT
    assert report.status_of(report.accepted[1]) is QualityStatus.OK


def test_jump_threshold_configurable_and_not_triggered_below():
    series = closes_series([100, 140])
    assert not run(series).flags
    assert [f.rule for f in run(series, jump_threshold=0.2).flags] == [v.SUSPICIOUS_PRICE_JUMP]


def test_stale_price_after_n_identical_closes():
    flags = run(closes_series([100, 100, 100, 100, 100, 100])).flags
    assert {f.rule for f in flags} == {v.STALE_PRICE}
    assert [f.trade_date for f in flags][0] == business_days(date(2024, 1, 2), 5)[-1]
    assert not run(closes_series([100, 100, 100, 100, 101])).flags
    assert run(closes_series([100, 100, 100]), stale_run=3).flags


def test_missing_observation_counts_gaps_without_fabricating():
    days = business_days(date(2024, 1, 2), 8)
    records = [rec(d=d, close=str(100 + i)) for i, d in enumerate(days) if i not in (3, 4)]
    report = run(records)
    assert report.missing == {"AAA": 2}
    assert len(report.accepted) == 6


def test_no_missing_for_contiguous():
    assert run(closes_series([100, 101, 102])).missing == {}


def test_inconsistent_adjustment_flagged_without_corporate_action():
    days = business_days(date(2024, 1, 2), 3)
    adj = ["100", "100", "50"]
    records = [rec(d=d, close="100", adj_close=a) for d, a in zip(days, adj)]
    flags = run(records).flags
    assert [f.rule for f in flags] == [v.INCONSISTENT_ADJUSTMENT]
    assert flags[0].trade_date == days[2]


def test_adjustment_step_with_corporate_action_not_flagged():
    days = business_days(date(2024, 1, 2), 3)
    adj = ["100", "100", "50"]
    records = [rec(d=d, close="100", adj_close=a) for d, a in zip(days, adj)]
    assert not run(records, corporate_action_dates={("AAA", days[2])}).flags


def test_stable_adjustment_ratio_not_flagged_even_if_not_one():
    records = closes_series([100, 101, 102], adj_close="50")
    assert not run(records).flags


def test_none_adj_close_skips_adjustment_rule():
    assert not run(closes_series([100, 101], adj_close=None)).flags


def test_never_silently_repair_preserves_original_record():
    bad = rec(high="90", low="95", open="92", close="93")
    good = rec(d=date(2024, 1, 3))
    report = run([bad, good])
    assert len(report.accepted) == 1
    assert report.accepted[0].trade_date == date(2024, 1, 3)
    q = report.quarantined[0]
    assert q.record == bad
    assert q.record["high"] == "90" and q.record["low"] == "95"
    assert len(report.accepted) + len(report.quarantined) == 2


def test_multiple_hard_failures_all_recorded():
    report = run([rec(d=date(2024, 1, 6), volume=-1)])
    q = report.quarantined[0]
    assert set(q.rules) == {v.NON_TRADING_DAY, v.NEGATIVE_VOLUME}
    assert report.rules_fired()[v.NEGATIVE_VOLUME] == 1


def test_series_validation_duplicates_and_future():
    rows = [
        dict(series_id="X", observation_date=date(2024, 1, 1), value=Decimal("1")),
        dict(series_id="X", observation_date=date(2024, 1, 1), value=Decimal("2")),
        dict(series_id="X", observation_date=date(2027, 1, 1), value=Decimal("3")),
        dict(series_id="X", observation_date=date(2024, 2, 1), value=Decimal("4")),
    ]
    report = validate_series_records(rows, as_of=AS_OF)
    assert [r["value"] for r in report.accepted] == [Decimal("1"), Decimal("4")]
    assert [q.rule for q in report.quarantined] == [v.DUPLICATE_SYMBOL_DATE, v.FUTURE_DATED]
