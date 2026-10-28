# SIGNALFORGE — Architecture

## The shape of it

```
                 data/archive (licensed, local, gitignored)
                 FRED (keyless, documented)
                          |
                    data/providers
                          |
                 data/validation  — 11 rules, never repairs
                    /           \
            raw_records      quarantine + quality_flags
                    \           /
                   data/storage (DuckDB)
                          |
                    pipeline.py  — warehouse -> PriceHistory
                          |
                 core/history.PointInTimeView   <-- the look-ahead guard
                          |
                    strategies  — propose Signals, size nothing
                          |
                  backtesting/engine
                     |          \
             risk/engine     backtesting/execution
            (decides)         (fills, strictly later bar)
                     \          /
                    core/types.Portfolio  — Decimal, exact identity
                          |
          backtesting/metrics  ->  learning/*  ->  promotion gate
                          |
                      dashboard + reports
```

The only venue is `paper/broker.PaperBroker`. There is no second one.

## The seams, and why they are where they are

### Strategy / risk
A strategy emits `Signal(symbol, direction, target_weight, reason)` and knows
nothing about cash, position size, limits or whether it is allowed to act. The
risk engine receives an `Order`, a `Portfolio` and a `PointInTimeView` and
decides. There is no callback, no back-reference and no shared mutable state
between them.

This is what makes "the risk engine is independent of strategy logic" a
property of the code rather than a claim. The test
`test_engine_needs_no_strategy_object_to_decide` is the proof: `evaluate()`'s
signature has nowhere to put a strategy.

### Data / execution
`signalforge/paper/**` must not import `signalforge.data`. The data layer is
allowed to use the network; the execution path is not. Keeping them apart stops
the execution path from borrowing network access transitively. A test enforces
it.

### Point in time
`PriceHistory` holds every bar. The only way to read it during a run is
`history.view(as_of)`, which returns a frozen `PointInTimeView` that cannot
return a bar dated after `as_of` — it raises `LookAheadError`. Strategies are
handed the view, never the history.

This is a structural defence rather than a convention: a strategy author
cannot read the future by mistake, because the future is not reachable through
the object they were given.

## Ordering inside one trading day

The event loop runs, per day, in exactly this order:

1. **Open the session**, valuing the book at the *previous* close.
2. **Settle orders that became due today**, against today's bar.
3. **Mark the book at today's close**, updating peak and drawdown.
4. **Ask the strategy for signals**, given a view truncated at today.
5. **Size, risk-check and queue** what survives, for a later bar.
6. **Record one equity point.**

Step 4 after step 3 and before step 5 is the whole design. A signal is formed
from information available at today's close and can only be acted on later.

## Why Decimal, and where floats are allowed

Cash, prices, P&L and weights are `Decimal`. Share quantities are `int`. The
invariant `equity == cash + sum(quantity * mark)` is asserted exactly in tests,
which binary floats cannot support.

Floats appear in exactly two places, both deliberate and both documented at
the site:
- `risk/limits.risk_budget_cap` computes a standard deviation. Volatility is an
  estimate; the result is converted back to `Decimal` before it touches a share
  count.
- `strategies/mean_reversion` and `volatility` compute a z-score and a realised
  volatility as decision statistics. Weights stay `Decimal`.

## Unknown is not zero

An index series publishes no volume. Two wrong answers were available:

- Invent a volume so the engine runs — which makes the liquidity breaker and
  the participation cap meaningless while they still *look* like controls.
- Let the zero-volume rule block every order — which makes the series unusable.

`Bar.volume_known` is the third answer. When it is False, the two rules that
depend on volume are **skipped**, the run's `data_limitations` says they were
skipped and why, and a genuine zero-volume print is still refused. Nothing is
fabricated and nothing is silently waved through.

## Limitations travel with results

`BacktestResult.data_limitations` is not decoration. The engine populates it
automatically (close-only series, single instrument, unknown volume, no
corporate-action handling, too few sessions for annualised statistics) and the
pipeline adds source-specific notes. Reports read from it, so a number cannot
be quoted without the caveat attached to it.

## Why the dashboard is generated HTML

`pandas` is blocked by a Windows Application Control policy on the target
machine. Streamlit depends on pandas, so it is unusable here. The dashboard is
therefore a single generated HTML file with inline SVG charts and no
JavaScript:

- Every number comes from live program state or a live warehouse query.
- Three provenance classes are visually and textually distinct:
  `HISTORICAL BACKTEST`, `PAPER SIMULATION`, `DATA UNAVAILABLE`.
- Every monetary figure carries a `SIMULATED` marker.
- A missing value renders as "not available" with the reason, never as 0 and
  never as a blank cell. An empty section states that it was checked.
- A stated count links to the rows it counts, and the clickable number equals
  the rows at the other end even when the table is capped.
- Charts downsample to 600 points with min/max-preserving decimation, so a
  one-day drawdown trough is never smoothed away. Stated statistics are always
  computed from the full series.

## Storage model

Four layers, deliberately separate:

| Layer | Table | Rule |
|---|---|---|
| Raw | `raw_records` | Immutable, as-ingested, never updated or repaired |
| Clean | `bars`, `series` | Validated; UNIQUE on `(symbol, trade_date, source)` |
| Rejected | `quarantine` | Failed validation, raw payload preserved, never dropped |
| Suspect | `quality_flags` | Passed but questionable, with the prior close in the detail |
| Audit | `ingest_runs`, `data_revisions` | A changed value records old and new rather than overwriting |

`Warehouse.fingerprint(symbols, start, end)` hashes exactly the rows a run will
consume, including `volume_known`, so an experiment's dataset version changes
whenever anything it acts on changes.

## Promotion

Nothing can promote itself. The gate takes `StrategyEvidence` (metrics) and an
explicit human approver string — never a strategy object, and never the
candidate's own name. Without an approver the best possible outcome is
`PROPOSED`. `apply_promotion` raises unless the proposal is both APPROVED and
fully passing.

The experiment registry enforces hold-out discipline in code: the test window
may be evaluated once per (strategy, parameters, dataset fingerprint). A second
attempt raises and is logged with `violation=TRUE`, because a held-out period
looked at twice is a tuning set.
