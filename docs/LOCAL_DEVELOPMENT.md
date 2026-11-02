# SIGNALFORGE - Local Development

Everything here was run on this machine. Where a command prints a figure, the
figure shown is the one it printed.

Local only. Nothing in this repository deploys, pushes, or reaches a
brokerage, and the absence of a live-order path is structural rather than a
setting. See `reports/BUILD_STATUS.md` section Safety posture.

## 0. What you need

- **Python 3.12.** The package declares `requires-python = ">=3.12"`.
- **No pandas.** It is blocked by a Windows Application Control policy on this
  machine; `import pandas` raises ImportError. The stack is duckdb, polars,
  pyarrow, numpy, pydantic, pytest and hypothesis. Streamlit depends on pandas
  and is therefore also unusable, which is why the dashboard is generated HTML
  (see `docs/DECISIONS.md`).
- **No API keys.** Everything that runs today runs from files already in
  `data/archive/` plus one keyless FRED endpoint.

```bash
cd ~/Repos/signalforge
python -m pip install -e ".[dev]"
```

## 1. Run the tests

```bash
python -m pytest -o addopts=""
# 1161 passed, 1 skipped
```

`-o addopts=""` drops the `-q --strict-markers` default from
`pyproject.toml` so you get the count and the progress bar. The suite is
hermetic: no network, no wall-clock dependence, no unseeded RNG.

The single skip is the live FRED call. To include it:

```bash
SIGNALFORGE_NETWORK_TESTS=1 python -m pytest -o addopts="" -m network
```

Tests that read `data/archive/**` skip themselves when the file is absent,
which is the case in any clone, because that directory holds licensed data and
is gitignored. If you see skips beyond the one network test, that is why.

Useful subsets:

```bash
python -m pytest -o addopts="" tests/test_paper_broker.py -k NoLiveOrderPath
python -m pytest -o addopts="" tests/test_risk_properties.py -s   # prints the branch census
python -m pytest -o addopts="" tests/test_diagnostics_verification.py  # ~95s, real data
```

## 2. Build the warehouse

There is no ingest CLI. Ingestion is three lines, and it is idempotent: run it
twice and nothing changes but the run log.

```bash
python -c "
from signalforge.data.archive import ingest_archive
from signalforge.data.storage import Warehouse
w = Warehouse()
r = ingest_archive(w)
print('accepted', r.rows_accepted, 'quarantined', r.rows_quarantined)
print(w.table_counts())
w.close()
"
```

Observed:

```
accepted 85493 quarantined 1
{'raw_records': 85494, 'bars': 26524, 'quarantine': 1, 'series': 58969,
 'corporate_actions': 0, 'ingest_runs': 1, 'data_revisions': 0,
 'quality_flags': 58}
```

The one quarantined row is CGCF 2026-10-22, whose open (9.82) is above its
high (9.81). It is stored unrepaired with its raw payload rather than fixed,
because inventing a price is worse than refusing one. The 58 flags are
`suspicious_price_jump` on micro-cap and penny names; they are stored as
SUSPECT with the prior close in the detail, not quarantined, because real and
bad cannot be told apart from the data alone. See `reports/OPEN_FINDINGS.md`
F-09 and F-10.

The warehouse lands at `data/warehouse/signalforge.duckdb` and is gitignored.
**DuckDB takes an exclusive lock**: a second process trying to open the same
file fails with `Cannot open file ... used by another process`. If a command
fails that way, something else (a dashboard server, another shell) still has
it open.

What is actually in there:

| Series | Observations | Range |
|---|---:|---|
| NASDAQCOM | 14,048 | 1971-02-05 to 2026-10-23 |
| VIXCLS | 9,302 | 1990-01-02 to 2026-10-23 |
| DGS10 | 16,188 | 1962-01-02 to 2026-10-23 |
| T10Y2Y | 12,597 | 1976-06-01 to 2026-10-26 |
| SP500 | 2,511 | 2016-10-27 to 2026-10-23 (10-year licence cap) |
| DJIA | 2,512 | 2016-10-27 to 2026-10-26 |
| UNRATE | 944 | 1948-01-01 to 2026-09-01 |
| FEDFUNDS | 867 | 1954-07-01 to 2026-09-01 |

| Bars | Symbols | Days | Range |
|---|---:|---:|---|
| `plotly_aapl_sample` | 1 | 506 | 2015-02-17 to 2017-02-16 |
| `marketparquet` | 6,799 | 4 | 2026-10-20 to 2026-10-23 |

## 3. Run the research

```bash
python -m signalforge.research_cli
```

Runs the five baselines over the **train and validation** windows, records
each one in the experiment registry, and writes the dashboard. It does **not**
touch the test window.

```bash
python -m signalforge.research_cli --run-holdout
```

Also evaluates the held-out test window. **This window has already been
spent.** The registry enforces one evaluation per (strategy, parameters,
dataset fingerprint), refuses a second, and logs the attempt as a violation.
That refusal has been observed in practice; do not route around it. A hold-out
looked at twice is a tuning set.

Other options: `--db PATH`, `--out PATH`, `--series ID`, `--no-dashboard`.

```bash
python -m signalforge.allocation_cli
```

Researches the multi-asset allocation book in `config/allocations/assets10.yaml`
(decision 19): ten asset-class ETFs held equally and never sold, against the
same ten held only while each one's own 12-month return is positive. Train and
validation only, both rows (as traded and strategy only), recorded in the
registry. The allocation's 10% single-name cap applies to this run alone; it
refuses `--run-holdout`. Options: `--db PATH`, `--allocation-file PATH`.

## 4. Run paper trading

Nothing is approved for paper trading, so with no arguments it refuses:

```
$ python -m signalforge.paper_cli
REFUSED TO RUN

no strategy is approved for paper trading, so the runner will not choose one.
Record an approved promotion with record_active_strategy(...), or pass an
explicit override_strategy with an override_reason. A loop that picks its own
strategy is the same failure as a strategy promoting itself.
```

That refusal is the design, not a missing feature. To approve a strategy,
which requires a named human and a reason, and then drive it:

```bash
python -m signalforge.paper_cli \
  --approve ma_trend --approved-by "the owner" --reason "walk-forward passed"
python -m signalforge.paper_cli --through 2026-10-23
```

To run an unapproved strategy you must say so on the record:

```bash
python -m signalforge.paper_cli \
  --override-strategy ma_trend \
  --override-reason "exercising the paper path on real bars; not an approval"
```

The override text is written to the ledger, to the warehouse, and into the
run's `data_limitations`, which means it reaches the rendered page. An
override without a reason is refused.

The runner **resumes without replaying**. Completed sessions are persisted, so
a restart over the same range does nothing and cannot double-count a fill.
Extending the range on the same runner preserves its book. A newly constructed
runner refuses to extend persisted sessions until verified book/ledger
restoration exists; it must not silently resume with fresh starting cash. A
resumed run with no due sessions returns `result=None` rather than an empty
equity curve, because zero points through `compute_metrics` renders a page
saying 0.00% instead of one saying nothing happened.

To get the run onto the dashboard:

```python
run = runner.run(history, through=history.trading_days[-1])
html = render_dashboard(warehouse=w, paper_runs=[run.result], config=bundle)
```

`run.result` is a `BacktestResult` built from the broker's own end-of-day
statements, so the page cannot disagree with the statements behind it.

## 5. Read the dashboard

```bash
python -m signalforge.dashboard                 # writes dashboard/build/index.html
python -m signalforge.dashboard --serve         # serves it on 127.0.0.1:8765
```

`--host` and `--port` are available. The server is for one reader on one
machine; it is not exposed and is not meant to be.

Six sections: Overview, Strategies, Data health, Learning lab, Risk, Ledger.

**Read the provenance badge on every figure before the figure.** Three
classes, deliberately distinct to the eye as well as in words:

| Badge | Border | Means |
|---|---|---|
| `HISTORICAL BACKTEST` | solid, accent colour | a strategy replayed over past data. Not a record of trading. |
| `PAPER SIMULATION` | dashed, warning colour | a forward simulation against a virtual book. Still not real money. |
| `DATA UNAVAILABLE` | dotted, hatched, muted | this figure could not be computed, and the page says why. |

The third one matters as much as the others. An omitted section makes "no
problem" indistinguishable from "not checked", so the page states an empty
result rather than hiding it.

Other things worth knowing when reading it:

- Every stated count links to the rows it counts.
- Tables cap at 250 rows and say so, with the true total. A phone will not
  load a 12 MB page.
- Charts downsample and say by how much.
- `INSUFFICIENT_EVIDENCE` is not a soft warning. It means the figure beside it
  should not be read as performance. Most figures in this project carry it.
- The `capDep` (capital deployed) column is the number that makes the others
  readable. A 5% single-name cap on a one-symbol universe leaves ~95% of the
  book in cash, so a portfolio CAGR describes a small slice diluted by cash.

## 6. Where things are

```
config/            research.yaml, risk.yaml, data_sources.yaml (names of env
                   vars only, never values; refused sources kept with reasons)
data/archive/      licensed source files. GITIGNORED. Never stage these.
data/warehouse/    the DuckDB file. Gitignored, rebuildable from archive.
signalforge/core/  types, point-in-time guard, interfaces, errors
signalforge/data/  providers, validation (11 rules), warehouse, ingestion
signalforge/backtesting/  engine, fill model, metrics, reconciliation
signalforge/risk/  the independent gatekeeper: engine, limits, breakers,
                   martingale detection, session state
signalforge/strategies/   four baselines plus the benchmark
signalforge/learning/     registry, walk-forward, overfitting diagnostics,
                          promotion gate
signalforge/paper/        broker, ledger, statements, runner
signalforge/dashboard/    generated HTML, charts, sections, server
docs/              ARCHITECTURE.md (why each seam is where it is),
                   DECISIONS.md (the non-obvious calls), this file
reports/           BUILD_STATUS, OPEN_FINDINGS, RISK_GAUNTLET,
                   STRATEGY_RESULTS, DATA_COVERAGE, NEXT_STEPS, MORNING_BRIEF
```

## 7. House rules

- **TDD, no exceptions.** Write the failing test, watch it fail for the right
  reason, then write the code. Every defect in `reports/RISK_GAUNTLET.md` was
  fixed that way.
- **Never loosen a guard, cap, threshold or tolerance** to make a number look
  better or a test pass. If a guard blocks you, that is information. A test
  that needed the 5% position cap raised is a test that was asking the wrong
  question.
- **Never spend the test window again.** The registry will refuse; do not
  route around the refusal.
- **Never stage `data/archive/**`.** It is licensed data under a
  no-redistribution term.
- **Every number in a report must come from output someone actually saw.** Not
  estimated, not rounded from memory, not carried forward from an earlier
  report without re-running it. The one time that discipline slipped, zscore's
  diagnostics drifted and nobody noticed until they were re-run; see
  `tests/test_diagnostics_verification.py`.
