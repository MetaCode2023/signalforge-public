# Build prompt — The Analog Almanac

Paste everything below the line into a fresh Claude Code session on this repo.

---

You are working in the SIGNALFORGE repository. Read `README.md`, `docs/DECISIONS.md`,
`docs/ARCHITECTURE.md`, `reports/DATA_COVERAGE.md`, `reports/STRATEGY_RESULTS.md` and
`reports/OPEN_FINDINGS.md` before writing any code. Every rule in them still applies:
paper only, Decimal money, `PointInTimeView` for anything that looks at prices,
nothing silently repairs data, unknown is not zero, nothing promotes itself, and a
held-out window is looked at once.

## What to build

**The Analog Almanac**: a tamper-evident, point-in-time record of "what the world
looked like" on every trading day since 1972, what every stock in the warehouse did
next, and a public track record of forecasts that cannot be edited after the fact.

It has four parts. Build them in this order and stop after each one to run the
full test suite.

### 1. The Fingerprint table (DuckDB, new module `signalforge/almanac/`)

For every trading session from 1972-01-25 to the latest bar:

- Build a **macro fingerprint** from the 122 FRED series in `series`. Use only
  values whose observation date is on or before that session (enforce with the
  existing point-in-time guard — write a `PointInTimeSeriesView` if one does not
  exist, and a test proving it cannot return a future value).
- Normalise each series to a rolling percentile over the trailing 10 years
  available *at that date*, never the full history. A series with no value yet
  is `NULL` and stays `NULL`; it is not filled.
- Record `fingerprint_hash` (SHA-256 of the canonical vector) and
  `series_available` (how many of the 122 were known that day).
- **State the vintage problem on every output:** keyless FRED serves revised
  data, not what was known on the day. Store a `vintage_caveat` column and
  surface it in every rendered page. If ALFRED vintages are ever added, the
  column flips per series.

### 2. Forward outcomes (`almanac_outcomes`)

For every session × symbol in the triaged universe only (the 20 names that passed
corporate-action review, plus any symbol whose flagged jumps have all been
resolved — refuse the rest, and list them with the reason):

- Forward total return at 21, 63, 126 and 252 sessions using raw prices plus
  booked dividends and splits from `corporate_actions`, not Tiingo's adjusted
  close (the CVX 1973/1981 finding shows why).
- Max drawdown and days-to-recover within each horizon.
- Mark any outcome that crosses an unresolved quality flag as `TAINTED` and
  exclude it from aggregates.

### 3. The Twin Finder

Given any date (default: the latest session), find the K nearest historical
fingerprints using only dates that are at least 252 sessions earlier, so no twin's
outcome window overlaps the query date.

Output per query:
- the twins, their dates, distance, and which series drove the match;
- the distribution of each symbol's forward outcomes across the twins
  (median, 10th/90th percentile, hit rate vs. buy-and-hold over the same window);
- an evidence label using the existing thresholds — fewer than 30 independent
  twins (twins within 63 sessions of each other count once) is
  `INSUFFICIENT_EVIDENCE`, and the page says so before it shows any number;
- a **null comparison**: the same statistics for K *random* historical dates. If
  the twins are not distinguishable from random, say "no signal" in the headline.

Backtest the Twin Finder with the existing walk-forward harness over the train
and validation windows only. Register it in the experiment registry. **Do not run
it on the test window**; leave that to the human with `--run-holdout`.

### 4. The Ledger of Calls (the part nobody else has)

Every session, after the collector finishes:

- Run the Twin Finder on that day and write a **call**: date, fingerprint hash,
  twin dates, predicted outcome distributions, evidence label, code commit, and
  dataset fingerprint.
- Append it to a hash-chained `almanac_calls` table (each row carries the
  previous row's hash, same pattern as the paper ledger). No update or delete
  path may exist; add an `ast` test like `TestNoLiveOrderPath` that fails if one
  is introduced.
- When a call's horizon matures, score it automatically against what actually
  happened (calibration: did reality land inside the 10–90 band ~80% of the
  time?). Misses stay in the ledger forever.
- A daily chain-head hash goes into `reports/ALMANAC_CHAIN.md` and is committed,
  so the git history itself timestamps every call before its outcome exists.

### 5. Dashboard section

Add an "Almanac" tab to the static dashboard (inline SVG, no JavaScript, same as
the rest):
- Today's fingerprint as a heat strip of the 122 series percentiles.
- The top twins on a 1972–present timeline.
- Fan charts of forward outcomes vs. the random null.
- The running calibration score of every matured call, misses included.
- The vintage caveat, survivorship caveat and evidence label above every chart.

## Hard constraints

- **Licensing:** Tiingo bars may not be redistributed. The Almanac, its D1
  mirror and its dashboard stay private. If a public version is ever wanted,
  it may show only fingerprints (FRED-derived), twin dates, aggregate
  calibration scores and chain hashes — never per-symbol prices or returns.
  Write a test that fails if the public renderer receives a `bars` row.
- **Survivorship:** every universe name survived to 2026. Print that on every
  outcome table.
- No pandas (blocked on the operator's machine). DuckDB, Polars, NumPy only.
- Every new number in a report must be reproducible from a command written
  next to it.

## Done means

- All existing tests still pass; new tests cover the point-in-time series guard,
  the 252-session twin separation, the random-null comparison, TAINTED
  exclusion, the append-only ledger, and the public-renderer licence guard.
- `reports/ALMANAC_RESULTS.md` states first, plainly, whether the Twin Finder
  beat the random null on the walk-forward — and if it did not, says so.
- One call has been written to the ledger and its chain hash committed.
