# SIGNALFORGE

> **Research concluded 2026-10-29: frozen.** No strategy beat buy-and-hold on
> walk-forward or the one-time held-out test, and the outside evidence says a
> solo, data-heavy bot is very unlikely to. The "beat the market" goal is
> stopped. The platform stays paper-only, green and reproducible as a record.
> Read [`reports/FINAL_VERDICT.md`](reports/FINAL_VERDICT.md).

A quantitative research and paper-trading platform for U.S. equities.

**PAPER ONLY.** There is no live-order code path in this package, and the
absence is structural rather than a setting — see [Safety](#safety).

## What it is

A system for discovering, testing, rejecting and improving trading strategies
without exposing real capital. It is built so that a negative result is a
first-class outcome: the first full run concluded that none of its four
baseline strategies beat buy-and-hold, and the reports say so.

## Status

```
1161 tests passing, 1 skipped (network-gated)
```

Read [`reports/BUILD_STATUS.md`](reports/BUILD_STATUS.md) for component-level
status, and [`reports/STRATEGY_RESULTS.md`](reports/STRATEGY_RESULTS.md) for
what the research actually found.

## Requirements

Python 3.12+. Install with:

```bash
python -m pip install duckdb polars pyarrow numpy pydantic PyYAML requests
python -m pip install pytest hypothesis          # for the test suite
```

**This project does not use `pandas`**, and not by preference: on the machine
it was built for, `import pandas` raises
`ImportError: DLL load failed ... blocked this file` under a Windows
Application Control policy. Streamlit depends on pandas and is therefore also
unusable, which is why the dashboard is generated static HTML rather than a
Streamlit app. DuckDB, Polars, PyArrow and NumPy cover the same ground.

## Quick start

```bash
# 1. Build the warehouse from the licensed local archive in data/archive/
python -c "from signalforge.data.archive import ingest_archive; \
           from signalforge.data.storage import Warehouse; \
           from datetime import date; \
           print(ingest_archive(Warehouse('data/warehouse/signalforge.duckdb'), \
                                as_of=date.today()))"

# 2. Run the research. Writes dashboard/build/index.html from the live results.
python -m signalforge.research_cli

# 3. Open the dashboard
python -m signalforge.dashboard --serve     # 127.0.0.1:8765

# 4. The held-out test window. This can honestly be done ONCE.
python -m signalforge.research_cli --run-holdout
```

Step 4 is gated deliberately. Before the test window runs at all, the
registry is asked whether this strategy and parameter set has already spent
its one look at the dataset. If it has, **the window is not run and the
figures are not shown** — a caveat printed beside the numbers does not undo
having seen them — and the blocked attempt is logged. A held-out period
looked at twice is a tuning set.

## Layout

```
signalforge/
├── config/       typed, validated configuration (paper_only is not tunable)
├── core/         domain types, the point-in-time look-ahead guard, errors
├── data/         providers, 11-rule validation, DuckDB warehouse, ingestion
├── strategies/   4 baselines + a buy-and-hold benchmark
├── backtesting/  event loop, fill model, performance metrics
├── risk/         limits, breakers, martingale detection — independent of strategy
├── paper/        the only venue: a simulated broker, hash-chained ledger
├── learning/     experiment registry, walk-forward, overfitting, promotion gate
├── dashboard/    generated static HTML, inline SVG charts, no JavaScript
└── pipeline.py   warehouse -> runnable histories -> the baseline suite
config/           research.yaml, risk.yaml, data_sources.yaml
docs/             architecture, decisions, local development
reports/          build status, data coverage, results, gauntlet, findings
data/archive/     licensed source data (gitignored, never redistributed)
```

[`docs/LOCAL_DEVELOPMENT.md`](docs/LOCAL_DEVELOPMENT.md) is how to run every
part of it. [`docs/DECISIONS.md`](docs/DECISIONS.md) is why the non-obvious
calls went the way they did. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is
why each seam is where it is.

## Design decisions worth knowing

**Money is Decimal, shares are integers.** The invariant
`equity == cash + marked positions` has to hold exactly, and binary floats
cannot promise that.

**A strategy is handed a `PointInTimeView`, never a series.** The view
physically cannot return a bar dated after its `as_of`, so a strategy cannot
read tomorrow's price even if its author tries.

**A signal from today's close cannot fill today.** `FillModel` raises
`LookAheadError` rather than returning a plausible number if asked to fill on
the signal bar.

**Slippage always works against the order.** A favourable gap earns no
discount; luck is not modelled as a bonus.

**The risk engine has no hook a strategy could use.** `evaluate()` takes an
order, a portfolio and a market view. Strategies propose; the engine decides.

**Breakers never block an exit.** A system that cannot reduce an existing
position is more dangerous than one that keeps trading, so the asymmetry is
deliberate.

**Unknown is not zero.** An index series publishes no volume. Rather than
invent one, `Bar.volume_known` is False, the liquidity breaker and
participation cap are *skipped* rather than passed, and the run says so. A
genuine zero-volume print is still refused.

**Nothing silently repairs data.** A record that fails validation is
quarantined with its raw payload and an audit trail. Suspect records are
flagged with the prior close so a human can judge them.

**Nothing can promote itself.** The promotion gate takes metrics and a named
human approver, never a strategy object, and refuses anything flagged
`INSUFFICIENT_EVIDENCE` however good its numbers look. The same reasoning one
layer out: the paper runner refuses to pick a strategy nobody approved, and an
override has to carry a reason that is then written to the ledger, the
warehouse and the rendered page.

**A misbehaving strategy aborts the run.** Catching a strategy exception and
carrying on would produce a complete equity curve and a full set of metrics
with one session of decisions silently missing, which is a partial backtest
presented as a finished one. The abort names the strategy, the date and the
window.

**A cap is never widened to make a number look better.** Gauntlet round 6
found a quantity cap that took 442 of a run's 443 seconds at penny prices; it
was made exact rather than loosened, and the output is byte-identical.

## Safety

The paper engine cannot reach a brokerage, and the argument is not a flag:

- `tests/test_paper_broker.py::TestNoLiveOrderPath` parses the shipped package
  with `ast` and fails if any module on the execution path imports a network
  library, if `signalforge/paper/**` reaches the data layer (which may use the
  network), or if live-brokerage vocabulary appears anywhere.
- A full paper session runs with `socket.socket` and
  `socket.create_connection` rigged to raise.
- `PaperBroker.is_paper` is a property with no setter.
- `ResearchConfig.paper_only` raises if a config file sets it false.

Both structural guards were mutation-tested: adding `import requests` to
`broker.py` fails the import scan, and adding a `place_order` method fails two
tests.

## Data and licensing

`data/archive/**` is gitignored. It holds data acquired by the operator whose
licence permits internal research and forbids redistribution, so it is never
committed. [`config/data_sources.yaml`](config/data_sources.yaml) records every
source investigated — including the four refused, with the reason — so those
decisions are not re-litigated from memory later.

[`reports/DATA_COVERAGE.md`](reports/DATA_COVERAGE.md) states plainly what the
data does and does not support. The short version: there is no per-symbol
OHLCV history available from any keyless, terms-clean source, so the current
results are a single-instrument proxy study.

A private D1 mirror of the market-data tables (not for public access) is described in [`docs/D1_MARKET_MIRROR.md`](docs/D1_MARKET_MIRROR.md).
