# SIGNALFORGE — Data Coverage

Generated 2026-10-26 from the real warehouse at
`data/warehouse/signalforge.duckdb` (gitignored; licensed source data is never
committed).

## The headline, stated plainly

**The brief asks for 5–10 years of daily OHLCV across 100–500 stocks. That is
not reachable from any keyless, terms-clean source, and this build does not
have it.**

What it does have is 55 years of daily index closes for one instrument, two
years of one stock's real OHLCV, and a four-day-wide cross-section. Every
result in `STRATEGY_RESULTS.md` is bounded by that, and the boundary is stated
on every figure rather than left for a reader to infer.

## Warehouse contents (real counts)

```
$ python -I <scratch>/verify_ingest.py
IngestResult(provider='local_archive', status='ok',
  rows_in=85494, rows_accepted=85493, rows_quarantined=1, rows_suspect=58,
  inserted=85493, revised=0, raw_inserted=85494)
```

| Table | Rows |
|---|---:|
| `raw_records` (immutable as-ingested) | 85,494 |
| `bars` (clean, usable) | 26,524 |
| `series` (index and macro) | 58,969 |
| `quarantine` | 1 |
| `quality_flags` | 58 |
| `corporate_actions` | 0 |
| `data_revisions` | 0 |
| `ingest_runs` | 1 |

Re-ingesting the same archive changes nothing but `ingest_runs`, verified by
running it twice and comparing every table count.

## Per-symbol OHLCV — what exists

| Source | Symbols | Rows | Span | Usable for |
|---|---:|---:|---|---|
| `plotly_aapl_sample` | 1 (AAPL) | 506 | 2015-02-17 → 2017-02-16 | validating the OHLCV path on real prices with an adjusted close |
| `marketparquet` | 6,799 | 26,018 | 2026-10-20 → 2026-10-23 | **4 trading days.** A cross-section snapshot, not a backtest |

The MarketParquet block is 6,799 symbols wide and **four days deep**. Four days
is not a backtest. It is enough to exercise the cross-sectional code path and
nothing more, which is why cross-sectional momentum is reported as
INSUFFICIENT EVIDENCE rather than given a verdict.

Rows per symbol in that block: 4 rows for 6,167 symbols, 3 for 259, 2 for 197,
1 for 175.

## Index and macro series — what exists

| Series | Rows | Span |
|---|---:|---|
| NASDAQCOM | 14,048 | 1971-02-05 → 2026-10-23 |
| DGS10 (10y Treasury) | 16,188 | 1962-01-02 → 2026-10-23 |
| T10Y2Y (10y–2y spread) | 12,597 | 1976-06-01 → 2026-10-26 |
| VIXCLS | 9,302 | 1990-01-02 → 2026-10-23 |
| SP500 | 2,511 | 2016-10-27 → 2026-10-23 |
| DJIA | 2,512 | 2016-10-27 → 2026-10-26 |
| UNRATE (monthly) | 944 | 1948-01-01 → 2026-09-01 |
| FEDFUNDS (monthly) | 867 | 1954-07-01 → 2026-09-01 |

These counts were cross-checked against an independent profile of the source
CSVs taken before any code existed, and matched exactly.

SP500 and DJIA stop at 10 years because the data owner's licence caps them
there on FRED. That is a licence limit, not missing data.

## Known defects in the real data

| Finding | Detail | Handling |
|---|---|---|
| `impossible_ohlc` × 1 | **CGCF 2026-10-22: open 9.82 above high 9.81.** A genuine source defect | Quarantined unrepaired, raw payload preserved |
| `suspicious_price_jump` × 58 | All MarketParquet. e.g. AARD 3.95→1.39, AIOK 4.25→12.02, AONC 8→21, AXNX 0.4399→1.13, BBIG 0.003→0.0015 | Stored as SUSPECT with the prior close in the detail. Mostly micro-cap and penny names, so real-versus-bad is a human judgment, not a certainty |
| `missing_observation` × 243 | 17 for AAPL, 226 across MarketParquet | Counted only. **No rows fabricated.** Most AAPL and NASDAQCOM gaps are exchange holidays, since the counter uses weekdays |
| Zero fires | duplicate_symbol_date, negative_or_zero_price, negative_volume, zero_volume, non_trading_day, future_dated, stale_price, inconsistent_adjustment, malformed_record | Checked and clean. Stating this because an omitted section makes "no data" indistinguishable from "not checked" |

## Source policy — every source investigated, including the refusals

The refusals are recorded in `config/data_sources.yaml` with their reasons, so
the decision is not re-litigated from memory in a later session.

| Source | Status | Evidence |
|---|---|---|
| **FRED** `fredgraph.csv` | **ACTIVE**, keyless | HTTP 200 verified. Documented public download |
| **Local archive** | **ACTIVE**, no network | Files the operator already acquired. MarketParquet's licence permits internal research and forbids redistribution, so `data/archive/**` is gitignored |
| **Tiingo** | **PENDING KEY** | Preferred for per-symbol OHLCV. Free tier ~1000 req/day, 500 symbols/month. Adapter written and fixture-tested; cannot run |
| **Alpha Vantage** | **PENDING KEY** | Fallback. The shared `demo` key returns only a "claim your free key" notice (verified). 25 req/day means 20 days to fill 500 symbols |
| **Stooq** | **REJECTED** | The CSV endpoint returns a JavaScript proof-of-work browser challenge; the bulk archive at `static.stooq.com` returns HTTP 401. Both are deliberate anti-automation controls, and solving one would be bypassing an access control |
| **MarketParquet API** | **REJECTED for automated pulls** | `robots.txt` contains `Disallow: /api/` and every sample download URL is under `/api/`. The free tier is also only a few recent days, so it cannot supply history anyway |
| **Yahoo / yfinance** | **REJECTED after review** | Undocumented endpoint, terms adverse to automated collection, known to break silently. A warehouse whose provenance is an undocumented endpoint cannot claim reproducibility |
| **Nasdaq Data Link** | **REJECTED for now** | HTTP 403 without a key (verified), and the free WIKI price table has been frozen since 2018 |

## Survivorship bias — unfixable with this data, so stated

- The 23-name universe in `config/research.yaml` consists entirely of
  companies that survived to 2026. Every firm that failed over the same period
  is absent by construction.
- The MarketParquet cross-section is a snapshot of symbols **listed today**, so
  it contains no delisted company at all.
- There is no delisted-securities dataset available on the permitted sources.

A backtest over a survivor-only universe overstates returns and understates
drawdown. No amount of code fixes that; only different data does.

## The one action that would unblock this

Claiming a **free Tiingo API key** (no payment, no card) would make per-symbol
daily OHLCV with adjusted closes, splits and dividends ingestable immediately —
the adapter already exists and is tested. That creates an account in Austin's
name, so it is his call, not mine. Nothing else in this build is waiting on it.
