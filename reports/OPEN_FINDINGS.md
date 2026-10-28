# SIGNALFORGE — Open Findings

Generated 2026-10-27. Everything unresolved, ranked by what it would cost to
be wrong about it. Nothing here is a crash: the suite is green at **1161
passed, 1 skipped**. These are gaps in what the system can honestly claim.

Resolved defects are in `RISK_GAUNTLET.md`, not repeated here.

## Critical — blocks the research programme, not the code

### F-01 · No per-symbol OHLCV history exists
**Status: BLOCKED on an external decision.**
The brief's target of 5–10 years across 100–500 stocks is unreachable from any
keyless, terms-clean source. Four sources were investigated and refused with
recorded reasons (Stooq proof-of-work, MarketParquet `robots.txt`, undocumented
Yahoo endpoint, Nasdaq Data Link 403/frozen). What exists is 55 years of one
index's closes, 506 days of one stock, and a four-day cross-section.

**Consequence:** every result in `STRATEGY_RESULTS.md` is a single-instrument
proxy study. Cross-sectional work cannot be evaluated at all.

**The one action that unblocks it:** a free Tiingo API key (no payment). The
adapter is written and fixture-tested; ingestion would run immediately. This
creates an account in Austin's name, so it is his call. **Needs Austin.**

### F-02 · Survivorship bias is structural and unfixable with this data
**Status: declared, not fixed.**
The 23-name universe contains only companies that survived to 2026. The
MarketParquet cross-section is a snapshot of symbols listed today, so it holds
no delisted company at all. A backtest over a survivor-only universe overstates
return and understates drawdown.

No permitted source offers delisted securities. Code cannot fix this; only
different data can. Declared on affected runs so it is never silently omitted.

## High — affects what the numbers mean

### F-03 · Portfolio metrics are dominated by idle cash
**Status: mitigated by disclosure, not resolved.**
A 5% single-name cap on a one-symbol universe leaves ~95% of the book in cash,
so portfolio CAGR and Sharpe describe a small slice of a strategy diluted by
cash. Real measured values: buy-and-hold deployed 10.7% average capital on the
test window; MA trend deployed 1.7%.

Added `average_capital_deployed` and `peak_capital_deployed` so the dilution is
visible. **I did not loosen the risk cap to make the numbers look better** —
that would be disabling a safety control to improve a metric.

**Decision needed:** whether single-instrument studies should run under a
config whose single-name cap equals the intended allocation (making the
portfolio figures directly readable), or whether to keep the production cap and
always read the `capDep` column. **Needs Austin.**

### F-04 · Cross-sectional momentum degenerates to buy-and-hold
**Status: open, by design of the spec.**
Ranking the cross-section and holding the top `n` always holds the only symbol
when the universe has one member. There is no absolute-momentum filter, so the
strategy cannot decline to be invested. Its test-window figures are identical
to buy-and-hold's to every decimal.

First flagged by the strategies agent (231 entries into the downtrend fixture),
confirmed on real data.

**Proposed fix:** add a `require_positive_return` parameter so the strategy can
stay flat when no symbol has positive trailing momentum. The spec did not ask
for it, so I have not added it. **Needs Austin's word** before it is used on a
real universe.

### F-05 · Vol-targeted trend is indistinguishable from plain trend here
**Status: open.**
Both report 13.0% / 0.45 Sharpe on train and 1.9% / 0.32 on test. The
volatility-scaled weight is clamped by the 5% single-name cap, so the cap binds
first and the scaling never takes effect. The two are distinguishable in unit
tests but not in this configuration — which means the vol-targeting layer is
**untested as a behaviour** on real data.

Related to F-03: raising the cap for single-instrument studies would also make
this strategy actually do something.

### F-06 · Keyed providers have never executed
**Status: BLOCKED (same blocker as F-01).**
Tiingo and Alpha Vantage adapters run against hand-written fixtures built from
documented response shapes. The real shapes are unverified. Alpha Vantage's
`TIME_SERIES_DAILY_ADJUSTED` may now be premium-only; this could not be checked
without a key.

## Medium — known blind spots

### F-17 · RESOLVED 2026-10-26 · Per-regime performance shipped blank
The brief requires performance by market regime (§12). The metrics layer
supported it from the start, but nothing ever passed it labels, so `regimes`
came back as an empty dict on every real run — a required figure rendering
blank rather than broken, which reads as "no regimes found".

Fixed with point-in-time VIXCLS tercile labelling (252-session expanding
warmup, so a label never changes when later data arrives). 9,050 labelled
sessions now populate, and the result was immediately informative: see the
regime table in `STRATEGY_RESULTS.md`, where trend following's only losing
regime on the test window is mid-volatility whipsaw while buy-and-hold earns
most of its return in high volatility.

Found by auditing which metric fields are actually populated on a real run
rather than assuming the ones with code behind them were being used.

### F-07 · Incremental-ingest blind spot
**Status: open.**
The `suspicious_price_jump`, `stale_price` and `inconsistent_adjustment` rules
see only the current batch. A jump spanning two separate ingests would not be
flagged. Harmless today because all data arrives in one archive ingest; it
becomes real the moment a daily incremental pull starts.

### F-08 · RESOLVED 2026-10-26 · Multiple-testing correction now measured
**Status: resolved, and it strengthened the negative result.**
Previously unquantified; the report said the correction "should be small",
which is not a measurement. Run against the real registry, **nothing in the
project survives it**: 5 variants tried, best out-of-sample Sharpe 0.676
against an expected best-of-five under the null of 0.347, deflating to 0.870
and failing significance. At four or more variants the best result stops being
significant, and five were tried.

Also found while doing it: `multiple_testing_summary` counts variants PER
STRATEGY, so with one parameter set each it reported 1 trial and no
adjustment while five strategies had been tried. Added `family_wise_summary`
for the denominator that actually applies. Both now coerce metric values
from the registry's JSON, which previously raised on a string.

### F-09 · 58 suspect price records are a human judgment, not a verdict
**Status: awaiting review.**
58 MarketParquet bars tripped `suspicious_price_jump` (e.g. AARD 3.95→1.39,
AIOK 4.25→12.02, AONC 8→21, BBIG 0.003→0.0015). They are stored as SUSPECT
with the prior close in the detail, not quarantined. Mostly micro-cap and
penny names where such moves are plausible, so real-versus-bad cannot be
settled from the data alone. **Needs a human call** if these symbols ever
enter a research universe.

### F-10 · One genuine source defect quarantined
**Status: resolved for safety, unresolved at source.**
CGCF 2026-10-22 has `open 9.82` above `high 9.81`. Quarantined unrepaired with
its raw payload. If MarketParquet data is used further, the publisher is worth
telling.

### F-11 · The index study is a proxy, not a tradeable result
**Status: declared on every run.**
An index cannot be bought. A real implementation would trade an ETF and pay
tracking error, expense ratio, spread and creation/redemption friction, none of
which are modelled. Even a positive result here would overstate a tradeable one.

### F-18 · RESOLVED 2026-10-26 · 14 defects found by independent review
A reviewer pointed at the source (not at my description of it) reproduced 14
defects I had missed, five HIGH. All are fixed with a failing test first; see
`RISK_GAUNTLET.md` round 5 for each one with its measured evidence.

Three of the five HIGHs were wrong in the direction that makes the system
*look* like it is working: the hold-out guard showed figures it refused to
record, the paper broker computed every cap against filled positions only,
and a risk reduction permanently pinned a strategy's size while the log
blamed "martingale" for risk's own decision.

### F-19 · RESOLVED 2026-10-26 · Two execution paths were never compared
The backtest engine and the paper broker are independent implementations of
the same rules, each built and tested alone. Nothing compared them, so a
divergence would have been invisible and neither would have indicated which
was wrong. They now agree byte-for-byte on final equity, fill dates, sides,
quantities, prices, positions and costs across 3 strategies x 3 market
shapes; mutation-checked.

### F-20 · Open · The paper runner is not scheduled
See F-15. The driver exists and is resumable; nothing invokes it on a timer.
A scheduled loop should also decide what happens when market data is stale
on a given morning, which the risk engine already refuses to trade on.

### F-21 · Open · No strategy can ask for a limit order
**Status: open, structural, found 2026-10-27.**
`Signal` has no limit-price field, and both drivers construct
`OrderType.MARKET` unconditionally: `BacktestEngine._order_from_signal` and
`PaperRunner._submit_signals`. `PaperBroker.create_order` is the only door
that accepts a limit.

So the limit model is implemented, unit-tested, and now exercised against
real intraday highs and lows across 505 bar transitions at eleven offsets with
all three branches firing, and **no backtest in this project can ever produce
one**. That is a gap in the research surface rather than a bug.

It matters because the measured difference is not small. A limit buy at the
previous close fills 409 of 505 times at a mean of 112.9195 against a market
order's 505 of 505 at 112.9641, and a naive limit round trip over 199 sessions
of the AAPL sample lost 167.934215 with the sells averaging BELOW the buys.
Any conclusion about whether limits help cannot currently be reached from a
backtest.

Three assertions in `tests/test_real_ohlcv_studies.py` pin the gap, and they
are also the list of what would have to change: a limit field on `Signal`, and
one of the two drivers honouring it.

### F-22 · Open · The walk-forward does not refit, so anchored equals rolling
**Status: open, documentation-level, measured 2026-10-27.**
`walk_forward` rebuilds the same fixed `StrategySpec` every fold, and each
fold's out-of-sample run reads only the test window. The training window
therefore reaches `train_metrics` and nothing else.

Measured on all four strategies: anchored (expanding) and rolling produce
**identical** out-of-sample aggregate return, Sharpe, max drawdown, trade
count, fill count and per-fold return. Only `train_start` and
`train_sessions` differ (1,260 fixed against 1,260 expanding to 10,584).
Anchored costs about 4x the wall clock for the same output: 39.6s against
10.4s for ma_trend 50/200, 402.6s against 248.1s for zscore.

Not a bug, but it changes what the table means. The consistency and aggregate
figures are a time-stability measure on fixed parameters, not evidence that
re-selected parameters hold up out of sample, and the report should be read
that way. Pinned in `tests/test_diagnostics_verification.py`, so if per-fold
refitting is ever added the test fails and every claim built on that table has
to be revisited.

### F-23 · Open · The zscore diagnostics in the reports do not reproduce
**Status: open, and the current figures are the test's, not the report's.**
Re-running every delegated diagnostic through the current code reproduced both
ma_trend variants and buy-and-hold to the decimal, and disagreed with zscore
mean reversion in all three:

| figure | reported | re-run 2026-10-27 |
|---|---|---|
| cost sweep, 0 bps | -2.55% | **-2.8768%** |
| cost sweep, 100 bps | -7.90% | **-8.0983%** |
| Monte Carlo closed trades | 65 | **63** |
| Monte Carlo terminal median | 97,333.98 | **96,939.62** |
| walk-forward positive folds | 21/38 | **17/38** |
| walk-forward aggregate return | -3.8% | **-4.0116%** |
| walk-forward trades | 132 | **121** |

Every verdict is unchanged: zscore has no edge at any cost level and loses
money out of sample either way. But those are not the current code's numbers.

zscore is the only strategy that trades often enough for the round 5 risk
fixes (martingale size pinning, order-id reservations, fill-time
reconciliation) to change its order flow, and `STRATEGY_RESULTS.md`'s own
train table already says 63 closed trades where its Monte Carlo row says 65.
The most likely reading is that the zscore diagnostics predate those fixes and
were never re-run.

ma_trend 50/200's walk-forward is now pinned in a test so the next drift fails
rather than surviving into a report. zscore's are not: its rolling
walk-forward takes 248 seconds and its anchored one 403, which is more than
the suite should spend on one row.

### F-24 · Open · `PaperBroker.portfolio` is a method where its neighbours are properties
**Status: open, cosmetic.**
`ledger`, `statements`, `risk_events`, `pending_count`, `starting_cash` and
`orders` are properties; `portfolio()` is a callable. Caught by tripping over
it: `broker.portfolio` returns a bound method that reads as an object until
something asks it for `.fills`. Not changed, because the public API has
callers and the churn is not worth it tonight, but it is a trap worth naming.

## Low — governance and housekeeping

### F-12 · Promotion thresholds are a delegated agent's choices
**Status: needs confirmation. The atomicity half is RESOLVED 2026-10-27.**
`PromotionCriteria` ships with: consistency floor 0.60, minimum 3 folds,
drawdown tolerance 1.25× baseline + 0.01, Sharpe strictly above baseline. These
were chosen by the agent that built the gate, not by Austin. They are
defensible but they are not his. **Needs Austin's sign-off**, because they
determine what would ever be allowed into paper trading.

The separate integrity gap in this area, flagged in `RISK_GAUNTLET.md` round
5, is fixed. `registry.record()` wrote the experiment row and the row that
spends the hold-out as two statements; a failure between them left the
experiment recorded while the hold-out read as unspent, which is exactly the
state that permits a second look at the held-out window. Both writes are now
one transaction, with `guard_test_window` deliberately outside it so a refused
attempt still leaves its violation on the record. See
`tests/test_registry_atomicity.py`, which pins both requirements because
satisfying one by breaking the other is the likely mistake.

### F-13 · RESOLVED 2026-10-26 · Private cross-module import
`closed_trades` is now public in `backtesting/metrics.py`, with the private
name aliased so the existing import keeps working.

### F-14 · Two trade-count definitions coexist
**Status: open, documentation-level.**
`BacktestResult.trade_count` counts fills; `PerformanceMetrics.trade_count`
counts closed round trips (with `fill_count` separate). Both are correct for
their purpose, but a reporting surface reading the wrong one would double-count
round trips. The reports here use the metrics value.

### F-15 · PARTLY RESOLVED 2026-10-26 · Paper runner built, not scheduled
**Status: the driver exists; nothing runs it on a timer.**
`PaperRunner` opens each session, settles, submits signals, closes the books
and persists progress, with a CLI at `python -m signalforge.paper_cli`.

Two safety properties are tested: it **refuses to pick a strategy nobody
approved** (a loop that quietly selects one is a strategy promoting itself,
moved one layer out), and it **resumes without replaying**, so a restart
cannot double-count fills.

Still open: nothing invokes it on a schedule. That is now a deployment
decision rather than missing code.

### F-16 · Dashboard Learning Lab reads the registry schema generically
**Status: works, worth tightening. Now also carries the selection-bias panel.**
The dashboard was built without importing `signalforge.learning` (to avoid a
collision during concurrent work), so it reads `SELECT * FROM experiments` and
infers columns by name. It now renders the 15 real recorded experiments, but a
schema change would degrade it to a "column missing" message rather than an
error.

## Explicitly checked and clean

Recorded because an omitted section makes "no problem" indistinguishable from
"not checked":

- No live-order code path. Structurally proven and mutation-tested.
- No brokerage credentials on this machine (41 `.env` keys inspected by name).
- No `pandas` import anywhere (it is blocked by Windows Application Control).
- No secret value in any source file, test or fixture.
- No licensed source data committed to git (`data/archive/**` is gitignored;
  verified at every commit).
- Nine of eleven validation rules fired zero times on real data:
  duplicate_symbol_date, negative_or_zero_price, negative_volume, zero_volume,
  non_trading_day, future_dated, stale_price, inconsistent_adjustment,
  malformed_record.
- Zero data revisions and zero corporate actions in the warehouse.
- Round 6's eleven hostile conditions, five of which held: a one-bar history,
  `start == end`, a strategy returning None, three identical signals in one
  call, a misdated signal, a price above the whole per-trade budget, a book
  collapsing to 0.045% of its starting equity, a fill costing exactly the cash
  on hand, one cent short of it, and `max_positions == 1` met by two
  simultaneous entries. See `RISK_GAUNTLET.md` round 6 for the six that did
  not hold.
- Exactly-zero equity is unreachable, by design: it would require marking an
  open position at zero, and the book refuses to value an unmarkable holding
  at nothing rather than understate a drawdown. Tested as a refusal.
- The risk engine's seven guarantees hold over arbitrary order sequences
  (approval bounded by the request, the single-name cap, gross leverage,
  available cash, a REJECT carrying zero, exits never blocked by a breaker,
  reservations netting to zero). No defect was found; the census shows 163
  ACCEPT, 202 REDUCE, 742 REJECT and seven distinct binding rules, and three
  mutants confirm the properties can detect a broken engine.
- The adjustment in the AAPL sample is genuine dividends: eight material ratio
  steps over two years, one a quarter, all positive, which is AAPL's dividend
  schedule. The 491 sub-threshold steps are six-decimal rounding in the
  published `adj_close`, not events.
