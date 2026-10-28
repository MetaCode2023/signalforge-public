from __future__ import annotations

import csv
import io
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable
from urllib.parse import urlencode

import requests

from signalforge.config.models import ProviderConfig
from signalforge.core.errors import ProviderError, TransientProviderError

DEFAULT_ARCHIVE_DIR = Path(__file__).resolve().parents[2] / "data" / "archive"
TIMEOUT_SECONDS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FetchResult:
    """What one request (or one file) returned, with its provenance attached."""

    source: str
    source_reference: str
    retrieved_at: datetime
    kind: str
    records: list[dict] = field(default_factory=list)
    corporate_actions: list[dict] = field(default_factory=list)


@runtime_checkable
class DataProvider(Protocol):
    name: str
    kind: str

    def fetch(self, *args, **kwargs) -> list[FetchResult]: ...


def _dec(text):
    """Decimal when parseable; otherwise the raw value, so validation quarantines it."""
    if text is None:
        return None
    try:
        out = Decimal(str(text).strip())
    except InvalidOperation:
        return text
    return out if out.is_finite() else text


def _day(text):
    try:
        return date.fromisoformat(str(text).strip()[:10])
    except ValueError:
        return text


def _int(text):
    d = _dec(text)
    if isinstance(d, Decimal) and d == d.to_integral_value():
        return int(d)
    return d


class _Paced:
    def __init__(self, config: ProviderConfig, sleep: Callable[[float], None], clock) -> None:
        self.config = config
        self.name = config.name
        self.kind = config.kind
        self._sleep = sleep
        self._clock = clock
        self._requests_made = 0

    def _pace(self) -> None:
        rpm = self.config.rate_limit_per_minute
        if self._requests_made and rpm:
            self._sleep(60.0 / rpm)
        self._requests_made += 1

    def _get(self, session, url: str, headers: dict) -> str:
        self._pace()
        try:
            response = session.get(url, headers=headers, timeout=TIMEOUT_SECONDS, allow_redirects=False)
        except requests.exceptions.SSLError:
            raise ProviderError(f"{self.name}: TLS verification failed") from None
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TransientProviderError(f"{self.name}: request failed ({type(exc).__name__})") from None
        except requests.RequestException as exc:
            raise ProviderError(f"{self.name}: request failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            error = TransientProviderError if response.status_code == 429 or response.status_code >= 500 else ProviderError
            raise error(f"{self.name}: HTTP {response.status_code}")
        return response.text


def parse_fred_csv(text: str, *, source: str = "fred", source_reference: str = "",
                   retrieved_at: datetime | None = None) -> list[dict]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows[0]) < 2 or rows[0][0].strip().lower() not in ("observation_date", "date"):
        raise ProviderError("not a FRED CSV: header must start with observation_date")
    series_ids = [h.strip() for h in rows[0][1:]]
    out: list[dict] = []
    for row in rows[1:]:
        if not row:
            continue
        try:
            day = date.fromisoformat(row[0].strip())
        except ValueError:
            raise ProviderError(f"FRED CSV has an unparseable date {row[0]!r}") from None
        for sid, raw in zip(series_ids, row[1:]):
            raw = raw.strip()
            if raw in ("", "."):
                continue
            try:
                value = Decimal(raw)
            except InvalidOperation:
                raise ProviderError(f"FRED CSV has a non-numeric value {raw!r} for {sid}") from None
            out.append({
                "series_id": sid, "observation_date": day, "value": value, "source": source,
                "source_reference": source_reference, "retrieved_at": retrieved_at,
            })
    return out


class FredSeriesProvider(_Paced):
    def __init__(self, config: ProviderConfig, *, user_agent: str = "signalforge-research/0.1",
                 session=None, sleep=time.sleep, clock=_utcnow) -> None:
        super().__init__(config, sleep, clock)
        self.user_agent = user_agent
        self.session = session or requests.Session()

    def fetch(self, series_ids, start: date | None = None, end: date | None = None) -> list[FetchResult]:
        params = {"id": ",".join(series_ids)}
        if start:
            params["cosd"] = start.isoformat()
        if end:
            params["coed"] = end.isoformat()
        url = f"{self.config.base_url}?{urlencode(params, safe=',')}"
        text = self._get(self.session, url, {"User-Agent": self.user_agent})
        retrieved_at = self._clock()
        records = parse_fred_csv(text, source="fred", source_reference=url, retrieved_at=retrieved_at)
        return [FetchResult("fred", url, retrieved_at, "series", records)]


SERIES_FILES = ("fred_market_indices", "fred_rates_daily", "fred_macro_monthly")
AAPL_FILE = "aapl_sample_2015_2017"
MARKETPARQUET_FILE = "marketparquet_recent_stock_ohlcv"
ARCHIVE_DATASETS = SERIES_FILES + (AAPL_FILE, MARKETPARQUET_FILE)
AAPL_SOURCE = "plotly_aapl_sample"
MARKETPARQUET_SOURCE = "marketparquet"


class LocalArchiveProvider:
    name = "local_archive"
    kind = "archive"

    def __init__(self, archive_dir: str | Path = DEFAULT_ARCHIVE_DIR) -> None:
        self.archive_dir = Path(archive_dir)

    def fetch(self, datasets=None, symbols=None) -> list[FetchResult]:
        names = list(datasets) if datasets is not None else list(ARCHIVE_DATASETS)
        wanted = set(symbols) if symbols is not None else None
        results = []
        for name in names:
            if name not in ARCHIVE_DATASETS:
                raise ProviderError(f"unknown archive dataset {name!r}; known: {ARCHIVE_DATASETS}")
            path = self.archive_dir / f"{name}.csv"
            if not path.is_file():
                raise ProviderError(f"archive file not found: {path}")
            retrieved_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            ref = str(path)
            if name in SERIES_FILES:
                records = parse_fred_csv(
                    path.read_text(encoding="utf-8"), source="fred", source_reference=ref,
                    retrieved_at=retrieved_at,
                )
                if wanted is not None:
                    records = [r for r in records if r["series_id"] in wanted]
                results.append(FetchResult("fred", ref, retrieved_at, "series", records))
            else:
                results.append(self._bars(name, path, ref, retrieved_at, wanted))
        return results

    def _bars(self, name, path, ref, retrieved_at, wanted) -> FetchResult:
        if name == AAPL_FILE:
            source, method, date_col, adj_col = AAPL_SOURCE, "provider_adjusted_close", "date", "adjusted_close"
        else:
            source, method, date_col, adj_col = MARKETPARQUET_SOURCE, "split_adjusted_no_dividends", "date", None
        records = []
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            needed = {"symbol", date_col, "open", "high", "low", "close", "volume"}
            if adj_col:
                needed.add(adj_col)
            missing = needed - set(reader.fieldnames or [])
            if missing:
                raise ProviderError(f"{path.name}: missing expected columns {sorted(missing)}")
            for row in reader:
                symbol = row["symbol"].strip()
                if wanted is not None and symbol not in wanted:
                    continue
                records.append({
                    "symbol": symbol, "trade_date": _day(row[date_col]),
                    "open": _dec(row["open"]), "high": _dec(row["high"]), "low": _dec(row["low"]),
                    "close": _dec(row["close"]), "volume": _int(row["volume"]),
                    "adj_close": _dec(row[adj_col]) if adj_col else None,
                    "source": source, "source_reference": ref, "retrieved_at": retrieved_at,
                    "adjustment_method": method,
                })
        return FetchResult(source, ref, retrieved_at, "bars", records)


class _Keyed(_Paced):
    key_label = ""

    def __init__(self, config: ProviderConfig, *, user_agent: str = "signalforge-research/0.1",
                 env: Mapping[str, str] | None = None, session=None, sleep=time.sleep,
                 clock=_utcnow) -> None:
        super().__init__(config, sleep, clock)
        self.user_agent = user_agent
        self.env = os.environ if env is None else env
        self.session = session or requests.Session()

    def _key(self) -> str:
        var = self.config.api_key_env
        key = self.env.get(var or "")
        if not key:
            raise ProviderError(f"{self.name}: no API key in {var}; set that environment variable")
        return key


def _action(symbol, day, kind, source, ratio=None, cash=None) -> dict:
    return {"symbol": symbol, "effective_date": day, "action_type": kind, "ratio": ratio,
            "cash_amount": cash, "source": source}


class TiingoProvider(_Keyed):
    def fetch(self, symbols, start: date | None = None, end: date | None = None) -> list[FetchResult]:
        key = self._key()
        results = []
        for symbol in symbols:
            params = {"format": "json"}
            if start:
                params["startDate"] = start.isoformat()
            if end:
                params["endDate"] = end.isoformat()
            url = f"{self.config.base_url}/{symbol}/prices?{urlencode(params)}"
            headers = {"User-Agent": self.user_agent, "Authorization": f"Token {key}"}
            text = self._get(self.session, url, headers)
            retrieved_at = self._clock()
            try:
                payload = json.loads(text, parse_float=Decimal)
            except ValueError:
                raise ProviderError("tiingo: response was not valid JSON") from None
            if not isinstance(payload, list):
                raise ProviderError("tiingo: unexpected response shape (expected a list of bars)")
            result = FetchResult("tiingo", url, retrieved_at, "bars")
            for row in payload:
                day = str(row["date"])[:10]
                result.records.append({
                    "symbol": symbol, "trade_date": _day(day), "open": _dec(row.get("open")),
                    "high": _dec(row.get("high")), "low": _dec(row.get("low")),
                    "close": _dec(row.get("close")), "volume": _int(row.get("volume")),
                    "adj_close": _dec(row.get("adjClose")), "source": "tiingo",
                    "source_reference": url, "retrieved_at": retrieved_at,
                    "adjustment_method": "split_dividend_adjusted",
                })
                d = date.fromisoformat(day)
                div, split = _dec(row.get("divCash")), _dec(row.get("splitFactor"))
                if isinstance(div, Decimal) and div > 0:
                    result.corporate_actions.append(_action(symbol, d, "dividend", "tiingo", cash=div))
                if isinstance(split, Decimal) and split != 1:
                    result.corporate_actions.append(_action(symbol, d, "split", "tiingo", ratio=split))
            results.append(result)
        return results


class AlphaVantageProvider(_Keyed):
    def fetch(self, symbols, start: date | None = None, end: date | None = None) -> list[FetchResult]:
        key = self._key()
        results = []
        for symbol in symbols:
            params = {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol, "outputsize": "full"}
            reference = f"{self.config.base_url}?{urlencode(params)}"
            url = f"{reference}&{urlencode({'apikey': key})}"
            text = self._get(self.session, url, {"User-Agent": self.user_agent})
            retrieved_at = self._clock()
            try:
                payload = json.loads(text, parse_float=Decimal)
            except ValueError:
                raise ProviderError("alphavantage: response was not valid JSON") from None
            for notice in ("Error Message", "Note", "Information"):
                if notice in payload:
                    reason = "rate limit or access notice" if notice in ("Note", "Information") else "request refused"
                    raise ProviderError(f"alphavantage: {reason}")
            series = payload.get("Time Series (Daily)")
            if not isinstance(series, dict):
                raise ProviderError("alphavantage: response has no 'Time Series (Daily)'")
            result = FetchResult("alphavantage", reference, retrieved_at, "bars")
            for day in sorted(series):
                d = date.fromisoformat(day)
                if (start and d < start) or (end and d > end):
                    continue
                row = series[day]
                adj = row.get("5. adjusted close")
                volume = row.get("6. volume", row.get("5. volume"))
                result.records.append({
                    "symbol": symbol, "trade_date": d, "open": _dec(row.get("1. open")),
                    "high": _dec(row.get("2. high")), "low": _dec(row.get("3. low")),
                    "close": _dec(row.get("4. close")), "volume": _int(volume),
                    "adj_close": _dec(adj) if adj is not None else None, "source": "alphavantage",
                    "source_reference": reference, "retrieved_at": retrieved_at,
                    "adjustment_method": "split_dividend_adjusted" if adj is not None else "none",
                })
                div, split = _dec(row.get("7. dividend amount")), _dec(row.get("8. split coefficient"))
                if isinstance(div, Decimal) and div > 0:
                    result.corporate_actions.append(_action(symbol, d, "dividend", "alphavantage", cash=div))
                if isinstance(split, Decimal) and split != 1:
                    result.corporate_actions.append(_action(symbol, d, "split", "alphavantage", ratio=split))
            results.append(result)
        return results
