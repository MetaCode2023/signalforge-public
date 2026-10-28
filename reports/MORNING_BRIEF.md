# SIGNALFORGE - Morning Brief

Overnight session, 2026-10-26 into 2026-10-27. Branch
`feature/signalforge-quant-research`, 8 commits, nothing pushed, nothing
deployed, no remote touched.

**Final test count: 1161 passed, 1 skipped, in 443.56 seconds.** Up from 966
passed, 1 skipped at handoff. The skip is still the one network-gated FRED
test. The tree is clean and every commit was made on a green suite.

Every number in this document came from output I saw in this session. Where a
figure is derived from a measured rate rather than measured directly, it says
so.

---

## What I completed

All nine queue items. In the order they were given:

1. **The cross-section scale run**, which turned out to be the most valuable
   thing in the queue because it exposed the night's worst defect.
2. **Gauntlet round 6**, eleven hostile conditions, six defects.
3. **Property-based tests for the risk engine**, 23 properties, with the
   non-vacuity census and three mutants.
4. **Re-ran every delegated diagnostic.** ma_trend reproduces, zscore does
   not. Plus anchored walk-forward and `bootstrap_metric`, neither previously
   run.
5. **The dividend study** on the AAPL sample.
6. **Limit orders on real OHLCV**, which found a structural gap.
7. **A real paper run rendered on the real dashboard**, which found two more.
8. **`docs/LOCAL_DEVELOPMENT.md` and `docs/DECISIONS.md`**, plus every report
   brought back in sync.
9. **`registry.record` made atomic**, without breaking the thing a naive fix
   would have broken.

You said items 1 to 4 were where the value was and that stopping after 4 was
an acceptable outcome. You were right about where the value was: items 1, 2
and 4 produced every significant finding below, and item 3 produced the
strongest evidence that something is *not* broken. Items 5, 6 and 7 each found
one real thing anyway, so I did not stop.

---

## Every defect found, with its measured evidence

### 1. A quantity cap that searched for its answer one share at a time (HIGH)

The one that matters. `risk.limits.cash_cap` started from
`(cash - min_commission) / price`, an upper bound that ignores the per-share
commission entirely, and then decremented one share at a time until the total
cost fitted.

The number of steps scales as cash/price. Invisible on a 100 stock. At a
0.0001 price with 100,000 of cash it is **980,382,157 decrements**.

Measured on the real MarketParquet cross-section, 6,799 symbols of which 85
trade under a cent and whose minimum close is 0.000001:

```
total wall clock: 443.030s   orders=85 fills=36
site                                calls      total_s        pct
RiskEngine.evaluate                    85      442.624      99.9%
limits.cash_cap                        41      442.094      99.8%
view.marks                             63        0.550       0.1%
view.latest                        428724        0.334       0.1%
strategy.generate_signals               4        0.255       0.1%
```

**442.094 of 443.030 seconds, 99.8%, in 41 calls to one function. 10.8
seconds per call.**

That attribution is from deliberate instrumentation rather than cProfile. I
tried cProfile first and killed it after 43 minutes without output: it
instruments every call, the hot loop calls a helper each iteration, and the
profiler's own overhead on roughly a billion iterations made the run
unfinishable. Worth remembering as a profiling lesson, not just a bug.

The cap it returned was always correct. It just took minutes to say so. Now
solved in closed form by inverting each side of the commission kink, and
cross-checked against a brute-force reference over 2,400 random price/cash
pairs across 6 commission schedules with zero mismatches. The same four-day
run now takes **0.930 seconds** with byte-identical output: 85 orders, 36
fills, 41 risk events, final equity 99,061.157008.

I did not loosen anything to get there. The caps, the thresholds and the
tolerances are all unchanged; the arithmetic is exact and the algorithm is
different.

### 2. The same search, a second time, in `FillModel.affordable_quantity` (MEDIUM)

Found by grepping for the shape of the first one rather than waiting for it to
hurt. The fill-time reconciliation reduces a buy whose cost has risen since it
was sized, and it did so with the same decrementing walk.

Bounded by the order size rather than by cash/price, so it could never run for
minutes. It could run for seconds: **1.556 seconds for one call** reducing a
10,000,000-share position priced at 0.005. And defect 1's fix makes positions
that large legitimately reachable, so this path got hotter rather than cooler.

Both call sites now share one solver, which lives on `ExecutionConfig` beside
the commission schedule it inverts, because neither the risk layer nor the
execution layer owns that schedule and two copies of the same arithmetic had
already drifted into two implementations.

### 3. A signal for a symbol the history has never held vanished (MEDIUM)

No order, no rejection, no risk event, no error. A renamed or mistyped ticker
produced a strategy that did nothing and a run that looked perfectly healthy.
The risk engine has `check_price_available` for exactly this case; the order
never reached it, because `_order_from_signal` found no bar and returned None.

Now refused, naming the symbol, the strategy and what the history does hold. A
symbol that *is* in the history but has no bar yet stays a quiet no-op,
because a late listing is a market fact rather than a programming error, and
that case has its own test.

### 4. A strategy exception aborted the run with no context (MEDIUM)

A 38-fold walk-forward dying inside a fold reported bare `ZeroDivisionError`.

I kept the abort deliberately and documented why. Catching it and carrying on
would produce a complete equity curve, a full set of metrics and a dashboard
page, with one session of the strategy's decisions silently missing. A partial
backtest presented as a finished one is the failure this project exists to
avoid. What changed is that the abort names the strategy, the date and the
window, and keeps the original exception as its cause.

Doing that broke the look-ahead guard's error type, which `test_engine.py`
caught immediately: `LookAheadError` was being re-wrapped as a generic
`ValidationError`, hiding the single most important thing this engine can
detect. It is now exempt from the wrapper, and the exemption is itself pinned.

### 5. A limitation note interpolated all 6,799 tickers (LOW)

`"Only 4 distinct trading days available for ['A', 'AA', 'AAC', ...]"` - a
**52KB string** that travels into `BacktestResult.data_limitations`, from
there into the reports, and from there onto the dashboard page. Now a count
plus eight symbols. The same shape existed in the "no bars in the warehouse"
refusal and is fixed too.

### 6. `registry.record` was not atomic (MEDIUM, F-12 area)

The experiment row and the `test_evaluations` row that spends the hold-out
were two separate statements. A failure between them left the experiment
recorded while the hold-out read as unspent, so the same strategy on the same
data could be evaluated against the held-out window a second time and the
registry would permit it. That is the one thing the registry exists to
prevent.

The naive fix defeats something else, which is the part worth knowing:
`guard_test_window` logs a refused second look as a violation and then raises,
and wrapping the whole call in a transaction would have rolled that log back
on the way out. A blocked attempt would have left no trace. So the guard runs
before the transaction opens and only the two writes are atomic. Both
requirements are tested, because satisfying one by breaking the other is the
likely mistake.

`Warehouse._tx` is now the public `Warehouse.transaction`, with `_tx` kept as
an alias. F-13 was the same shape.

### 7. The paper broker kept no order log (MEDIUM)

Found by wiring item 7. The backtest engine accumulates every order with its
final status; the broker wrote order events to the ledger and held only
unfilled orders in `_pending`, so a submitted, rejected or expired order could
not be recovered as an `Order` at all.

The dashboard's risk section reads `result.orders`. A paper run would have
rendered with an order count of zero beside a non-zero fill count: a page
saying nothing was ordered next to a page saying something filled.

### 8. `RunResult` carried no result (MEDIUM)

The reason the dashboard's PAPER SIMULATION provenance class had never
rendered from an actual paper run, despite having a CSS rule, a legend entry
and a `paper_runs=` argument since the dashboard was built.
`PaperRunner.run` returns a `RunResult`; `render_dashboard` needs a
`BacktestResult`; nothing converted one into the other. The only way to
exercise the class was to hand-build a fake, which is what `test_dashboard.py`
did, and a fake cannot tell you whether a real run would render.

`RunResult.result` is now built from the broker's own end-of-day statements
rather than recomputed, so the page cannot disagree with the statements behind
it. It is `None` when no session was due, because an equity curve of zero
points through `compute_metrics` renders a page saying 0.00% rather than one
saying nothing happened, and the resume path hits that case on every restart.

Verified on the AAPL sample: 506 sessions driven, 189 orders, 3 fills, a
1,139,712-byte page carrying both provenance classes, textually distinct
(PAPER SIMULATION against HISTORICAL BACKTEST, on each run and in the legend)
and visually distinct (`.prov-paper` is a dashed border in `var(--warn)`,
`.prov-backtest` a solid border in `var(--accent)`).

### 9. Two structural gaps, not bugs, now pinned

**No strategy can ask for a limit order.** `Signal` has no limit-price field,
and both drivers construct `OrderType.MARKET` unconditionally.
`PaperBroker.create_order` is the only door that accepts one. The limit model
is implemented, unit-tested, and now proven against real intraday range, and
no backtest in this project can ever produce one. F-21.

**The walk-forward does not refit per fold.** Anchored and rolling produce
identical out-of-sample results on all four strategies, in every field, and
anchored costs about 4x the wall clock (39.6s against 10.4s for ma_trend
50/200; 402.6s against 248.1s for zscore). The consistency and aggregate
figures are a time-stability measure on fixed parameters, not evidence about
re-selected ones. F-22.

---

## What did not reproduce

**zscore mean reversion, in all three diagnostics.** Both ma_trend variants
and buy-and-hold reproduce to the decimal. zscore does not:

| figure | reported | re-run |
|---|---|---|
| cost sweep, 0 bps | -2.55% | **-2.8768%** |
| cost sweep, 100 bps | -7.90% | **-8.0983%** |
| Monte Carlo closed trades | 65 | **63** |
| Monte Carlo terminal median | 97,333.98 | **96,939.62** |
| walk-forward positive folds | 21/38 | **17/38** |
| walk-forward consistency | 55.3% | **44.74%** |
| walk-forward aggregate return | -3.8% | **-4.0116%** |
| walk-forward trades | 132 | **121** |

Every verdict is unchanged, and in one respect worse: zscore is now positive
in fewer than half its folds. But those are not the current code's numbers.

My reading, stated as a reading rather than a fact: zscore is the only
strategy that trades often enough for the round 5 risk fixes (martingale size
pinning, order-id reservations, fill-time reconciliation) to change its order
flow, and `STRATEGY_RESULTS.md`'s own train table already said 63 closed
trades where its Monte Carlo row said 65. The zscore diagnostics most likely
predate those fixes and were never re-run. I have corrected the tables in
place and kept the reported figures beside them. F-23.

**Parameter sensitivity reproduced its verdict but not reproducibly.** Both
ma_trend variants come back `fragile=False`, which matches. The report never
recorded the grid, so the claim could not be reproduced from the report alone.
The grid is now in the test. Doing it also turned up that zscore's
`window=10` neighbour returns **+0.7588%** against a -3.177% base: the only
positive zscore variant anywhere in this project, and another trial against
the family-wise denominator.

---

## Two findings that sharpen the negative result

Both cut against the result rather than for it, which is worth saying plainly
because the easy mistake is to find reasons the strategies were treated
unfairly.

**The bootstrap scheme changes a verdict.** `bootstrap_metric` had never been
run on real data. On the 8,559-session train window, ma_trend 20/100's
annualised Sharpe is 0.42667827 with an iid 95% interval of [0.0823, 0.7929],
which excludes zero, and a stationary block-bootstrap interval of
**[-0.0100, 0.9245]**, which does not. Daily equity returns are serially
dependent, so the iid resample destroys exactly the structure that widens the
interval, and iid is the default. Buy-and-hold's own Sharpe interval,
0.28446269, contains zero under both schemes. On the train window, not one
strategy's Sharpe is distinguishable from zero under the honest scheme, and
neither is the benchmark's.

**Nothing in this project earns a dividend, and buy-and-hold is what that
understates most.** The AAPL sample is the only series with genuine adjusted
closes. The adjustment is real: eight material ratio steps over two years, one
a quarter, all positive, which is AAPL's dividend schedule. Over 1.9986 years
the price return is +5.882816% and the total return is +10.125484%, so
dividends are 4.242668 percentage points, or **2.042766 points a year**.
Through the engine at the production 5% cap, buy-and-hold reports +0.297592%
on the price series against +0.502028% on the total-return series: the
price-only figure is 59% of the real one and the Sharpe falls from 0.2418 to
0.1455.

NASDAQCOM is a price index, so `series_history` sets `adj_close` equal to the
close and the two returns are identical by construction. The engine also fills
and marks at `close`. Every return and Sharpe in the index study is a price
return reported as a return. I did **not** extrapolate the AAPL figure to the
index: the Nasdaq Composite's yield is in none of the data this project holds,
so the size of the understatement is unknown. The direction is not, and
buy-and-hold is invested the whole time, so a correct total-return benchmark
would have been harder to beat.

---

## The risk engine: no defect found, and that is the result

23 hypothesis properties over arbitrary order sequences against arbitrary
books, driven through the same call order the backtest engine uses. All seven
guarantees hold: approval bounded by the request, the single-name cap, gross
leverage, available cash, a REJECT carrying exactly zero, exits never blocked
by a breaker however the breaker was tripped, and reservations netting to zero
once every order resolves.

Nothing broke. So the real question is whether the properties could detect
anything, and that is proven twice rather than asserted.

The census, over 200 generated examples: **163 ACCEPT, 202 REDUCE, 742
REJECT**, seven distinct binding rules (`short_selling_blocked`,
`martingale_averaging_down`, `single_name_exposure`,
`max_orders_per_symbol_per_day`, `martingale_order_doubling`,
`duplicate_order`, plus `all_rules_passed`), and **419 decisions taken while
cash was already committed by an unfilled order**, which is the condition the
reservation caps exist for. The test fails if any outcome is missing and
prints the distribution rather than asking to be trusted.

Three mutants confirm teeth: a single-name cap 10x too generous, an engine
approving one more share than asked, and one whose breaker blocks exits. Each
is caught, and each mutation test first asserts the mutation changed the
decision at all. The cap mutant initially did not, because two identical
orders were stopped by the duplicate guard rather than by the cap, so the
mutation looked harmless. That is precisely how a teeth test passes vacuously,
and catching it is the reason the assertion is there.

---

## Three of my own tests asserted the wrong thing

Said plainly, because a bad test is worse than no test.

1. The exit property seeded a position from a fixed 1,000,000 of cash, which
   could not pay for 385 shares at 2,597.41, so it raised during setup instead
   of testing anything. Now funded from the drawn quantity and price.
2. The position-count property asserted against the production limit of 20
   with only three symbols in the generator, where no breakage could ever fail
   it. It now runs at `max_positions=2`, where the rule fires 14 times in 150
   examples.
3. A test asserted `warehouse._tx is warehouse.transaction`, which is False
   for two bound methods of the same function. It compares `__func__` now.

One test I wrote was also a bad *design* and I rewrote it: the `cash_cap`
timing bound was originally set at a 0.0001 price, where the defect takes
roughly 400 seconds. That test would have hung rather than failed, which is
useless as a regression guard. It is set at 0.005, where the old code took a
measured 4.076s and the fix takes microseconds.

---

## What I chose not to do, and why

- **I did not create any account or claim any API key.** Tiingo stays blocked.
- **I did not spend the test window again.** Nothing in this session touched
  it, and the atomicity work makes the guard harder to defeat, not easier.
- **I did not add the single-instrument research profile** (F-03, F-05). It
  needs your word on whether portfolio figures should read directly or keep
  the production cap and the `capDep` column.
- **I did not change `PromotionCriteria`** (F-12). The thresholds are still a
  delegated agent's choices and still need your sign-off.
- **I did not pin zscore's walk-forward in a test.** Rolling takes 248
  seconds and anchored 403, which is more than the suite should spend on one
  row. ma_trend 50/200's is pinned, so the next drift of that shape fails.
- **I did not change `PaperBroker.portfolio()` to a property** to match its
  six sibling properties (F-24). It has callers and the churn is not worth it;
  it is logged as the trap it is.
- **I did not fix the em dash in `signalforge/pipeline.py` or the existing
  report titles.** It is pre-existing and unrelated to tonight's scope. The
  two docs I wrote are dash-clean.
- **I did not re-run `research_cli`,** so the dashboard page in
  `dashboard/build/index.html` is still the one from handoff. The reports
  carry the corrected figures; the page does not yet.

---

## The single highest-value next action

**Decide on the free Tiingo key.**

Nothing else in the queue moves the research. Tonight found and fixed six real
defects, confirmed the risk engine's guarantees under arbitrary input, and
proved three previously unexercised paths work on real data, and the research
conclusion did not change by a single basis point, because the data cannot
support a different one. There is one index's closes, 506 days of one stock,
and a four-day cross-section. Every result is a single-instrument proxy study
and the cross-sectional half of the programme cannot be evaluated at all.

The adapter is written and fixture-tested. The moment `TIINGO_API_KEY` exists,
ingestion runs. It is free and takes no payment details, but it creates an
account in your name, so it is yours to decide.

**If the answer is no**, say so and the next most useful thing is small and I
can do it immediately: write up precisely what this system can and cannot
conclude from index-level data alone, and stop presenting the 23-name universe
in `config/research.yaml` as an intention it cannot meet.

Two smaller things also wait only on a sentence from you: whether to add the
single-instrument research profile (F-03), and whether a strategy should be
able to ask for a limit order at all (F-21). The second is more interesting
than it sounds now that the trade-off is measured: a limit buy at the previous
close pays 4.5 cents less and misses 19% of bars, and a naive limit round trip
over 199 real sessions lost 167.934215 with the sells averaging *below* the
buys. That is adverse selection, and no backtest here can currently see it.
