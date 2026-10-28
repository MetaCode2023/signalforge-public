# SIGNALFORGE — Build Status

Generated 2026-10-27. Branch `feature/signalforge-quant-research`.
Operating mode: **PAPER ONLY**. No brokerage credentials exist on this machine.

Every number below was observed in real command output and is reproducible
with the commands given. Where a figure came from a delegated agent and I did
not re-run it myself, it says so.

## Test suite

```
$ python -m pytest -o addopts=""
1161 passed, 1 skipped in 468.63s
```

The single skip is the live FRED network test, gated behind
`SIGNALFORGE_NETWORK_TESTS=1` so the suite stays hermetic by default.

| Test file | Tests | Covers |
|---|---:|---|
| `test_strategies.py` | 64 | 5 strategies, look-ahead immunity, determinism |
| `test_gauntlet_round6.py` | 60 | round 6: penny-price caps, unknown symbols, hostile boundaries |
| `test_overfitting.py` | 56 | sensitivity, cost, Monte Carlo, bootstrap, deflated Sharpe |
| `test_risk_engine.py` | 51 | every risk rule, breaker, martingale control, reconciliation |
| `test_engine_broker_agreement.py` | 45 | the two execution paths agree byte for byte |
| `test_dashboard.py` | 45 | six sections, escaping, count contract |
| `test_real_ohlcv_studies.py` | 44 | dividends, limit fills on real range, the limit-order gap |
| `test_metrics.py` | 42 | all performance metrics, evidence gating |
| `test_paper_broker.py` | 38 | paper broker, ledger, statements, **no-live-path proof** |
| `test_core_types.py` | 36 | Decimal accounting, position and portfolio invariants |
| `test_promotion.py` | 32 | the promotion gate |
| `test_engine.py` | 32 | event loop, accounting identity, reproducibility |
| `test_learning_registry.py` | 29 | experiment registry, hold-out enforcement |
| `test_data_providers.py` | 29 | provider parsing, secret handling, rate-limit notices |
| `test_config.py` | 27 | config validation, split disjointness, PAPER_ONLY not tunable |
| `test_data_storage.py` | 26 | warehouse upserts, revisions, coverage, fingerprints |
| `test_walkforward.py` | 25 | fold construction, consistency |
| `test_execution.py` | 25 | fill model, slippage, gaps, limits, partial fills |
| `test_data_validation.py` | 25 | the 11 validation rules |
| `test_point_in_time.py` | 24 | the look-ahead guard |
| `test_risk_properties.py` | 23 | hypothesis properties for the risk engine, with the teeth proof |
| `test_paper_runner.py` | 22 | approval refusal, resume without replay |
| `test_paper_dashboard.py` | 22 | a real paper run rendered on the real page |
| `test_gauntlet_round3.py` | 21 | round 3 |
| `test_diagnostics_verification.py` | 19 | the delegated diagnostics, re-run on real data |
| `test_review_medium.py` | 18 | round 5 MEDIUM findings |
| `test_review_low.py` | 17 | round 5 LOW findings |
| `test_incremental_validation.py` | 17 | batch-boundary continuity |
| `test_data_archive.py` | 16 | archive ingestion, never touches the network |
| `test_research_cli.py` | 15 | hold-out gating, dashboard wiring |
| `test_registry_atomicity.py` | 15 | record() atomicity, and the refusal log that must survive it |
| `test_chart_downsampling.py` | 15 | page size without lying |
| `test_table_capping.py` | 14 | table caps that state the true total |
| `test_dashboard_new_sections.py` | 14 | regime and selection-bias panels |
| `test_review_paper_defences.py` | 13 | round 5 paper-broker findings |
| `test_regimes.py` | 13 | point-in-time regime labelling |
| `test_gauntlet_round1.py` | 13 | round 1 |
| `test_accounting_properties.py` | 13 | hypothesis properties for the portfolio |
| `test_review_reservations.py` | 12 | order-id reservations |
| `test_cross_section_scale.py` | 12 | 6,799 symbols, and a 126-day lookback over 4 days |
| `test_charts.py` | 12 | inline SVG generation |
| `test_family_wise.py` | 11 | the family-wise correction denominator |
| `test_gauntlet_round2.py` | 10 | round 2 |
| `test_unknown_volume.py` | 9 | instruments with no published volume |
| `test_review_martingale_pinning.py` | 9 | risk reductions no longer pin a strategy's size |
| `test_review_holdout.py` | 9 | the hold-out guard showing figures it refused to record |
| `test_schema_migration.py` | 8 | additive-only migration |
| `test_gauntlet_round4.py` | 8 | round 4 |
| `test_benchmark_window.py` | 7 | benchmark window regression |
| **Total** | **1162** | 1161 pass, 1 network test skipped by default |

## Component status

| Component | Status | Evidence |
|---|---|---|
| Core domain types (Decimal money, integer shares) | **Tested** | 36 tests; equity identity exact |
| Point-in-time look-ahead guard | **Tested** | 24 tests; strategies cannot reach a future bar |
| Validated configuration | **Tested** | 27 tests; `paper_only` cannot be set false |
| Data warehouse (DuckDB, raw/clean/quarantine separation) | **Tested** | 85,494 raw rows ingested; re-ingest changes nothing but the run log |
| Data validation (11 rules) | **Tested** | fired on real data: 58 suspect, 1 quarantined |
| FRED provider (keyless, live) | **Tested** | parsing unit-tested offline; live call network-gated |
| Local archive provider | **Tested** | asserted never to touch the network |
| Tiingo / Alpha Vantage providers | **Partially implemented** | written and fixture-tested; **BLOCKED**, no API key exists |
| Backtest engine | **Tested** | 32 tests; fill strictly after signal bar |
| Fill model (slippage, gaps, partials, participation) | **Tested** | 25 tests |
| Risk engine (independent of strategy) | **Tested** | 51 example tests plus 23 hypothesis properties over arbitrary order sequences; no hook a strategy could use; 3 mutants prove the properties have teeth |
| Martingale detection | **Tested** | averaging down, pyramiding, size-up after loss, doubling, sustained decline |
| Circuit breakers and kill switches | **Tested** | breakers never block an exit |
| Paper broker | **Tested** | 38 tests incl. structural no-live-path proof, mutation-checked |
| Immutable trade ledger | **Tested** | hash-chained; tampering detectable |
| Four baseline strategies + benchmark | **Tested** | 64 tests; all five proven scale-invariant |
| Performance metrics | **Tested** | 42 tests; `None` rather than a misleading 0 |
| Experiment registry | **Tested** | content-addressed ids; hold-out enforced in code; `record()` now atomic, with the refusal log deliberately outside the transaction |
| Walk-forward evaluation | **Tested** | 38 real folds on NASDAQCOM, rolling and anchored; the two are identical out of sample because nothing refits per fold (F-22) |
| Overfitting diagnostics | **Tested** | 56 tests plus 19 re-verifying them on real data; Monte Carlo seed-reproducible; `bootstrap_metric` now run on real returns |
| Promotion gate | **Tested** | 32 tests; nothing can promote itself |
| Operator dashboard (6 sections) | **Tested** | 71 tests; real page renders at 3.69 MB from the research CLI and 1,139,712 bytes from a paper run plus a backtest over the AAPL sample; 87 count links, 0 contract violations |
| Paper-trading runner + CLI | **Tested** | 44 tests; refuses an unapproved strategy, resumes without replaying, and now produces a `BacktestResult` the dashboard renders |
| Engine/broker agreement | **Tested** | 45 assertions; two independent execution paths produce byte-identical fills |
| Per-regime performance | **Tested** | point-in-time VIXCLS terciles, 9,050 labelled sessions, rendered on the page |
| Selection-bias correction | **Tested** | family-wise; measured on the real registry |
| Limit orders on real range | **Tested** | all three fill branches exercised over 505 real bar transitions at 11 offsets; a fill never trades through its limit |
| Dividend / total-return study | **Tested** | 8 quarterly adjustments measured on the only real adjusted series; 2.042766 points a year |
| Reports | **Implemented** | this directory |
| Local development + decisions docs | **Written** | `docs/LOCAL_DEVELOPMENT.md`, `docs/DECISIONS.md` |
| Cloudflare migration | **Not started** | explicitly out of scope for the first build |
| Live brokerage integration | **Not started, and must stay that way** | no code path exists; see §Safety |

## Safety posture

There is no live-order code path, and the absence is structural rather than a
setting:

- `tests/test_paper_broker.py::TestNoLiveOrderPath` parses the shipped package
  with `ast` and fails if any module on the execution path (`paper`, `risk`,
  `backtesting`, `strategies`, `core`, `config`) imports a network library,
  if `signalforge/paper/**` reaches the data layer (which may use the
  network), or if live-brokerage vocabulary appears anywhere in the package.
- A complete paper session runs with `socket.socket` and
  `socket.create_connection` rigged to raise.
- `PaperBroker.is_paper` is a property with no setter.
- `ResearchConfig.paper_only` raises if a config file sets it false.

Both structural guards were **mutation-tested**: adding `import requests` to
`broker.py` failed the import scan, and adding a `place_order` method failed
two tests. Both mutations were reverted and the suite re-confirmed green.

No Schwab credentials exist in `Scripts/.env` (41 keys checked by name only;
none are brokerage credentials), so there was nothing to misuse.

## Reproducing this build

```bash
cd ~/Repos/signalforge
python -m pytest -o addopts=""                 # 1161 passed, 1 skipped
# rebuild the warehouse from data/archive (see docs/LOCAL_DEVELOPMENT.md)
python -c "from signalforge.data.archive import ingest_archive; from signalforge.data.storage import Warehouse; w=Warehouse(); print(ingest_archive(w)); w.close()"
python -m signalforge.research_cli             # train + validation, writes the dashboard
python -m signalforge.research_cli --run-holdout   # ALSO consumes the test window, once
python -m signalforge.dashboard --serve        # serve the page on 127.0.0.1:8765
```

## What is NOT done

- **No per-symbol OHLCV history.** The 5–10 years × 100–500 stocks target in
  the brief is not reachable from any keyless, terms-clean source. See
  `DATA_COVERAGE.md`. This is the single largest gap and it blocks the
  cross-sectional half of the research programme.
- **Tiingo / Alpha Vantage ingestion has never executed.** Their adapters run
  against hand-written fixtures only; the real response shapes are unverified.
- **No live paper-trading loop is scheduled.** `PaperRunner` drives it and is
  resumable, but nothing runs it on a timer yet; it is invoked by hand via
  `python -m signalforge.paper_cli`.
- **Dividends and splits are not applied to open positions.** Declared on
  every run rather than discovered later from a wrong P&L, and now quantified:
  on the only real adjusted series in the project the dividend is worth
  2.042766 percentage points a year, and the engine earns none of it because
  it fills and marks at `close`. See `STRATEGY_RESULTS.md`.
- **No strategy can emit a limit order.** `Signal` has no limit-price field
  and both drivers hardcode `OrderType.MARKET`. The limit model is tested and
  now proven on real intraday range, but no backtest can produce one.
  `OPEN_FINDINGS.md` F-21.
- **The walk-forward does not refit per fold**, so anchored and rolling are
  identical out of sample and the table is a stability measure on fixed
  parameters rather than evidence about re-selected ones. F-22.
- **The promotion criteria thresholds are a delegated agent's choices**, not
  Austin's. See `OPEN_FINDINGS.md` F-12.
