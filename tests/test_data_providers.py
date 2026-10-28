from __future__ import annotations

import os
import socket
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from signalforge.config.models import load_data_sources
from signalforge.core.errors import ProviderError
from signalforge.data.providers import (
    AlphaVantageProvider,
    DataProvider,
    FredSeriesProvider,
    LocalArchiveProvider,
    TiingoProvider,
    parse_fred_csv,
)

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures"
CFG = load_data_sources()
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def clock():
    return NOW


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "headers": headers or {}, "kw": kw})
        return self.responses.pop(0)


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- FRED


def test_parse_fred_csv_skips_dot_and_empty_without_zero_fill():
    recs = parse_fred_csv(fixture_text("fred_sample.csv"))
    assert [(r["series_id"], r["observation_date"], r["value"]) for r in recs] == [
        ("DGS10", date(2024, 1, 2), D("3.95")),
        ("DGS2", date(2024, 1, 2), D("4.33")),
        ("DGS2", date(2024, 1, 3), D("4.28")),
        ("DGS10", date(2024, 1, 4), D("4.02")),
        ("DGS2", date(2024, 1, 4), D("4.36")),
    ]
    assert all(r["value"] != 0 for r in recs)


def test_parse_fred_csv_accepts_legacy_date_header():
    recs = parse_fred_csv("DATE,X\n2024-01-02,1.5\n")
    assert recs[0]["observation_date"] == date(2024, 1, 2)


@pytest.mark.parametrize("text", ["<html>blocked</html>", "", "a,b\n1,2\n"])
def test_parse_fred_csv_rejects_non_fred_payloads(text):
    with pytest.raises(ProviderError):
        parse_fred_csv(text)


def test_parse_fred_csv_rejects_garbage_value():
    with pytest.raises(ProviderError):
        parse_fred_csv("observation_date,X\n2024-01-02,abc\n")


def test_fred_fetch_builds_request_and_stamps_provenance():
    session = FakeSession(FakeResponse(fixture_text("fred_sample.csv")))
    p = FredSeriesProvider(CFG.providers["fred"], user_agent=CFG.user_agent, session=session,
                           sleep=lambda s: None, clock=clock)
    (result,) = p.fetch(["DGS10", "DGS2"], start=date(2024, 1, 1), end=date(2024, 1, 31))
    call = session.calls[0]
    assert call["url"].startswith("https://fred.stlouisfed.org/graph/fredgraph.csv?")
    assert "id=DGS10,DGS2" in call["url"] and "cosd=2024-01-01" in call["url"]
    assert "coed=2024-01-31" in call["url"]
    assert call["headers"]["User-Agent"] == CFG.user_agent
    assert result.kind == "series" and result.source == "fred"
    assert result.source_reference == call["url"]
    assert result.retrieved_at == NOW
    assert len(result.records) == 5
    assert all(r["source"] == "fred" and r["source_reference"] == call["url"]
               and r["retrieved_at"] == NOW for r in result.records)


def test_fred_http_error_is_provider_error():
    p = FredSeriesProvider(CFG.providers["fred"], session=FakeSession(FakeResponse("no", 503)),
                           sleep=lambda s: None, clock=clock)
    with pytest.raises(ProviderError, match="503"):
        p.fetch(["DGS10"])


def test_fred_rate_limit_sleeps_between_requests_only():
    sleeps = []
    session = FakeSession(*[FakeResponse(fixture_text("fred_sample.csv")) for _ in range(3)])
    p = FredSeriesProvider(CFG.providers["fred"], session=session, sleep=sleeps.append, clock=clock)
    p.fetch(["DGS10"])
    assert sleeps == []
    p.fetch(["DGS10"])
    p.fetch(["DGS10"])
    assert sleeps == [3.0, 3.0]


@pytest.mark.network
def test_fred_live_fetch():
    p = FredSeriesProvider(CFG.providers["fred"], user_agent=CFG.user_agent)
    (result,) = p.fetch(["DGS10"], start=date(2024, 1, 1), end=date(2024, 1, 31))
    assert result.records and result.records[0]["series_id"] == "DGS10"


# ---------------------------------------------------------------- local archive


def write(path, text):
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def mini_archive(tmp_path):
    write(tmp_path / "fred_market_indices.csv",
          "observation_date,SP500,NASDAQCOM,DJIA,VIXCLS\n"
          "2024-01-02,,100.5,.,12.5\n2024-01-03,4700.25,101,38000,\n")
    write(tmp_path / "fred_rates_daily.csv", "observation_date,DGS10,T10Y2Y\n2024-01-02,3.95,-0.3\n")
    write(tmp_path / "fred_macro_monthly.csv", "observation_date,UNRATE,FEDFUNDS\n2024-01-01,3.7,5.33\n")
    write(tmp_path / "aapl_sample_2015_2017.csv",
          "symbol,date,open,high,low,close,volume,adjusted_close,provider,quality_label\n"
          "AAPL,2015-02-17,127.489998,128.880005,126.919998,127.830002,63152400,122.905254,"
          "Plotly historical sample,OLD SAMPLE\n")
    write(tmp_path / "marketparquet_recent_stock_ohlcv.csv",
          "date,symbol,asset_type,open,high,low,close,volume,source,adjustments,quality_label\n"
          "2026-10-20,A,Stock,170.94,174.8,170.375,173.01,1978712,MarketParquet,Split-adjusted,x\n"
          "2026-10-20,AA,Stock,40.72,41.36,40.03,40.79,5743982,MarketParquet,Split-adjusted,x\n")
    return tmp_path


def archive(mini_archive):
    return LocalArchiveProvider(mini_archive)


def by_name(results):
    return {r.source_reference.rsplit(os.sep, 1)[-1].rsplit("/", 1)[-1]: r for r in results}


def test_archive_maps_fred_wide_files_to_series(mini_archive):
    results = by_name(archive(mini_archive).fetch())
    idx = results["fred_market_indices.csv"]
    assert idx.kind == "series" and idx.source == "fred"
    got = {(r["series_id"], r["observation_date"]): r["value"] for r in idx.records}
    assert got == {
        ("NASDAQCOM", date(2024, 1, 2)): D("100.5"), ("VIXCLS", date(2024, 1, 2)): D("12.5"),
        ("SP500", date(2024, 1, 3)): D("4700.25"), ("NASDAQCOM", date(2024, 1, 3)): D("101"),
        ("DJIA", date(2024, 1, 3)): D("38000"),
    }
    assert {r["series_id"] for r in results["fred_rates_daily.csv"].records} == {"DGS10", "T10Y2Y"}
    assert {r["series_id"] for r in results["fred_macro_monthly.csv"].records} == {"UNRATE", "FEDFUNDS"}


def test_archive_maps_aapl_shape(mini_archive):
    r = by_name(archive(mini_archive).fetch())["aapl_sample_2015_2017.csv"]
    assert r.kind == "bars"
    (rec,) = r.records
    assert rec["symbol"] == "AAPL" and rec["trade_date"] == date(2015, 2, 17)
    assert rec["open"] == D("127.489998") and rec["close"] == D("127.830002")
    assert rec["adj_close"] == D("122.905254") and rec["volume"] == 63152400
    assert rec["source"] == r.source and rec["adjustment_method"] == "provider_adjusted_close"


def test_archive_maps_marketparquet_shape(mini_archive):
    r = by_name(archive(mini_archive).fetch())["marketparquet_recent_stock_ohlcv.csv"]
    assert r.source == "marketparquet"
    a = r.records[0]
    assert (a["symbol"], a["trade_date"], a["close"], a["volume"]) == ("A", date(2026, 10, 20), D("173.01"), 1978712)
    assert a["adj_close"] is None
    assert a["adjustment_method"] == "split_adjusted_no_dividends"


def test_archive_symbol_and_dataset_filters(mini_archive):
    p = archive(mini_archive)
    (r,) = p.fetch(datasets=["marketparquet_recent_stock_ohlcv"], symbols=["AA"])
    assert [x["symbol"] for x in r.records] == ["AA"]


def test_archive_stamps_provenance_with_file_path(mini_archive):
    for r in archive(mini_archive).fetch():
        assert Path(r.source_reference).is_file()
        assert r.retrieved_at.tzinfo is not None
        assert all(x["source_reference"] == r.source_reference and x["retrieved_at"] == r.retrieved_at
                   for x in r.records)


def test_archive_never_touches_the_network(mini_archive, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network touched")

    monkeypatch.setattr(socket.socket, "connect", boom)
    assert archive(mini_archive).fetch()


def test_archive_missing_file_is_provider_error(tmp_path):
    with pytest.raises(ProviderError):
        LocalArchiveProvider(tmp_path).fetch(datasets=["aapl_sample_2015_2017"])


def test_archive_unknown_dataset(mini_archive):
    with pytest.raises(ProviderError):
        archive(mini_archive).fetch(datasets=["nope"])


def test_archive_real_files_have_expected_shape(archive_dir):
    if not os.path.isfile(os.path.join(archive_dir, "aapl_sample_2015_2017.csv")):
        pytest.skip("local archive not present")
    results = {r.source_reference.replace("\\", "/").rsplit("/", 1)[-1]: r
               for r in LocalArchiveProvider(archive_dir).fetch()}
    assert len(results["aapl_sample_2015_2017.csv"].records) == 506
    assert len(results["marketparquet_recent_stock_ohlcv.csv"].records) == 26019


# ---------------------------------------------------------------- keyed providers


def test_all_providers_satisfy_protocol(mini_archive):
    ps = [
        FredSeriesProvider(CFG.providers["fred"]), LocalArchiveProvider(mini_archive),
        TiingoProvider(CFG.providers["tiingo"], env={}), AlphaVantageProvider(CFG.providers["alphavantage"], env={}),
    ]
    assert all(isinstance(p, DataProvider) for p in ps)


@pytest.mark.parametrize("cls,name,env_var", [
    (TiingoProvider, "tiingo", "TIINGO_API_KEY"),
    (AlphaVantageProvider, "alphavantage", "ALPHAVANTAGE_API_KEY"),
])
def test_missing_key_raises_clear_provider_error(cls, name, env_var):
    session = FakeSession()
    p = cls(CFG.providers[name], env={}, session=session, sleep=lambda s: None, clock=clock)
    with pytest.raises(ProviderError, match=f"no API key in {env_var}"):
        p.fetch(["AAPL"])
    assert session.calls == []


def test_tiingo_parses_fixture_and_hides_key():
    key = "SECRETKEY123"
    session = FakeSession(FakeResponse(fixture_text("tiingo_aapl_prices.json")))
    p = TiingoProvider(CFG.providers["tiingo"], user_agent="ua", env={"TIINGO_API_KEY": key},
                       session=session, sleep=lambda s: None, clock=clock)
    (r,) = p.fetch(["AAPL"], start=date(2020, 8, 1), end=date(2020, 9, 30))
    call = session.calls[0]
    assert call["url"].startswith("https://api.tiingo.com/tiingo/daily/AAPL/prices?")
    assert "startDate=2020-08-01" in call["url"]
    assert key not in call["url"] and key not in r.source_reference
    assert call["headers"]["Authorization"] == f"Token {key}"
    assert r.kind == "bars" and r.source == "tiingo" and r.source_reference == call["url"]
    first = r.records[0]
    assert first["symbol"] == "AAPL" and first["trade_date"] == date(2020, 8, 28)
    assert first["close"] == D("499.23") and first["adj_close"] == D("124.8075")
    assert first["volume"] == 46907479 and first["retrieved_at"] == NOW
    assert first["adjustment_method"] == "split_dividend_adjusted"
    assert key not in repr(r)
    actions = {(a["effective_date"], a["action_type"]): a for a in r.corporate_actions}
    assert actions[(date(2020, 8, 31), "split")]["ratio"] == D("4.0")
    assert actions[(date(2020, 9, 1), "dividend")]["cash_amount"] == D("0.205")
    assert len(actions) == 2


def test_tiingo_http_error_does_not_leak_key():
    key = "SECRETKEY123"
    p = TiingoProvider(CFG.providers["tiingo"], env={"TIINGO_API_KEY": key},
                       session=FakeSession(FakeResponse("denied", 401)), sleep=lambda s: None)
    with pytest.raises(ProviderError) as exc:
        p.fetch(["AAPL"])
    assert key not in str(exc.value) and "401" in str(exc.value)


def test_tiingo_rate_limit_between_symbols():
    sleeps = []
    session = FakeSession(*[FakeResponse(fixture_text("tiingo_aapl_prices.json")) for _ in range(2)])
    p = TiingoProvider(CFG.providers["tiingo"], env={"TIINGO_API_KEY": "k"}, session=session,
                       sleep=sleeps.append, clock=clock)
    assert len(p.fetch(["AAPL", "MSFT"])) == 2
    assert sleeps == [2.0]


def test_alphavantage_parses_adjusted_fixture_ascending():
    key = "AVSECRET"
    session = FakeSession(FakeResponse(fixture_text("alphavantage_daily_adjusted_msft.json")))
    p = AlphaVantageProvider(CFG.providers["alphavantage"], env={"ALPHAVANTAGE_API_KEY": key},
                             session=session, sleep=lambda s: None, clock=clock)
    (r,) = p.fetch(["MSFT"], start=date(2024, 6, 7))
    call = session.calls[0]
    assert "function=TIME_SERIES_DAILY_ADJUSTED" in call["url"] and "symbol=MSFT" in call["url"]
    assert f"apikey={key}" in call["url"]
    assert key not in r.source_reference and "apikey" not in r.source_reference
    assert [x["trade_date"] for x in r.records] == [date(2024, 6, 11), date(2024, 6, 12)]
    last = r.records[-1]
    assert last["open"] == D("441.0500") and last["adj_close"] == D("441.0600")
    assert last["volume"] == 22366600 and last["source"] == "alphavantage"
    assert last["adjustment_method"] == "split_dividend_adjusted"


def test_alphavantage_dividend_becomes_corporate_action_and_respects_start():
    session = FakeSession(FakeResponse(fixture_text("alphavantage_daily_adjusted_msft.json")))
    p = AlphaVantageProvider(CFG.providers["alphavantage"], env={"ALPHAVANTAGE_API_KEY": "k"},
                             session=session, sleep=lambda s: None, clock=clock)
    (r,) = p.fetch(["MSFT"])
    assert [(a["effective_date"], a["action_type"], a["cash_amount"]) for a in r.corporate_actions] == [
        (date(2024, 6, 6), "dividend", D("0.7500"))]
    assert len(r.records) == 3


def test_alphavantage_unadjusted_shape_has_no_adj_close():
    session = FakeSession(FakeResponse(fixture_text("alphavantage_daily_unadjusted_msft.json")))
    p = AlphaVantageProvider(CFG.providers["alphavantage"], env={"ALPHAVANTAGE_API_KEY": "k"},
                             session=session, sleep=lambda s: None, clock=clock)
    (r,) = p.fetch(["MSFT"])
    assert r.records[0]["adj_close"] is None and r.records[0]["volume"] == 22366600
    assert r.records[0]["adjustment_method"] == "none"


def test_alphavantage_notice_is_provider_error_not_empty_success():
    session = FakeSession(FakeResponse(fixture_text("alphavantage_rate_limit_notice.json")))
    p = AlphaVantageProvider(CFG.providers["alphavantage"], env={"ALPHAVANTAGE_API_KEY": "k"},
                             session=session, sleep=lambda s: None)
    with pytest.raises(ProviderError, match="rate limit"):
        p.fetch(["MSFT"])
