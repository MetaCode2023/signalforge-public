from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from signalforge.core.errors import ProviderError
from signalforge.core.history import PriceHistory
from signalforge.data.archive import ingest_archive, load_archive_history, load_benchmark_series
from signalforge.data.ingestion import IngestResult, ingest
from signalforge.data.providers import FetchResult
from signalforge.data.storage import Warehouse
from signalforge.data import validation as v

D = Decimal
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
AS_OF = date(2026, 10, 26)


def clock():
    return NOW


@pytest.fixture
def wh():
    w = Warehouse(":memory:")
    yield w
    w.close()


@pytest.fixture
def mini_archive(tmp_path):
    def write(name, text):
        (tmp_path / name).write_text(text, encoding="utf-8")

    write("fred_market_indices.csv",
          "observation_date,SP500,NASDAQCOM,DJIA,VIXCLS\n"
          "2024-01-02,,100.5,.,12.5\n2024-01-03,4700.25,101,38000,\n2024-01-04,,102,38100,13\n")
    write("fred_rates_daily.csv", "observation_date,DGS10,T10Y2Y\n2024-01-02,3.95,-0.3\n")
    write("fred_macro_monthly.csv", "observation_date,UNRATE,FEDFUNDS\n2024-01-01,3.7,5.33\n")
    write("aapl_sample_2015_2017.csv",
          "symbol,date,open,high,low,close,volume,adjusted_close,provider,quality_label\n"
          "AAPL,2015-02-17,127.489998,128.880005,126.919998,127.830002,63152400,122.905254,p,q\n"
          "AAPL,2015-02-18,127.629997,128.779999,127.449997,128.720001,44891700,123.760965,p,q\n"
          "AAPL,2015-02-21,127.0,128.0,126.0,127.0,100,122.0,p,q\n")
    write("marketparquet_recent_stock_ohlcv.csv",
          "date,symbol,asset_type,open,high,low,close,volume,source,adjustments,quality_label\n"
          "2026-10-20,A,Stock,170.94,174.8,170.375,173.01,1978712,MarketParquet,Split-adjusted,x\n"
          "2026-10-20,AA,Stock,40.72,41.36,40.03,40.79,5743982,MarketParquet,Split-adjusted,x\n"
          "2026-10-21,A,Stock,173,175,172,174,1000,MarketParquet,Split-adjusted,x\n")
    return tmp_path


class StubProvider:
    name = "stub"
    kind = "bars"

    def __init__(self, records, kind="bars", actions=None, error=None):
        self.records, self.kind, self.actions, self.error = records, kind, actions or [], error

    def fetch(self, **kw):
        if self.error:
            raise self.error
        return [FetchResult("stub", "mem://x", NOW, self.kind, list(self.records), list(self.actions))]


def rec(d, close="10", symbol="AAA", **over):
    base = dict(symbol=symbol, trade_date=d, open=close, high=close, low=close, close=close,
                volume=100, adj_close=close, source="stub", source_reference="mem://x",
                retrieved_at=NOW, adjustment_method="none")
    base.update(over)
    return base


def counts(w):
    c = w.table_counts()
    c.pop("ingest_runs")
    return c


def test_ingest_writes_every_layer_and_records_run(wh):
    records = [rec(date(2024, 1, 2)), rec(date(2024, 1, 3)), rec(date(2024, 1, 6)),
               rec(date(2024, 1, 4), high="9", low="11", open="10", close="10")]
    result = ingest(StubProvider(records), wh, clock=clock, as_of=AS_OF)
    assert isinstance(result, IngestResult)
    assert (result.rows_in, result.rows_accepted, result.rows_quarantined) == (4, 2, 2)
    assert result.rules_fired[v.NON_TRADING_DAY] == 1 and result.rules_fired[v.IMPOSSIBLE_OHLC] == 1
    assert wh.count("raw_records") == 4 and wh.count("bars") == 2 and wh.count("quarantine") == 2
    run = wh.con.execute(
        "SELECT run_id, provider, rows_in, rows_accepted, rows_quarantined, status FROM ingest_runs"
    ).fetchall()
    assert run == [(result.run_id, "stub", 4, 2, 2, "ok")]


def test_quarantined_record_is_stored_unrepaired(wh):
    bad = rec(date(2024, 1, 4), high="9", low="11", open="10", close="10")
    ingest(StubProvider([bad]), wh, clock=clock, as_of=AS_OF)
    assert wh.count("bars") == 0
    (rule, detail, payload) = wh.con.execute("SELECT rule, detail, raw_payload FROM quarantine").fetchall()[0]
    assert rule == v.IMPOSSIBLE_OHLC and "high=9" in detail
    stored = json.loads(payload)
    assert stored["high"] == "9" and stored["low"] == "11"


def test_ingest_is_idempotent_on_synthetic_data(wh):
    records = [rec(date(2024, 1, 2)), rec(date(2024, 1, 6)), rec(date(2024, 1, 3), close="11")]
    p = StubProvider(records)
    ingest(p, wh, clock=clock, as_of=AS_OF)
    before = counts(wh)
    second = ingest(p, wh, clock=clock, as_of=AS_OF)
    assert counts(wh) == before
    assert wh.count("data_revisions") == 0
    assert wh.count("ingest_runs") == 2
    assert second.revised == 0 and second.inserted == 0


def test_changed_value_on_reingest_creates_revision_not_overwrite_silently(wh):
    ingest(StubProvider([rec(date(2024, 1, 2), close="10")]), wh, clock=clock, as_of=AS_OF)
    result = ingest(StubProvider([rec(date(2024, 1, 2), close="12")]), wh, clock=clock, as_of=AS_OF)
    assert result.revised == 1
    rows = wh.con.execute(
        "SELECT field, old_value, new_value FROM data_revisions WHERE field = 'close'"
    ).fetchall()
    assert rows == [("close", "10.000000", "12.000000")]
    assert wh.count("raw_records") == 2


def test_suspect_rows_are_stored_with_status(wh):
    records = [rec(date(2024, 1, 2), volume=0)]
    result = ingest(StubProvider(records), wh, clock=clock, as_of=AS_OF)
    assert result.rows_suspect == 1
    assert wh.con.execute("SELECT quality_status FROM bars").fetchall() == [("SUSPECT",)]


def test_corporate_actions_are_stored_and_suppress_adjustment_flag(wh):
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    records = [rec(days[0], adj_close="10"), rec(days[1], adj_close="10"), rec(days[2], adj_close="5")]
    action = dict(symbol="AAA", effective_date=days[2], action_type="split", ratio=D("2"),
                  cash_amount=None, source="stub")
    result = ingest(StubProvider(records, actions=[action]), wh, clock=clock, as_of=AS_OF)
    assert wh.count("corporate_actions") == 1
    assert v.INCONSISTENT_ADJUSTMENT not in result.rules_fired


def test_series_ingest_and_idempotent(wh):
    records = [dict(series_id="X", observation_date=date(2024, 1, d), value=D(str(d)), source="fred",
                    source_reference="mem://x", retrieved_at=NOW) for d in (2, 3)]
    records.append(dict(series_id="X", observation_date=date(2030, 1, 1), value=D("1"), source="fred",
                        source_reference="mem://x", retrieved_at=NOW))
    p = StubProvider(records, kind="series")
    r = ingest(p, wh, clock=clock, as_of=AS_OF)
    assert (r.rows_in, r.rows_accepted, r.rows_quarantined) == (3, 2, 1)
    before = counts(wh)
    ingest(p, wh, clock=clock, as_of=AS_OF)
    assert counts(wh) == before
    assert wh.count("series") == 2


def test_failed_fetch_is_recorded_and_reraised(wh):
    with pytest.raises(ProviderError):
        ingest(StubProvider([], error=ProviderError("boom")), wh, clock=clock, as_of=AS_OF)
    row = wh.con.execute("SELECT status, error, rows_in FROM ingest_runs").fetchall()
    assert row == [("failed", "boom", 0)]


def test_fetch_kwargs_are_forwarded(wh):
    seen = {}

    class P(StubProvider):
        def fetch(self, **kw):
            seen.update(kw)
            return super().fetch()

    ingest(P([]), wh, clock=clock, as_of=AS_OF, symbols=["AAA"])
    assert seen == {"symbols": ["AAA"]}


def test_ingest_archive_populates_and_is_idempotent(wh, mini_archive):
    results = ingest_archive(wh, mini_archive, clock=clock, as_of=AS_OF)
    assert results.rows_in > 0 and results.status == "ok"
    c = counts(wh)
    assert c["bars"] == 2 + 3 and c["quarantine"] == 1
    assert c["series"] > 0 and c["raw_records"] > 0
    assert wh.con.execute("SELECT rule FROM quarantine").fetchall() == [(v.NON_TRADING_DAY,)]
    ingest_archive(wh, mini_archive, clock=clock, as_of=AS_OF)
    assert counts(wh) == c
    assert wh.count("data_revisions") == 0


def test_blank_fred_observations_are_not_stored_as_zero(wh, mini_archive):
    ingest_archive(wh, mini_archive, clock=clock, as_of=AS_OF)
    assert len(wh.load_series("SP500")) == 1
    assert all(val != 0 for _, val in wh.load_series("DJIA"))
    assert wh.load_series("DJIA") == [(date(2024, 1, 3), D("38000")), (date(2024, 1, 4), D("38100"))]


def test_load_archive_history_returns_validated_price_history(mini_archive):
    history = load_archive_history(archive_dir=mini_archive, as_of=AS_OF)
    assert isinstance(history, PriceHistory)
    assert set(history.symbols) == {"AAPL", "A", "AA"}
    assert len(history.bars_for("AAPL")) == 2
    only = load_archive_history(["A"], archive_dir=mini_archive, as_of=AS_OF)
    assert only.symbols == ("A",)


def test_load_benchmark_series(mini_archive):
    got = load_benchmark_series("NASDAQCOM", archive_dir=mini_archive)
    assert got == [(date(2024, 1, 2), D("100.5")), (date(2024, 1, 3), D("101")), (date(2024, 1, 4), D("102"))]
    assert load_benchmark_series("DGS10", archive_dir=mini_archive) == [(date(2024, 1, 2), D("3.95"))]


def test_unknown_benchmark_series_raises(mini_archive):
    with pytest.raises(ProviderError):
        load_benchmark_series("NOPE", archive_dir=mini_archive)


def test_real_archive_ingest_populates_warehouse(archive_dir):
    if not os.path.isfile(os.path.join(archive_dir, "fred_market_indices.csv")):
        pytest.skip("local archive not present")
    w = Warehouse(":memory:")
    ingest_archive(w, archive_dir, as_of=AS_OF)
    series = {s: len(w.load_series(s)) for s in ("SP500", "NASDAQCOM", "DJIA", "VIXCLS")}
    assert series == {"SP500": 2511, "NASDAQCOM": 14048, "DJIA": 2512, "VIXCLS": 9302}
    aapl = w.con.execute("SELECT count(*) FROM bars WHERE source = 'plotly_aapl_sample'").fetchall()[0][0]
    aapl_q = w.con.execute("SELECT count(*) FROM quarantine WHERE source = 'plotly_aapl_sample'").fetchall()[0][0]
    assert aapl + aapl_q == 506
    mp = w.con.execute("SELECT count(*) FROM bars WHERE source = 'marketparquet'").fetchall()[0][0]
    mp_q = w.con.execute("SELECT count(*) FROM quarantine WHERE source = 'marketparquet'").fetchall()[0][0]
    assert mp + mp_q == 26019
    before = counts(w)
    ingest_archive(w, archive_dir, as_of=AS_OF)
    assert counts(w) == before
    w.close()


def test_suspect_flag_detail_is_persisted_for_human_review(wh):
    records = [rec(date(2024, 1, 2), close="10"), rec(date(2024, 1, 3), close="40")]
    ingest(StubProvider(records), wh, clock=clock, as_of=AS_OF)
    rows = wh.con.execute("SELECT symbol, trade_date, rule, detail FROM quality_flags").fetchall()
    assert len(rows) == 1 and rows[0][2] == v.SUSPICIOUS_PRICE_JUMP and "prior close 10" in rows[0][3]
    before = wh.count("quality_flags")
    ingest(StubProvider(records), wh, clock=clock, as_of=AS_OF)
    assert wh.count("quality_flags") == before
