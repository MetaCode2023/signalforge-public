# Build prompt — The Analog Almanac (v2)

Paste everything below the line into a fresh Claude Code session on this repo.

---

You are working in the SIGNALFORGE repository. Read `README.md`, `docs/DECISIONS.md`,
`docs/ARCHITECTURE.md`, `reports/DATA_COVERAGE.md`, `reports/STRATEGY_RESULTS.md` and
`reports/OPEN_FINDINGS.md` before writing any code. Every rule in them still applies:
paper only, Decimal money, `PointInTimeView` for anything that looks at prices,
nothing silently repairs data, unknown is not zero, nothing promotes itself, and a
held-out window is looked at once.

## What to build

**The Analog Almanac**: a tamper-evident, point-in-time record of what the world
looked like on every trading day since 1972, what every audited stock did next,
and a forecast track record that provably cannot be edited after the fact — with
its own failure conditions written down before it ever makes a call.

Build in the order below. Stop after each step and run the full test suite.

---

### Step 0 — Pre-register before touching data

Before any code reads `series` or `bars`, write `docs/ALMANAC_PREREGISTRATION.md`
and commit it on its own. It must fix, in advance:

- the fingerprint families and their weights (Step 1);
- the distance metric, K, the twin-separation rule and the horizons;
- the baselines the Twin Finder must beat (Step 4) and by how much;
- the scoring rules (Step 5);
- the failure criteria that retire the Almanac (Step 8).

Store the commit hash of that file in the experiment registry. Any later change
to these choices is a **new experiment** with a new registry entry and a new
walk-forward; the old one's results are kept, not overwritten. Add a test that
fails if `signalforge/almanac/` reads a parameter that is not in the
pre-registration file.

### Step 1 — The Fingerprint table (`signalforge/almanac/fingerprint.py`)

For every trading session from 1972-01-25 to the latest bar:

**Release lag, not observation date.** FRED dates a value by the period it
describes, not the day it was published (Q3 GDP is "2026-07-01" but appears in
late October). Using observation dates is look-ahead. Build a `release_lags`
table: one row per series with a conservative publication lag in sessions,
sourced from each series' FRED release schedule and committed with its source.
A value is visible on session T only if `observation_date + lag <= T`. Write a
`PointInTimeSeriesView` that enforces this and a test proving GDP for a quarter
is invisible until its lag has passed. A series with no known lag is excluded,
and the exclusion is listed — it is not given a lag of zero.

**Families, not 122 equal votes.** Twenty rate series would otherwise outvote
one volatility series. Assign every series to one family — rates, curve shape,
credit, inflation, labour, growth, volatility, liquidity, energy, FX — and weight
families equally. Within a family, reduce to at most 3 components with PCA fit
**only on data visible at that date** (expanding window, refit at most yearly).

**Normalise point-in-time.** Each input becomes a rolling percentile over the
trailing 10 years available at that date. A series not yet published is `NULL`
and stays `NULL`; a family with no members that day is `NULL`; the distance
function skips `NULL` families and records how many it compared.

Store `fingerprint_hash`, `families_available`, `series_available`, and a
`vintage_caveat` column (keyless FRED serves revised values, not what was
known on the day — surface that on every rendered page).

### Step 2 — Forward outcomes (`almanac_outcomes`)

For every session × symbol in the **triaged** universe only (the 20 names that
passed corporate-action review, plus any symbol whose flagged jumps are all
resolved — refuse the rest and list them with the reason):

- Forward total return at 21, 63, 126 and 252 sessions from raw prices plus
  booked dividends and splits in `corporate_actions` — not Tiingo's adjusted
  close (the CVX 1973/1981 fake crashes are why).
- Max drawdown and sessions-to-recover within each horizon.
- `TAINTED` if the window crosses an unresolved quality flag; excluded from all
  aggregates and counted in the output.

**Survivorship counterweight.** Every universe name survived to 2026. Alongside
the stock outcomes, compute the same outcomes for the index proxies
(NASDAQCOM, and SPY/QQQ/IWM from their listing) — instruments whose results do
not depend on picking survivors. Every page shows the index row first, the
survivor-biased stock rows second, with the caveat between them.

### Step 3 — The Twin Finder

Given a date (default: the latest session), return the K nearest historical
fingerprints, drawn only from dates at least 252 sessions earlier so no twin's
outcome window overlaps the query.

**Effective sample size, not raw K.** Twins from the same episode are one
observation, not ten. Cluster twins that fall within 63 sessions of each other,
count each cluster once, and report `n_effective`. Below 30, the label is
`INSUFFICIENT_EVIDENCE` and the page says so before it shows any number. Use
block-bootstrap confidence intervals (block length ≥ the horizon), never i.i.d.

**Explain the match.** For each twin show which families drove the similarity,
and tag it with the episode it belongs to from a curated `episodes` table
(e.g. 1973–74 oil shock, 1987, 2000–02, 2008, 2020, 2022 rate shock). The
table is hand-written, committed, and dated; label nothing by inference.

### Step 4 — Baselines it must beat

Report every Twin Finder result next to three baselines, all computed with the
same separation and clustering rules:

1. **Climatology** — the unconditional distribution of outcomes over all
   earlier dates.
2. **Random dates** — K random earlier dates, repeated 1,000 times; report the
   percentile of the Twin Finder's score within that distribution.
3. **Regime-matched random** — K random dates from the same VIX tercile and
   decade, to separate "found the right analogue" from "noticed it was a
   volatile year".

If the Twin Finder does not beat all three by the pre-registered margin, the
headline reads **"No signal"** and the outcome charts render greyed out.

### Step 5 — Proper scoring

Coverage of a 10–90 band is not enough; a forecast that always predicts a huge
band covers everything. Score every forecast with:

- **CRPS** (or pinball loss across the 10/25/50/75/90 quantiles), expressed as
  a **skill score against climatology** — positive means better than knowing
  nothing;
- a **reliability diagram** (predicted quantile vs. observed frequency);
- **sharpness** (average band width), reported beside calibration so a wide,
  useless band cannot pass.

**Multiple testing.** 20+ symbols × 4 horizons is 80+ tests. The headline
claim is the single pre-registered portfolio-level skill score. Per-symbol
results are shown with Benjamini–Hochberg-adjusted significance and a line
stating how many would be expected to look good by chance.

Backtest with the existing walk-forward harness over train and validation only.
Register it. **Do not run the test window** — that is the human's single look
via `--run-holdout`.

### Step 6 — The Ledger of Calls

Every session, after the collector finishes, write a **call**: date,
fingerprint hash, twins and clusters, predicted quantiles per symbol and
horizon, evidence label, baseline scores, pre-registration hash, code commit,
dataset fingerprint and a lockfile hash of the Python environment.

- Append it to a hash-chained `almanac_calls` table (each row carries the
  previous row's hash, as the paper ledger does). No update or delete path may
  exist; add an `ast` test in the style of `TestNoLiveOrderPath` that fails if
  one is introduced.
- When a horizon matures, score it automatically with Step 5's rules. Misses
  stay forever.

### Step 7 — Proof it was written before the outcome

A git commit alone is not proof: history can be rewritten and force-pushed.

- Write the daily chain head to `reports/ALMANAC_CHAIN.md` in a **signed**
  commit (the repo already has a commit-signing key).
- Anchor the same hash with an RFC 3161 timestamp authority and/or
  OpenTimestamps, storing the receipt beside the hash. This network call lives
  in its own module under `signalforge/almanac/anchor/` and must never be
  imported by `signalforge/paper/**`; extend the existing import scan to prove
  it.
- `python -m signalforge.almanac verify` walks the whole chain, recomputes every
  hash, and checks every receipt. A broken link exits non-zero and names the row.

### Step 8 — Replay and self-retirement

- `python -m signalforge.almanac replay --call <id>` checks out the call's
  commit, rebuilds from the recorded dataset fingerprint, and must reproduce the
  call byte-for-byte. A test replays the first call in CI.
- **Pre-declared failure.** The pre-registration states, e.g.: "after 60
  matured 63-session calls, if the skill score vs. climatology is ≤ 0 with 90%
  bootstrap confidence, the Almanac is retired." When that triggers, the
  dashboard headline changes to **RETIRED — did not beat climatology**, new
  calls stop, and the full record stays published. Nothing re-enables it
  except a new pre-registered experiment.

### Step 9 — Dashboard section

Add an "Almanac" tab to the static dashboard (inline SVG, no JavaScript):

- the verdict line first: skill score vs. climatology, `n_effective`, evidence
  label, or "No signal" / "RETIRED";
- today's fingerprint as a heat strip by family;
- the twin clusters on a 1972–present timeline with episode labels;
- fan charts of forward outcomes, index proxies first, against all three
  baselines;
- reliability diagram and sharpness for every matured call, misses included;
- the chain head, signature status and timestamp receipt for today's call;
- vintage, release-lag, survivorship and licensing caveats above every chart.

## Hard constraints

- **Licensing:** Tiingo bars may not be redistributed. The Almanac, its D1
  mirror and its dashboard stay private. A public version may show only
  FRED-derived fingerprints, twin dates, episode labels, aggregate skill scores
  and chain hashes/receipts — never per-symbol prices or returns. Write a test
  that fails if the public renderer receives a `bars`-derived row.
- No pandas (blocked on the operator's machine). DuckDB, Polars, NumPy only.
- Every number in a report is reproducible from a command written next to it.

## Done means

- All existing tests pass; new tests cover: release-lag visibility, family
  weighting, point-in-time PCA, 252-session separation, clustering and
  `n_effective`, all three baselines, CRPS skill, BH adjustment, TAINTED
  exclusion, the append-only ledger, chain verification, replay, the
  retirement trigger, the anchor-module import guard, and the public-renderer
  licence guard.
- `reports/ALMANAC_RESULTS.md` states first, in one sentence, whether the
  Twin Finder beat climatology on the walk-forward — and if it did not, says so.
- One call is in the ledger, its chain head is in a signed commit, and its
  timestamp receipt verifies.
