# Collection and verification

Paper-only implementation on `feature/signalforge-quant-research`. No service,
scheduled task, collector daemon, or research agent was started by this work.
Existing runtime status remains **unknown**. The existing untracked
`docs/REPORTING_SETUP.md` is preserved.

## Collector behavior

`python -m signalforge.collector_cli` performs **one cycle**, then exits.
Only an explicit `--continuous` requests an unbounded loop. `--cycles N` is
bounded. This command collects data; it never starts the paper runner or picks,
approves, promotes, or runs a strategy.

- Reuses the existing providers, validation, ingestion, DuckDB schema, logical
  row upserts, raw-record deduplication, revision tracking, and ingest-run log.
  Raw records are deduplicated by provider/source and content hash even when
  rolling request URLs change; the first retrieval's provenance is retained.
  The collector exhausts its watermark query before staging Arrow data, as
  required by DuckDB 2.0's result lifecycle.
  Network pulls start from the oldest persisted requested item minus the
  configured correction lookback, so an extended outage is caught up rather
  than silently discarded. An item with no history starts at the lookback.
- `config/collection.json` selects daily FRED index/volatility series. The first
  cycle runs immediately. Subsequent cycles run at 23:30 UTC. Set
  `daily_at_utc` to null to use an interval instead. Daily data is not a tick feed.
- Only providers explicitly active and enabled in `config/data_sources.yaml`
  can run. Tiingo and Alpha Vantage remain disabled. No environment file is
  read; keyed providers would use their already documented environment names.
- Provider request pacing is preserved. Retry delays cannot be shorter than
  the provider's configured request spacing. Each retry creates a new session.
  Transport failures, HTTP 429, and HTTP 5xx receive bounded exponential
  backoff with jitter. Authentication, configuration, and parsing errors do not.
- A persisted UTC-day request budget reserves requests **before** every fetch,
  including failed and partially completed batches. The example allows 24/day;
  one FRED batch costs one request, equity batches cost one per symbol, and
  archive reads cost zero. This is an operator budget, not a claim about a
  vendor's current entitlement or quota-reset timezone. Corrupt budget state
  fails closed. Alpha Vantage additionally refuses a budget over 25/day.
- Every cycle writes UTC status to `<db>.collector.json` and appends safe
  events to `<db>.collector.jsonl`. Existing ingestion timestamps are retained
  in the warehouse. Vendor error bodies and credential-bearing details are
  excluded from collector/ingest failure logs by the collector's provider wrapper.
- Freshness uses the latest **observation date**, not the retrieval time. Each
  requested item must exist. Missing, future, or old observations are flagged;
  an old archive does not become healthy merely because it was read again.
  `stale_after_days_by_item` permits longer limits for monthly releases, which
  also need a sufficiently long `lookback_days`. The default seven-day daily
  threshold tolerates ordinary weekends, not every possible exchange holiday.
- An OS-held lock on the resolved warehouse path refuses a second collector,
  including another provider targeting that warehouse. Windows byte locking
  and POSIX flock release on process exit/crash; persistent lock-file existence
  is never treated as proof of a running process. Do not delete lock files.
- The warehouse is closed before each scheduling wait. An unrelated process
  already holding an incompatible DuckDB connection still causes a safe failure;
  the collector does not terminate it or remove database locks.

Collector code is separate from `signalforge/paper/**`, strategies, and risk
controls. Existing order/risk settings were not changed.

## Verification performed

The local ignored `.venv` uses supported Python 3.13.15. Austin installed the
declared `.[dev]` dependencies from a normal PowerShell terminal after this
automation environment's downloads were blocked (socket permission error 10013).
Dependency requirements were not relaxed. Installed versions include DuckDB
2.0.0, Polars 2.1.0, PyArrow 26.0.0, NumPy 2.5.4, Pydantic 2.14.1, PyYAML 6.0.3,
requests 2.34.2, pytest 9.2.0, and Hypothesis 6.170.12.

- **24 collector tests passed on Python 3.13.15.** Includes actual Windows
  cross-process exclusion, recovery after abrupt process exit, scheduling,
  budgets across restarts, bounded retry, timestamped event persistence,
  freshness checks, and secret-safe error handling.
- **4 real-DuckDB collector integration tests passed on Python 3.13.15.**
  They verify persistence after reopening, timestamped observations, deduplication
  across changed request windows, retry/reconnection with safe failure logs,
  refusal of disabled providers, no authentication retry, and catch-up from an
  August 2023 observation to January 2024 fixture observations. A new storage
  regression test also verifies preservation of first-retrieval provenance.
- **Full supported-runtime suite: 1,201 passed, 1 network test deselected,
  31 subtests passed in 282.72 seconds.** The earlier Python 3.11 supplementary
  results have been superseded. The XML counts 1,232 test/subtest results with
  zero failures/errors. Full results are in `data/verification/full-suite.xml`;
  focused integration results are in `data/verification/collector-integration.xml`.
- The default pytest temporary/cache directories were inaccessible in this
  automation session. Use a fresh `--basetemp` inside `data/verification`, a
  workspace-local `cache_dir`, and a local Hypothesis storage directory. No
  system permissions or security settings were changed.
- Syntax compilation passed. No live data fetch or signed reporting request
  was sent. No persistent service was enabled.

One bounded local-archive collection cycle completed at
**2026-10-27 18:29:22 America/Phoenix (2026-10-28 01:29:22 UTC)**. After closing
the collector connection and reopening the database read-only:

| Check | Observed |
|---|---|
| Database | `data/verification/collector-smoke.duckdb` |
| Stored bars / immutable raw records | 506 / 506 (previously 0 / 0) |
| Ingest runs | 1 successful, 0 failed |
| Quarantined records | 0 |
| Observation range | 2015-02-17 through 2017-02-16 |
| Ingest started / finished, UTC | 2026-10-28 01:29:22.048447 / 01:29:22.095982 |
| Source archive retrieval timestamp, UTC | 2026-10-27 02:58:20.156462 |
| Collector health / exit | `stale` / 2, expected for this old sample |

`data/verification/integration-verification.json` retains the dependency
versions, test summary, before/after counts, collection status, and timestamps.
No second cycle or continuous service was started. The main research warehouse
was not used by this bounded collection check.

## Bounded historical replay

`signalforge.verification_cli` runs one offline replay using the existing
`BacktestEngine`, validator, configuration, and metrics. It reads one local CSV
without opening the main warehouse and never consumes experiment-registry
holdout state. It does not tune parameters or promote a strategy.

Observed run repeated successfully on supported Python 3.13.15:

| Field | Value |
|---|---|
| Dataset | `data/archive/aapl_sample_2015_2017.csv`, `plotly_aapl_sample` |
| Window | 2015-02-17 through 2017-02-16; 506 daily AAPL bars |
| Strategy | Moving-average trend, fast=20, slow=50, target weight=0.05 |
| Simulated starting/ending equity | 100,000 / 100,091.171524 |
| Commission | 0.005/share, minimum 1.00 per fill |
| Slippage | 5 bps each way; configured gap multiplier 2.0 |
| Execution | Earliest next-bar fill, volume participation cap 1%, partial fills allowed |
| Measured cost | Commission 3.00; slippage 7.288400 |
| Closed trades / fills | 1 / 3 |
| Evidence | `INSUFFICIENT_EVIDENCE` |
| Output | `data/verification/bounded-backtest.json` (ignored) |

The JSON records the source SHA-256, dataset fingerprint, complete execution
and risk assumptions, metrics, and the satisfied Python-version requirement.
Limitations: one surviving stock, an old sample already examined in this
project, daily rather than intraday execution, no explicit dividend/split cash
flows, and limited capital deployment under unchanged risk limits. This is an
engineering replay, not independent validation or evidence of a tradable edge.

## Reporting contract: still blocked

The only matching contract note found locally is `docs/REPORTING_SETUP.md`.
It documents:

- POST `https://signalforge-api.example.com/v1/ingest`; no credential-carrying redirects.
- Cloudflare Access headers `CF-Access-Client-Id`, `CF-Access-Client-Secret`.
- HMAC-SHA256 over the exact UTF-8 bytes of
  `v1:<timestamp>:<nonce>:<raw body>`.
- `x-signalforge-timestamp`, `x-signalforge-nonce`, and
  `x-signalforge-signature: v1=<hex digest>`; fresh timestamp/nonce per attempt.
- Expected successful acceptance HTTP 202; test records isolated from metrics.

Still missing: the deployed allowlisted JSON **report types, envelope, required
and optional field names, value types/enums, and schema/version rules**. Also
missing: the stable report identity and duplicate/replay semantics needed for
safe retries, acceptance-response shape, and a documented way to confirm a test
record in D1/dashboard. The notes explicitly say the deployed schema was not
retrieved. No sender, guessed payload, or invented endpoint was added.

## Reproduction and next steps

Run from `C:\Users\you\repos\signalforge`.

Verified, dependency-free control tests:

```powershell
& '.venv\Scripts\python.exe' -m unittest tests.test_collector -v
```

Supported-runtime smoke checks and replay:

```powershell
& '.venv\Scripts\python.exe' -m unittest tests.test_engine_smoke tests.test_collector_providers -v
& '.venv\Scripts\python.exe' -m signalforge.verification_cli
```

Install the unchanged declared dependencies if needed. For the full suite,
select a new temporary folder each time (pytest clears an existing basetemp):

```powershell
uv pip install --python '.venv\Scripts\python.exe' -e '.[dev]'
$env:HYPOTHESIS_STORAGE_DIRECTORY = Join-Path $PWD 'data\verification\hypothesis'
$testTemp = 'data/verification/pytest-' + [guid]::NewGuid().ToString('N')
& '.venv\Scripts\python.exe' -m pytest -m 'not network' --basetemp=$testTemp -o cache_dir=data/verification/pytest-cache --junitxml=data/verification/full-suite.xml
& '.venv\Scripts\python.exe' -m signalforge.verification_cli
```

After the suite passes, a bounded **offline** collection check is:

```powershell
& '.venv\Scripts\python.exe' -m signalforge.collector_cli --config config/collection-archive.json --db data/verification/collector-smoke.duckdb --cycles 1
```

That old AAPL archive should produce status `stale` and exit 2 after successful
ingestion. Deduplication and catch-up are verified separately by the integration
tests above. Do not start `--continuous` yet.
