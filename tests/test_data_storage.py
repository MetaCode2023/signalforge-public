from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import duckdb
import pytest

from signalforge.core.errors import DataQualityError
from signalforge.core.types import Bar, QualityStatus
from signalforge.data.storage import Warehouse
from tests.conftest import business_days, make_bar

D = Decimal
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def wh():
    w = Warehouse(":memory:")
    yield w
    w.close()


def bars_for(symbol, closes, source="s"):
    days = business_days(date(2024, 1, 2), len(closes))
    return [make_bar(symbol, d, D(str(c)), source=source) for d, c in zip(days, closes)]


def test_schema_creation_is_idempotent(tmp_path):
    path = tmp_path / "w.duckdb"
    Warehouse(path).close()
    w = Warehouse(path)
    names = {r[0] for r in w.con.execute("SHOW TABLES").fetchall()}
    w.close()
    assert {
        "raw_records", "bars", "quarantine", "series", "corporate_actions",
        "ingest_runs", "data_revisions",
    } <= names


def test_bars_unique_key_is_enforced_by_the_database(wh):
    wh.upsert_bars([make_bar("AAA", date(2024, 1, 2), D("10"), source="s")])
    with pytest.raises(duckdb.ConstraintException):
        wh.con.execute(
            "INSERT INTO bars (symbol, trade_date, open, high, low, close, volume, source) "
            "VALUES ('AAA', DATE '2024-01-02', 1, 1, 1, 1, 1, 's')"
        )


def test_series_unique_key_is_enforced_by_the_database(wh):
    wh.upsert_series([dict(series_id="X", observation_date=date(2024, 1, 1), value=D("1"),
                           source="fred", retrieved_at=T0)])
    with pytest.raises(duckdb.ConstraintException):
        wh.con.execute(
            "INSERT INTO series (series_id, observation_date, value, source) "
            "VALUES ('X', DATE '2024-01-01', 2, 'fred')"
        )


def test_same_symbol_date_different_source_is_allowed(wh):
    day = date(2024, 1, 2)
    wh.upsert_bars([make_bar("AAA", day, D("10"), source="a"), make_bar("AAA", day, D("10"), source="b")])
    assert len(wh.load_bars(["AAA"])) == 2


def test_upsert_and_load_roundtrip_exact_decimal(wh):
    bar = Bar("AAA", date(2024, 1, 2), D("10.123456"), D("11.5"), D("9.25"), D("10.1"),
              1234, adj_close=D("9.987654"), source="s")
    wh.upsert_bars([bar])
    (got,) = wh.load_bars(["AAA"])
    assert got.open == D("10.123456") and isinstance(got.open, Decimal)
    assert got.adj_close == D("9.987654") and got.volume == 1234
    assert got == Bar("AAA", date(2024, 1, 2), D("10.123456"), D("11.500000"), D("9.250000"),
                      D("10.100000"), 1234, adj_close=D("9.987654"), source="s")


def test_adj_close_none_roundtrips_as_none(wh):
    bar = Bar("AAA", date(2024, 1, 2), D("1"), D("1"), D("1"), D("1"), 5, adj_close=None, source="s")
    wh.upsert_bars([bar])
    assert wh.load_bars()[0].adj_close is None


def test_meta_is_stored(wh):
    bar = make_bar("AAA", date(2024, 1, 2), D("10"), source="s")
    meta = {("AAA", date(2024, 1, 2), "s"): dict(
        retrieved_at=T0, source_reference="file.csv", adjustment_method="split_adjusted",
        quality_status=QualityStatus.SUSPECT)}
    wh.upsert_bars([bar], meta=meta)
    row = wh.con.execute(
        "SELECT adjustment_method, quality_status, source_reference, retrieved_at FROM bars"
    ).fetchone()
    assert row[0] == "split_adjusted" and row[1] == "SUSPECT" and row[2] == "file.csv"
    assert row[3] == datetime(2026, 10, 26, 12, 0)


def test_reupsert_identical_is_a_noop_and_creates_no_revision(wh):
    bars = bars_for("AAA", [10, 11, 12])
    first = wh.upsert_bars(bars, retrieved_at=T0)
    second = wh.upsert_bars(bars, retrieved_at=T1)
    assert (first.inserted, first.revised, first.unchanged) == (3, 0, 0)
    assert (second.inserted, second.revised, second.unchanged) == (0, 0, 3)
    assert wh.count("bars") == 3 and wh.count("data_revisions") == 0
    assert wh.con.execute("SELECT max(retrieved_at) FROM bars").fetchone()[0] == datetime(2026, 10, 26, 12, 0)


def test_changed_value_records_revision_with_old_and_new(wh):
    wh.upsert_bars(bars_for("AAA", [10]), retrieved_at=T0)
    result = wh.upsert_bars(bars_for("AAA", [11]), retrieved_at=T1, run_id="r2")
    assert result.revised == 1
    rows = wh.con.execute(
        "SELECT entity, symbol, trade_date, source, field, old_value, new_value, ingest_run_id "
        "FROM data_revisions ORDER BY field"
    ).fetchall()
    assert {r[4] for r in rows} == {"open", "high", "low", "close", "adj_close"}
    close = [r for r in rows if r[4] == "close"][0]
    assert close[0] == "bar" and close[1] == "AAA" and close[5] == "10.000000"
    assert close[6] == "11.000000" and close[7] == "r2"
    assert wh.load_bars()[0].close == D("11")


def test_only_changed_fields_are_logged(wh):
    base = make_bar("AAA", date(2024, 1, 2), D("10"), volume=100)
    wh.upsert_bars([base])
    wh.upsert_bars([make_bar("AAA", date(2024, 1, 2), D("10"), volume=200)])
    fields = [r[0] for r in wh.con.execute("SELECT field FROM data_revisions").fetchall()]
    assert fields == ["volume"]


def test_duplicate_keys_in_one_batch_rejected(wh):
    b = make_bar("AAA", date(2024, 1, 2), D("10"))
    with pytest.raises(DataQualityError):
        wh.upsert_bars([b, b])


def test_load_bars_filters(wh):
    wh.upsert_bars(bars_for("AAA", [1, 2, 3, 4]) + bars_for("BBB", [5, 6]))
    days = business_days(date(2024, 1, 2), 4)
    assert {b.symbol for b in wh.load_bars(["BBB"])} == {"BBB"}
    got = wh.load_bars(["AAA"], start=days[1], end=days[2])
    assert [b.trade_date for b in got] == days[1:3]
    assert len(wh.load_bars()) == 6
    assert wh.load_bars(["AAA"], source="other") == []


def test_quarantine_insert_and_idempotent(wh):
    row = dict(symbol="AAA", trade_date=date(2024, 1, 6), source="s", rule="non_trading_day",
               detail="weekend", raw_payload='{"a": 1}', quarantined_at=T0)
    assert wh.insert_quarantine([row]) == 1
    assert wh.insert_quarantine([row]) == 0
    assert wh.insert_quarantine([{**row, "trade_date": None, "rule": "malformed_record"}]) == 1
    assert wh.insert_quarantine([{**row, "trade_date": None, "rule": "malformed_record"}]) == 0
    assert wh.count("quarantine") == 2


def test_raw_records_immutable_and_deduped_by_content(wh):
    row = dict(source="s", source_reference="f.csv", payload='{"x": 1}', retrieved_at=T0,
               content_sha256="abc", ingest_run_id="r1")
    assert wh.insert_raw_records([row]) == 1
    assert wh.insert_raw_records([{**row, "ingest_run_id": "r2"}]) == 0
    assert wh.insert_raw_records([{**row, "content_sha256": "def"}]) == 1
    assert wh.count("raw_records") == 2


def test_raw_records_deduped_across_changing_request_windows(wh):
    row = dict(source="s", source_reference="https://example.test/data?start=2024-01-01",
               payload='{"x": 1}', retrieved_at=T0, content_sha256="same", ingest_run_id="first")
    assert wh.insert_raw_records([row]) == 1
    assert wh.insert_raw_records([{**row, "source_reference": "https://example.test/data?start=2024-01-02",
                                   "ingest_run_id": "second"}]) == 0
    assert wh.con.execute("SELECT source_reference, ingest_run_id FROM raw_records").fetchall() == [
        (row["source_reference"], "first")
    ]
    assert wh.insert_raw_records([{**row, "source": "another-provider"}]) == 1
    assert wh.insert_raw_records([{**row, "content_sha256": "changed", "payload": '{"x": 2}'}]) == 1
    assert wh.count("raw_records") == 3


def test_series_upsert_load_and_revision(wh):
    rows = [dict(series_id="X", observation_date=date(2024, 1, d), value=D(str(d)),
                 source="fred", retrieved_at=T0) for d in (2, 3)]
    first = wh.upsert_series(rows)
    again = wh.upsert_series(rows)
    assert (first.inserted, again.unchanged, wh.count("series")) == (2, 2, 2)
    assert wh.load_series("X") == [(date(2024, 1, 2), D("2")), (date(2024, 1, 3), D("3"))]
    changed = wh.upsert_series([{**rows[0], "value": D("2.5")}], run_id="r9")
    assert changed.revised == 1
    rev = wh.con.execute("SELECT entity, symbol, field, old_value, new_value FROM data_revisions").fetchall()
    assert rev == [("series", "X", "value", "2.000000", "2.500000")]


def test_corporate_actions_idempotent(wh):
    row = dict(symbol="AAA", effective_date=date(2024, 1, 2), action_type="split",
               ratio=D("2"), cash_amount=None, source="s")
    assert wh.upsert_corporate_actions([row]) == 1
    assert wh.upsert_corporate_actions([row]) == 0
    assert wh.corporate_action_dates() == {("AAA", date(2024, 1, 2))}


def test_record_run(wh):
    wh.record_run(run_id="r1", provider="p", started_at=T0, finished_at=T1, rows_in=3,
                  rows_accepted=2, rows_quarantined=1, status="ok", error=None)
    row = wh.con.execute("SELECT run_id, provider, rows_in, rows_accepted, rows_quarantined, status, error FROM ingest_runs").fetchone()
    assert row == ("r1", "p", 3, 2, 1, "ok", None)


def test_coverage_report_bars(wh):
    days = business_days(date(2024, 1, 2), 6)
    kept = [d for i, d in enumerate(days) if i not in (2, 3)]
    wh.upsert_bars([make_bar("AAA", d, D("10")) for d in kept], retrieved_at=T0)
    wh.upsert_bars(bars_for("BBB", [1, 2, 3]), retrieved_at=T1)
    report = {r["symbol"]: r for r in wh.coverage_report()}
    a = report["AAA"]
    assert (a["kind"], a["rows"], a["min_date"], a["max_date"]) == ("bars", 4, days[0], days[-1])
    assert a["missing_business_days"] == 2
    assert a["last_retrieved_at"] == datetime(2026, 10, 26, 12, 0)
    assert report["BBB"]["missing_business_days"] == 0


def test_coverage_report_series_daily_vs_monthly(wh):
    daily = [dict(series_id="DAILY", observation_date=d, value=D("1"), source="fred", retrieved_at=T0)
             for d in business_days(date(2024, 1, 2), 5)[:2] + business_days(date(2024, 1, 2), 5)[3:]]
    monthly = [dict(series_id="MONTHLY", observation_date=date(2024, m, 1), value=D("1"),
                    source="fred", retrieved_at=T0) for m in (1, 2, 3)]
    wh.upsert_series(daily + monthly)
    report = {r["symbol"]: r for r in wh.coverage_report()}
    assert report["DAILY"]["kind"] == "series" and report["DAILY"]["missing_business_days"] == 1
    assert report["MONTHLY"]["missing_business_days"] is None


def test_fingerprint_is_deterministic_and_order_independent():
    bars = bars_for("AAA", [10, 11, 12]) + bars_for("BBB", [5, 6, 7])
    a, b = Warehouse(":memory:"), Warehouse(":memory:")
    a.upsert_bars(bars)
    b.upsert_bars(list(reversed(bars)), retrieved_at=T1)
    args = (["AAA", "BBB"], date(2024, 1, 1), date(2024, 12, 31))
    assert a.fingerprint(*args) == b.fingerprint(*args)
    assert a.fingerprint(*args) == a.fingerprint(*args)
    assert len(a.fingerprint(*args)) == 64


def test_fingerprint_changes_when_any_consumed_value_changes(wh):
    wh.upsert_bars(bars_for("AAA", [10, 11, 12]))
    args = (["AAA"], date(2024, 1, 1), date(2024, 12, 31))
    before = wh.fingerprint(*args)
    wh.upsert_bars(bars_for("AAA", [10, 11, 13]))
    assert wh.fingerprint(*args) != before


def test_fingerprint_only_covers_requested_window(wh):
    wh.upsert_bars(bars_for("AAA", [10, 11, 12]))
    days = business_days(date(2024, 1, 2), 3)
    before = wh.fingerprint(["AAA"], days[0], days[1])
    wh.upsert_bars([make_bar("AAA", days[2], D("99"))])
    assert wh.fingerprint(["AAA"], days[0], days[1]) == before


def test_file_warehouse_persists(tmp_path):
    path = tmp_path / "w.duckdb"
    w = Warehouse(path)
    w.upsert_bars(bars_for("AAA", [1, 2]))
    w.close()
    w2 = Warehouse(path)
    assert len(w2.load_bars()) == 2
    w2.close()


def test_coverage_report_is_per_symbol_and_source_never_merging_sources(wh):
    wh.upsert_bars([make_bar("AAA", date(2015, 2, 17), D("10"), source="old")])
    wh.upsert_bars([make_bar("AAA", date(2026, 10, 20), D("11"), source="new")])
    report = wh.coverage_report()
    assert sorted((r["symbol"], r["source"], r["rows"], r["missing_business_days"]) for r in report) == [
        ("AAA", "new", 1, 0), ("AAA", "old", 1, 0)]


def test_series_coverage_carries_source(wh):
    wh.upsert_series([dict(series_id="X", observation_date=date(2024, 1, 2), value=D("1"),
                           source="fred", retrieved_at=T0)])
    assert wh.coverage_report()[0]["source"] == "fred"


def test_quality_flags_stored_idempotently_and_not_counted_as_quarantine(wh):
    flag = dict(symbol="AAA", trade_date=date(2024, 1, 2), source="s", rule="suspicious_price_jump",
                detail="close 10 -> 30; prior close 10", flagged_at=T0)
    assert wh.insert_flags([flag]) == 1
    assert wh.insert_flags([flag]) == 0
    assert wh.insert_flags([{**flag, "rule": "stale_price"}]) == 1
    assert wh.count("quality_flags") == 2 and wh.count("quarantine") == 0
