"""mirror sync and verify against a FakeD1 (real SQL on in-memory sqlite3) and tmp_path warehouses."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from signalforge.mirror import codec, sync
from signalforge.mirror.codec import BARS, CORPORATE_ACTIONS, SERIES, SPECS
from signalforge.mirror.wrangler import CMD_UNSAFE, WranglerError
from tests.mirror_support import FakeD1, add_action, add_bar, make_warehouse


@pytest.fixture
def wh(tmp_path):
    w = make_warehouse(tmp_path / "w.duckdb")
    yield w
    w.close()


@pytest.fixture
def out(tmp_path):
    return tmp_path / "mirror"


def run(wh, d1, out, **kw):
    kw.setdefault("say", lambda *_: None)
    return sync.run_sync(wh, d1, out_dir=out, **kw)


def remote_counts(d1):
    return {s.name: d1.one(f"SELECT count(*) FROM {s.name}") for s in SPECS}


def local_counts(wh):
    return {s.name: wh.count(s.name) for s in SPECS}


def test_local_summary_groups_by_key_source_year(wh):
    summary = sync.local_summary(wh, BARS)
    assert set(summary) == {(s, "tiingo", y) for s in ("AAA", "BBB", "CCC") for y in ("2023", "2024")}
    stat = summary[("AAA", "tiingo", "2023")]
    assert stat.n == 6 and stat.lo == "2023-12-26" and stat.hi == "2023-12-31"
    assert len(stat.micro) == len(BARS.micro)
    assert sum(s.n for s in summary.values()) == wh.count("bars")


def test_remote_summary_sql_is_cmd_safe():
    for spec in SPECS:
        sql = sync.remote_summary_sql(spec)
        assert len(sql) < 7000 and not any(c in sql for c in CMD_UNSAFE)
        assert "GROUP BY" in sql


def test_sync_into_empty_remote_copies_everything(wh, out):
    d1 = FakeD1()
    result = run(wh, d1, out)
    assert result.complete and not result.mismatches
    assert remote_counts(d1) == local_counts(wh)
    for spec in SPECS:
        assert sync.remote_summary(d1, spec) == sync.local_summary(wh, spec)
    assert d1.one("SELECT count(*) FROM bars WHERE adj_close IS NULL") == 3


def test_second_sync_writes_nothing(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    files = len(d1.files)
    result = run(wh, d1, out)
    assert result.rows == 0 and result.complete
    assert len(d1.files) == files + 1  # only the last_sync stamp, no data
    assert d1.one("SELECT count(*) FROM bars") == 36


def test_revision_writes_only_changed_row(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    wh.con.execute("UPDATE bars SET close = 99 WHERE symbol='BBB' AND trade_date = DATE '2024-01-03'")
    before = len(d1.files)
    result = run(wh, d1, out)
    assert result.rows == 1 and len(d1.files) == before + 2  # one data file, one state file
    assert d1.one("SELECT close FROM bars WHERE symbol='BBB' AND trade_date='2024-01-03'") == "99.000000"
    assert remote_counts(d1) == local_counts(wh)


def test_row_with_past_retrieved_at_is_mirrored(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    add_bar(wh, "DDD", date(2024, 3, 1), retrieved_at=datetime(2001, 1, 1))
    result = run(wh, d1, out)
    assert result.rows == 1
    assert d1.one("SELECT count(*) FROM bars WHERE symbol='DDD'") == 1


def test_corporate_action_delete_propagates(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    wh.con.execute("DELETE FROM corporate_actions WHERE symbol='BBB' AND effective_date = DATE '2020-01-08'")
    result = run(wh, d1, out)
    assert result.deletes == 1
    assert remote_counts(d1) == local_counts(wh)


def test_mass_delete_over_cap_refused_remote_unchanged(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    wh.con.execute("DELETE FROM corporate_actions WHERE effective_date < DATE '2020-01-04'")
    before, files = remote_counts(d1), len(d1.files)
    with pytest.raises(sync.MirrorError, match="max-deletes"):
        run(wh, d1, out, max_deletes=2)
    assert remote_counts(d1) == before and len(d1.files) == files


def test_refuses_empty_local_table(tmp_path, out):
    from signalforge.data.storage import Warehouse

    w = Warehouse(tmp_path / "e.duckdb")
    add_bar(w, "AAA", date(2024, 1, 2))
    d1 = FakeD1()
    with pytest.raises(sync.MirrorError, match="series"):
        run(w, d1, out)
    assert d1.files == []
    w.close()


def test_refuses_local_count_below_half_remote(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    wh.con.execute("DELETE FROM bars WHERE symbol IN ('AAA', 'BBB')")
    with pytest.raises(sync.MirrorError, match="50%"):
        run(wh, d1, out, max_deletes=10_000)
    assert d1.one("SELECT count(*) FROM bars") == 36


def test_resume_after_failed_file(wh, out):
    d1 = FakeD1()
    d1.fail_on_file = 2
    with pytest.raises(WranglerError):
        run(wh, d1, out, rows_per_file=10)
    assert 0 < d1.one("SELECT count(*) FROM bars") < 36
    d1.fail_on_file = None
    result = run(wh, d1, out, rows_per_file=10)
    assert result.complete and not result.mismatches
    assert remote_counts(d1) == local_counts(wh)
    assert not list(out.glob("*.sql"))


def test_version_mismatch_refused(wh, out):
    d1 = FakeD1()
    d1.con.execute("UPDATE mirror_state SET value='sfm0' WHERE key='hash_version'")
    with pytest.raises(sync.MirrorError, match="hash_version"):
        run(wh, d1, out)
    assert d1.files == []


def test_uninitialised_remote_refused(wh, out):
    with pytest.raises(sync.MirrorError, match="init"):
        run(wh, FakeD1(init=False), out)


def test_postcondition_detects_dropped_writes(wh, out):
    d1 = FakeD1()
    d1.drop_writes = True
    result = run(wh, d1, out)
    assert result.mismatches and not result.ok


def test_files_removed_after_success(wh, out):
    d1 = FakeD1()
    run(wh, d1, out, rows_per_file=20)
    assert out.exists() and not list(out.glob("*.sql"))
    assert len(d1.files) >= 3


def test_stale_files_cleared_at_start(wh, out):
    out.mkdir()
    (out / "old.sql").write_text("DROP TABLE bars;")
    d1 = FakeD1()
    run(wh, d1, out)
    assert not (out / "old.sql").exists()
    assert d1.one("SELECT count(*) FROM bars") == 36


def test_max_files_stops_early_and_reports_incomplete(wh, out):
    d1 = FakeD1()
    lines = []
    result = run(wh, d1, out, rows_per_file=10, max_files=1, say=lines.append)
    assert not result.complete and result.files_done == 1 and result.ok
    assert len(d1.files) == 1
    assert any(l.startswith("file 1/") for l in lines)


def test_dry_run_executes_nothing_reports_rows_bytes_files(wh, out):
    d1 = FakeD1()
    result = run(wh, d1, out, dry_run=True, rows_per_file=20)
    assert d1.files == [] and remote_counts(d1) == {"bars": 0, "series": 0, "corporate_actions": 0}
    total = sum(local_counts(wh).values())
    assert result.rows == total and result.files_total == -(-total // 20) and result.bytes > 0
    assert not out.exists() or not list(out.glob("*.sql"))


# ---- verify ----

def synced(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    return d1


def test_verify_ok_reports_totals(wh, out):
    d1 = synced(wh, out)
    report = sync.run_verify(wh, d1, sample=5, seed=1, say=lambda *_: None)
    assert report.mismatches == [] and report.ok
    assert report.local_total == report.remote_total == sum(local_counts(wh).values())
    assert report.sampled > 0


def verify_msgs(wh, d1):
    return sync.run_verify(wh, d1, sample=0, seed=1, say=lambda *_: None).mismatches


def test_verify_detects_count_mismatch(wh, out):
    d1 = synced(wh, out)
    d1.con.execute("DELETE FROM bars WHERE symbol='AAA' AND trade_date='2024-01-03'")
    assert any("[count]" in m for m in verify_msgs(wh, d1))


def test_verify_detects_hash_mismatch(wh, out):
    d1 = synced(wh, out)
    d1.con.execute("UPDATE bars SET row_hash = row_hash + 1 WHERE symbol='AAA' AND trade_date='2024-01-03'")
    assert any("[hash]" in m for m in verify_msgs(wh, d1))


def test_verify_detects_minmax_mismatch(wh, out):
    d1 = synced(wh, out)
    d1.con.execute("UPDATE bars SET trade_date='2024-12-31' WHERE symbol='AAA' AND trade_date='2024-01-02'")
    msgs = verify_msgs(wh, d1)
    assert any("[minmax]" in m for m in msgs)
    assert not any("[count]" in m or "[hash]" in m for m in msgs)


def test_verify_detects_missing_group(wh, out):
    d1 = synced(wh, out)
    d1.con.execute("DELETE FROM series WHERE series_id='NEG'")
    assert any("[missing group]" in m for m in verify_msgs(wh, d1))


def test_verify_detects_extra_group(wh, out):
    d1 = synced(wh, out)
    row = codec.prepare_row(SERIES, ("ZZZ", date(2024, 1, 1), "fred", Decimal("1"), None))
    d1.con.executescript(next(codec.render_files([("u", SERIES, row)], 10))[0])
    assert any("[extra group]" in m for m in verify_msgs(wh, d1))


def test_verify_detects_value_tamper_with_intact_row_hash(wh, out):
    d1 = synced(wh, out)
    d1.con.execute("UPDATE bars SET close='999.000000' WHERE symbol='AAA' AND trade_date='2024-01-03'")
    msgs = verify_msgs(wh, d1)
    assert any("[value]" in m for m in msgs)
    assert not any("[hash]" in m for m in msgs)


def test_verify_detects_negative_series_tamper(wh, out):
    d1 = synced(wh, out)
    d1.con.execute("UPDATE series SET value='-0.000002' WHERE series_id='NEG' AND observation_date='2024-02-01'")
    assert any("[value]" in m for m in verify_msgs(wh, d1))


def test_sample_compares_full_rows_rehashed_from_remote(wh, out):
    d1 = synced(wh, out)
    d1.con.execute("UPDATE bars SET quality_status='bad' WHERE symbol='CCC' AND trade_date='2024-01-03'")
    assert verify_msgs(wh, d1) == []  # group checks cannot see this column
    report = sync.run_verify(wh, d1, sample=1000, seed=1, say=lambda *_: None)
    assert any("[sample]" in m for m in report.mismatches)


def test_sample_includes_latest_date_and_batches_queries(wh, out):
    d1 = synced(wh, out)
    before = len(d1.queries)
    sync.run_verify(wh, d1, sample=2, seed=3, say=lambda *_: None)
    assert any("2024-01-06" in q for q in d1.queries[before:])


def test_sample_queries_are_batched_under_the_budget(wh, out, monkeypatch):
    d1 = synced(wh, out)
    monkeypatch.setattr(sync, "QUERY_BUDGET", 500)
    before = len(d1.queries)
    report = sync.run_verify(wh, d1, sample=1000, seed=1, say=lambda *_: None)
    batches = [q for q in d1.queries[before:] if "IN (VALUES" in q]
    assert report.mismatches == [] and report.sampled == 51
    assert len(batches) >= 5 and all(len(q) <= 700 for q in batches)


# ---- review fixes ----

def test_whole_group_resent_when_sums_differ_but_row_hashes_match(wh, out):
    d1 = synced(wh, out)
    d1.con.execute("UPDATE bars SET close='999.000000' WHERE symbol='AAA' AND trade_date='2024-01-03'")
    result = run(wh, d1, out)
    assert result.rows == 6 and result.ok
    assert d1.one("SELECT close FROM bars WHERE symbol='AAA' AND trade_date='2024-01-03'") != "999.000000"
    assert sync.run_verify(wh, d1, sample=0, say=lambda *_: None).ok


def test_group_present_only_on_d1_is_removed(wh, out):
    d1 = synced(wh, out)
    wh.con.execute("DELETE FROM bars WHERE symbol='CCC'")
    result = run(wh, d1, out, max_deletes=1000)
    assert result.deletes == 12 and result.ok
    assert d1.one("SELECT count(*) FROM bars WHERE symbol='CCC'") == 0
    assert remote_counts(d1) == local_counts(wh)


def test_year_boundary_rows_are_diffed_exactly(wh, out):
    d1 = synced(wh, out)
    wh.con.execute("UPDATE bars SET close=77 WHERE symbol='BBB' AND trade_date IN "
                   "(DATE '2023-12-31', DATE '2024-01-01')")
    result = run(wh, d1, out)
    assert result.rows == 2 and result.ok


def test_micro_sum_above_2_pow_53_compares_equal(wh, out):
    add_bar(wh, "BIG", date(2024, 5, 1), close="99999999999.123457")
    d1 = FakeD1()
    result = run(wh, d1, out)
    assert result.ok, result.mismatches
    assert sync.run_verify(wh, d1, sample=0, say=lambda *_: None).ok
    stat = sync.local_summary(wh, BARS)[("BIG", "tiingo", "2024")]
    assert stat.micro[3] > 2**53


def many_symbol_warehouse(tmp_path, n=50):
    from signalforge.data.storage import Warehouse
    from tests.mirror_support import add_series

    w = Warehouse(tmp_path / "many.duckdb")
    for i in range(n):
        for d in (date(2024, 1, 2), date(2024, 1, 3)):
            add_bar(w, f"S{i:03d}", d)
    add_series(w, "X", date(2024, 1, 1), "1")
    add_action(w, "S000", date(2024, 1, 1))
    return w


def level2_queries(d1):
    return [q for q in d1.queries if q.startswith("SELECT symbol, trade_date, source, row_hash FROM bars")]


def test_level2_batches_many_groups_per_query(tmp_path, out):
    w = many_symbol_warehouse(tmp_path)
    d1 = FakeD1()
    run(w, d1, out)
    w.con.execute("UPDATE bars SET close = 55")
    d1.queries.clear()
    result = run(w, d1, out)
    queries = level2_queries(d1)
    assert result.rows == 100 and result.ok
    assert 1 <= len(queries) <= 4 and len(queries) < 50
    assert all(len(q) <= 7000 for q in queries)
    w.close()


def test_level2_respects_row_cap_per_query(tmp_path, out, monkeypatch):
    w = many_symbol_warehouse(tmp_path)
    d1 = FakeD1()
    run(w, d1, out)
    w.con.execute("UPDATE bars SET close = 55")
    monkeypatch.setattr(sync, "LEVEL2_ROW_CAP", 20)
    d1.queries.clear()
    result = run(w, d1, out)
    assert result.ok and len(level2_queries(d1)) == 5
    w.close()


def small_pages(wh, spec=BARS, rows=12):
    return sync.page_ranges(sync.local_summary(wh, spec), rows)


def test_page_ranges_cut_on_key_boundaries(wh):
    assert small_pages(wh) == [("AAA", "AAA"), ("BBB", "BBB"), ("CCC", "CCC")]
    assert sync.page_ranges(sync.local_summary(wh, BARS), 10_000) is None


def test_paged_remote_summary_equals_unpaged_and_counts_queries(wh, out):
    d1 = synced(wh, out)
    pages = small_pages(wh)
    d1.queries.clear()
    assert sync.remote_summary(d1, BARS, pages) == sync.remote_summary(d1, BARS)
    assert len(d1.queries) == len(pages) + 1 + 1  # pages + complement, then the one unpaged query
    assert all(len(q) < 7000 for q in d1.queries)


def test_paged_summary_sees_remote_only_keys_before_first_and_after_last(wh, out):
    d1 = synced(wh, out)
    for sym in ("000", "ZZZ", "BBBB"):
        row = codec.prepare_row(BARS, (sym, date(2024, 1, 1), "tiingo", *[Decimal("1")] * 4, None, 5, True,
                                       None, None, None, None))
        d1.con.executescript(next(codec.render_files([("u", BARS, row)], 10))[0])
    remote = sync.remote_summary(d1, BARS, small_pages(wh))
    extra = set(remote) - set(sync.local_summary(wh, BARS))
    assert extra == {("000", "tiingo", "2024"), ("ZZZ", "tiingo", "2024"), ("BBBB", "tiingo", "2024")}


def test_sync_with_paging_removes_remote_only_keys(wh, out):
    d1 = synced(wh, out)
    row = codec.prepare_row(BARS, ("ZZZ", date(2024, 1, 1), "tiingo", *[Decimal("1")] * 4, None, 5, True,
                                   None, None, None, None))
    d1.con.executescript(next(codec.render_files([("u", BARS, row)], 10))[0])
    result = run(wh, d1, out, summary_page_rows=12)
    assert result.deletes == 1 and result.ok
    assert d1.one("SELECT count(*) FROM bars WHERE symbol='ZZZ'") == 0


def test_failed_import_still_deletes_its_sql_file(wh, out):
    d1 = FakeD1()
    d1.fail_on_file = 1
    with pytest.raises(WranglerError):
        run(wh, d1, out)
    assert not list(out.glob("*.sql"))


def test_noop_sync_stamps_last_sync_at(wh, out):
    from datetime import timezone

    d1 = synced(wh, out)
    run(wh, d1, out, now=datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert d1.one("SELECT value FROM mirror_state WHERE key='last_sync_at'") == "2030-01-02T03:04:05+00:00"
    assert d1.one("SELECT value FROM mirror_state WHERE key='last_sync_upserts'") == "0"


def test_a_remote_row_with_a_non_canonical_date_is_repaired(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    d1.con.execute(
        "INSERT INTO bars (symbol, trade_date, source, open, high, low, close, adj_close, volume, "
        "volume_known, retrieved_at, adjustment_method, quality_status, source_reference, row_hash) "
        "VALUES ('AAA', '2024-12-31 00:00:00', 'tiingo', '1.000000', '1.000000', '1.000000', "
        "'1.000000', NULL, 1, 1, NULL, NULL, NULL, NULL, 7)"
    )
    result = run(wh, d1, out, max_deletes=10)
    assert result.ok
    assert d1.one("SELECT count(*) FROM bars WHERE trade_date = '2024-12-31 00:00:00'") == 0
    assert remote_counts(d1) == local_counts(wh)


def test_max_deletes_is_counted_over_the_whole_plan(wh, out):
    d1 = FakeD1()
    run(wh, d1, out)
    wh.con.execute("DELETE FROM bars WHERE symbol = 'AAA' AND trade_date <= DATE '2023-12-27'")
    wh.con.execute("DELETE FROM corporate_actions WHERE effective_date <= DATE '2020-01-02'")
    before = remote_counts(d1)
    with pytest.raises(sync.MirrorError, match="max-deletes"):
        run(wh, d1, out, max_deletes=3)
    assert remote_counts(d1) == before
