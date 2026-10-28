# Free market data only

No paid plan, paid endpoint, upgrade, or live trading is authorized. Remote
reporting remains pending its verified API contract. The existing research
universe is 23 US stocks/ETFs and the execution model uses daily bars.

## Provider selection

The existing Tiingo EOD adapter is suitable for daily OHLCV and adjustment/
corporate-action fields. Tiingo's published **Starter** plan is $0/month with
500 unique symbols/month, 50 requests/hour, and 1,000/day. Access and coverage
for this account and each symbol are not verified without its free API key.
The individual plan restricts use to the account holder; do not redistribute
data or assume it grants organizational use rights. Select only the $0 plan
whose permissions cover the actual use. Do not upgrade if access is refused.

- Pricing and plan limits: https://app.tiingo.com/pricing/
- EOD API fields/timing: https://www.tiingo.com/documentation/end-of-day

`config/collection-tiingo-free.json` contains the existing 23-symbol universe,
a daily 03:00 UTC schedule, one attempt per cycle, and a local 46-request/day
budget. This permits two 23-symbol verification cycles. Keep those within the
published 50-request/hour account budget, including other API uses. This local
daily budget is deliberately conservative; the collector does not implement
an account-wide rolling hourly quota or know requests made by other clients.
The provider is **still disabled** until local setup and access validation.

FRED is keyless and free, but NASDAQCOM and VIXCLS are daily index closes,
not OHLCV for the 23 stock/ETF symbols. Local archives are offline historical
inputs and do not establish a fresh feed.

The existing Alpha Vantage adapter requests `TIME_SERIES_DAILY_ADJUSTED` with
`outputsize=full`. Those capabilities are premium under current documentation,
so it must remain disabled for this free-only setup. A future free fallback
would require `TIME_SERIES_DAILY` plus `outputsize=compact` (100 observations),
and explicit handling of unadjusted prices. It is not enabled here.

- https://www.alphavantage.co/documentation/

## Bounded attempt and network diagnosis

At 2026-10-27 18:32:26 America/Phoenix (2026-10-28 01:32:26 UTC), one
bounded FRED cycle requested NASDAQCOM and VIXCLS in
`data/verification/fred-freshness.duckdb`. After reopening DuckDB:

- Stored series rows: 0; raw records: 0; failed ingest runs: 1.
- Newest stored observation: none. Collection health: `failed`.
- Status: `data/verification/fred-freshness.duckdb.collector.json`.
- Events: `data/verification/fred-freshness.duckdb.collector.jsonl`.
- Summary: `data/verification/fred-freshness-result.json`.

A separate credential-free TCP diagnostic to `fred.stlouisfed.org:443` returned
`PermissionError`, errno 13, Windows error 10013. This is an execution-environment
access failure, not evidence of a source outage or a market-calendar failure.
No additional collection retries or continuous process were started.

Tiingo and Alpha Vantage are both disabled/pending credentials. Their configured
key variable names are absent from this process environment. No environment
file or actual credential value was read or printed.

## Freshness rules before any continuous launch

Tiingo documents most US equity EOD bars as available by 5:30 PM EST, with
corrections through 8 PM EST. The proposed 03:00 UTC daily polling time is after
that correction window in both US Eastern seasonal offsets. These are daily
observations, not current intraday quotes.

Evaluate each symbol against the latest completed exchange session whose
provider publication window has elapsed. During weekends, full market holidays,
and before publication, the prior eligible session is normal. Early-close
sessions are still valid observation dates. Do not compare daily bars to an
arbitrary wall-clock interval and call a closed market an outage.

Official calendar: https://www.nyse.com/markets/hours-calendars

The existing collector's seven-calendar-day health threshold is only a coarse
staleness alarm. It is **not** sufficient proof that all 23 daily bars are current.
Before starting continuous collection, add/verify the provider/calendar-aware
gate against observed records. Do not start solely because the coarse status
says `ok`. Intraday collection is outside this EOD provider configuration.

FRED's series pages currently show observations through 2026-10-26. NASDAQCOM
shows a late-evening update; VIXCLS shows its next release on 2026-10-28.
At the time of this check, an October 26 observation is not by itself evidence
of a FRED outage. Those website observations were not imported or represented
as a successful local collection.

- https://fred.stlouisfed.org/series/NASDAQCOM
- https://fred.stlouisfed.org/series/VIXCLS

## Exact local setup

1. Obtain a key from a **$0 Tiingo Starter** account. Use the provider's own
   account page; never paste the key into chat or a terminal command.
2. In a normal Windows PowerShell terminal, enter the key at a hidden prompt.
   It is held only in this process environment, not a shell command or file:

```powershell
Set-Location 'C:\Users\you\repos\signalforge'
$tiingoKeyInput = Read-Host 'Free Tiingo API key (hidden)' -AsSecureString
$env:TIINGO_API_KEY = [System.Net.NetworkCredential]::new('', $tiingoKeyInput).Password
Remove-Variable tiingoKeyInput
```

3. In `config/data_sources.yaml`, change **only** the Tiingo block's
   `status: pending_key` to `status: active` and `enabled: false` to
   `enabled: true`. Leave the existing `api_key_env: TIINGO_API_KEY` name and
   other providers unchanged. That YAML contains variable names, not keys.
4. In the same normal terminal, run exactly one bounded cycle. Do not use
   `--continuous` yet:

```powershell
& '.venv\Scripts\python.exe' -m signalforge.collector_cli --config config/collection-tiingo-free.json --db data/warehouse/market-daily.duckdb --cycles 1
```

5. Reopen DuckDB and verify every requested symbol's newest observation against
   the exchange calendar and provider delay. After a successful fresh-data
   gate, use the warehouse lock to exclude duplicates before launching one
   collector. Record the process ID, logs, launch/stop commands, and confirm
   a second scheduled cycle. None of these launch steps has occurred yet.

The variable set in a separate PowerShell window is not automatically inherited
by an already-running ChatGPT/Codex process. Run the bounded command in that
same window. No credential is needed in chat to review its safe status output.
If the provider or plan refuses access, stop; do not accept a paid upgrade.
