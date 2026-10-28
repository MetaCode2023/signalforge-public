# SIGNALFORGE - Decisions

The non-obvious calls, and why. `docs/ARCHITECTURE.md` says where each seam
is; this says why a choice went one way when the other way looked easier.

A decision is recorded here when a later reader would otherwise re-litigate
it from memory and plausibly reach a different answer. Several of these were
re-litigated once already.

---

## 1. Money is `Decimal`. Prices, cash, fees, everything.

Floating point is the wrong type for money and the reason is not pedantry.
`0.1 + 0.2 != 0.3` in binary floating point, and an accounting identity that
holds only approximately is not an accounting identity. The portfolio asserts
`equity == cash + marked position value` with `==`, not `isclose`, and
`tests/test_accounting_properties.py` checks that exactly after every single
applied fill in arbitrary sequences. That assertion is only possible because
the type is exact.

The counter-argument is speed, and it is real: `Decimal` arithmetic is roughly
an order of magnitude slower than float. That cost was accepted deliberately
and then paid for once, visibly: gauntlet round 6 found a quantity cap that
searched downward one share at a time, and `Decimal` made each of its
980,382,157 iterations expensive enough to turn a 0.9-second run into a
443-second one. The fix was to stop searching, not to switch to float. The
arithmetic was never the problem; the algorithm was.

Where an estimate is genuinely an estimate, float is used and the boundary is
explicit. `risk.limits.risk_budget_cap` computes a volatility in float because
a standard deviation is a model input, not a ledger entry, and converts back
to `Decimal` before the number touches a share count. Same in the Monte Carlo
and bootstrap code, which resample in numpy. The rule is: float for
statistics, `Decimal` for anything that becomes cash or shares.

Share counts are `int`. Not `Decimal`, not float. You cannot own 3.7 shares in
this book, and `Order.__post_init__` refuses a non-integer quantity, a bool
dressed as an int, and anything non-positive.

## 2. `volume_known` exists because a missing volume is not a zero volume

`Bar` carries a `volume_known` flag beside `volume`. It looks redundant and it
is not.

Two of the project's controls are functions of traded size. The liquidity
breaker refuses an order in a symbol that printed zero shares, and the
participation cap limits a fill to 1% of that bar's volume on the grounds that
assuming you could buy an unlimited quantity of a thin name is one of the most
flattering errors a backtest can make.

An index series publishes no volume. Three ways to handle that:

1. **Store zero.** Then the liquidity breaker refuses every order and the
   index study cannot run at all, for a reason that is an artefact of the data
   format rather than a market fact.
2. **Store a large placeholder.** Then both controls pass, silently, forever.
   The log says the participation cap was applied. It was not. A delegated
   agent did exactly this with a placeholder of 1,000,000,000 per bar, and
   the resulting figures were re-run through the honest path before any of
   them were published; see `reports/STRATEGY_RESULTS.md`.
3. **Say you do not know.** `volume_known=False` makes the fill model return
   the full requested quantity rather than a cap, makes the liquidity breaker
   return None rather than a pass, and makes the run's `data_limitations`
   state that the participation cap was SKIPPED for this instrument.

Three is the only one that does not lie. The distinction between "this control
passed" and "this control could not be evaluated" is the whole point, and it
is the same distinction the dashboard's `DATA UNAVAILABLE` badge exists for.
A control that cannot be evaluated and reports a pass is worse than no control
at all, because the log still says it fired.

## 3. The dashboard is generated HTML, written to a file

Not Streamlit, not Dash, not a React app.

The forcing constraint is that **pandas is blocked by a Windows Application
Control policy on this machine**. `import pandas` raises ImportError. Streamlit
imports pandas, so Streamlit is unusable here, and so is most of the Python
dashboarding ecosystem. That is a hard fact about the environment, verified,
not a preference.

Having been forced into it, it turned out to be the better answer anyway:

- **It is a file.** You can open it with no server, mail it, diff two of them,
  or check the size. `write_dashboard` returns the path and the CLI prints the
  byte count, so "the page got huge" is a number rather than a feeling.
- **It cannot disagree with itself.** A live app recomputes on each render, so
  two readers can see two numbers. A generated page is one snapshot of one set
  of results, stamped with the time it was generated.
- **A phone can open it.** Tables cap at 250 rows and say so with the true
  total; charts downsample and say by how much. `--serve` binds 127.0.0.1 and
  is for one reader on one machine.
- **It is testable as text.** 45 dashboard tests plus 14 for the newer
  sections assert on the rendered HTML: that every stated count links to the
  rows it counts, that the provenance classes are present and distinct, that
  an empty section says it was checked rather than vanishing, and that every
  value interpolated from a database row is escaped. That is much harder
  against a running app.

The charts are inline SVG generated from the data. No chart library, so no
dependency that could pull in pandas, and no client-side JavaScript that could
render something different from what was measured.

## 4. Four data sources were refused, and the reasons are in the repo

`config/data_sources.yaml` keeps every source investigated, including the ones
rejected, with the reason and the date. The refusals are part of the audit
trail: without them a later session re-derives the decision from memory and
may reach a different answer. All four were verified on 2026-10-26.

**Stooq - REJECTED, access control.** The CSV endpoint now returns a
JavaScript proof-of-work browser challenge instead of data, and the bulk
archive at `static.stooq.com` returns HTTP 401. Both are deliberate
anti-automation controls. Solving the challenge would be bypassing an access
control, and this project does not do that regardless of how convenient the
data is. This is the one refusal that is about conduct rather than quality.

**MarketParquet `/api/` - REJECTED, `robots.txt`.** The site's `robots.txt`
contains `Disallow: /api/`, and every sample download URL lives under
`/api/`. Fetching them programmatically is automated extraction the publisher
has asked crawlers not to perform. Files the operator downloaded by hand are
used through the `local_archive` provider instead, which never makes a network
request at all, and that is also what keeps the licence's
no-redistribution term safe. The free tier is a few recent days, so it could
not have supplied history anyway.

**Yahoo Finance - REJECTED, provenance.** The `query1.finance.yahoo.com`
chart endpoint used by `yfinance` is undocumented, Yahoo's terms are adverse
to automated collection, and it has a history of silent breaking changes and
rate limiting. A research warehouse whose provenance is an undocumented
endpoint cannot claim reproducibility: the dataset fingerprint would be a hash
of whatever that endpoint returned that day, with no way to know whether its
semantics changed.

**Nasdaq Data Link - REJECTED, not usable.** HTTP 403 without a key, and the
free WIKI/PRICES table has been frozen since 2018. Recorded as a candidate
rather than a live source, worth revisiting only if a key appears and pre-2018
history is wanted.

What that leaves is in `reports/DATA_COVERAGE.md`: 55 years of one index's
closes, 506 days of one stock, and a four-day 6,799-symbol cross-section. The
single action that unblocks per-symbol history is a free Tiingo API key. The
adapter is written and fixture-tested and would run immediately. Claiming the
key creates an account in the operator's name, so it is the operator's
decision and not one this project makes for them.

## 5. The hold-out window is spent once, enforced in code

Not requested politely in a comment. `ExperimentRegistry.record` refuses a
second evaluation of the test window for the same (strategy, parameters,
dataset fingerprint), raises `HoldoutViolation`, and logs the attempt as a
violation so the request itself stays on the record.

The reason it has to be code is that the temptation is real and arrives
disguised as diligence: "just check whether the fix changed the test result."
A hold-out looked at twice is a tuning set, and the second look does not feel
like cheating while you are doing it.

Three things about the mechanism are deliberate:

- **The key is content-addressed.** A changed parameter, a changed dataset
  fingerprint, or a changed cost assumption is a different experiment. That is
  correct, and it is also the obvious bypass, which is why `_number`
  normalises numerically equal values to one text form: `Decimal("0.05")`,
  `Decimal("0.050")` and the float `0.05` used to hash differently and bought
  three free looks.
- **The probe is read-only.** `holdout_spent` does not log, because callers
  use it to decide whether to run at all, and a probe that logged on every CLI
  start would bury the real signal under false positives.
- **The refusal is logged outside the transaction.** Making the two writes
  atomic was necessary (a failure between them left the experiment recorded
  while the hold-out read as unspent, which is exactly the state that permits
  a second look), but wrapping the whole call would have rolled back the
  violation log on the way out. Those are opposite requirements and both are
  tested, in `tests/test_registry_atomicity.py`.

## 6. The risk engine is independent by construction, not by convention

`RiskEngine.evaluate` takes an order, a portfolio, a point-in-time view, the
session state and the outstanding reservations. It holds no reference to a
strategy and there is no hook, callback or back-reference a strategy could use
to influence a verdict. A strategy proposes; risk decides; the engine applies
what survives.

Two consequences that are easy to get wrong the other way:

**Exits are exempt from every breaker.** A halted system that cannot reduce an
existing position is more dangerous than one that keeps trading, so
`check_manual_pause`, `check_drawdown`, `check_daily_loss`,
`check_staleness` and `check_liquidity` apply to risk-increasing orders only.
`tests/test_risk_properties.py` checks this against arbitrary combinations of
tripped state, and against a mutant whose breaker blocks exits, so the
exemption is proven rather than asserted.

**Caps count commitments, not just fills.** A cap computed against filled
positions only lets four orders on one bar each pass the limit and together
exceed it four times, with the log saying every one was approved. That is
worse than no cap. `Reservations` is keyed by order id and releases exactly
what it recorded, because an earlier version recomputed the release value from
a different day's mark and permanently stranded the difference: 9,060 of
phantom committed cash on a 95,457 book, measured, understating buying power
until every buy was refused for want of cash.

## 7. A misbehaving strategy aborts the run. Loudly.

A strategy that raises mid-run could be isolated: catch it, log it, carry on
with no signals for that session. That was considered and rejected.

The run would finish. It would produce a complete equity curve, a full set of
metrics, and a dashboard page, and nothing in the result would say that one
session of the strategy's decisions had been silently dropped. A partial
backtest presented as a finished one is the exact failure this project exists
to avoid.

So the run dies, and the abort names the strategy, the date and the window and
keeps the original exception as its cause. `LookAheadError` is exempt from
that wrapper: its type is the finding, and burying it inside a generic
`ValidationError` would hide the single most important thing this engine can
detect. Pinned in `tests/test_gauntlet_round6.py`.

The same reasoning applies one level down. A signal naming a symbol the
history has never held used to be dropped in silence: no order, no rejection,
no risk event, so a strategy that did nothing was indistinguishable from one
with no opportunities. It now raises. A symbol that IS in the history but has
no bar yet stays a quiet no-op, because a late listing is a market fact rather
than a programming error.

## 8. `INSUFFICIENT_EVIDENCE` instead of a number

Below 30 closed trades or 252 observations, `compute_metrics` returns
`EvidenceLevel.INSUFFICIENT_EVIDENCE` with a note naming each shortfall and
its size. Most figures in this project carry it. That is the honest reading of
the data available, not a defect in the metrics.

Related: a metric that cannot be computed returns `None`, never `0`. A Sharpe
of zero means "no risk-adjusted return"; a Sharpe of None means "not
computable from this sample". Returning 0 for the second conflates a measured
result with an absent one, and 0 renders as a number on a page.

The four-day cross-section is where this gets tested properly. A 126-day
momentum lookback over four sessions could rank on the three returns that
exist and report a figure that looks exactly like a real one. It does not: the
warmup gate withholds every signal and the metrics report
INSUFFICIENT_EVIDENCE naming both shortfalls. `tests/test_cross_section_scale.py`.

## 9. The walk-forward does not refit, and that changes what it means

`walk_forward` rebuilds the same fixed `StrategySpec` every fold, and each
fold's out-of-sample run reads only the test window. The training window
therefore reaches `train_metrics` and nothing else.

Measured consequence: anchored (expanding window) and rolling walk-forward
produce **identical** out-of-sample results on all four strategies, in every
field, differing only in `train_start` and `train_sessions`. Anchored costs
about 4x the wall clock for the same output.

That is not a bug, but it does mean the consistency and aggregate figures are
a time-stability measure on fixed parameters, not evidence that re-selected
parameters hold up out of sample. Anyone reading the walk-forward table should
read it that way. `tests/test_diagnostics_verification.py` pins the identity,
so if per-fold refitting is ever added the test fails, and that failure is the
signal to revisit every claim built on that table.

## 10. Costs are conservative in one direction only

Slippage always moves against the order. A favourable gap earns no discount,
because modelling luck as a bonus is how a backtest flatters itself. Market
orders fill at the next bar's open, never at the close of the bar the strategy
was reacting to. A gap through the reference price costs double. A limit fill
is clamped at its limit, so slippage can never push it through the price the
order was protecting. A zero-volume bar produces no fill at all.

`fill_delay_days` has a floor of 1, enforced by a config validator that
refuses to load otherwise. A same-bar fill assumes knowledge the strategy did
not have, so it is not a tunable.

## 11. PAPER ONLY is structural, not a setting

There is no live-order code path and the absence is proven rather than
promised. `tests/test_paper_broker.py::TestNoLiveOrderPath` parses the shipped
package with `ast` and fails if any module on the execution path (`paper`,
`risk`, `backtesting`, `strategies`, `core`, `config`) imports a network
library, if `signalforge/paper/**` references the data layer (which may use
the network), or if live-brokerage vocabulary appears anywhere in the package.
A complete paper session runs with `socket.socket` and
`socket.create_connection` rigged to raise. `PaperBroker.is_paper` is a
property with no setter. `ResearchConfig.paper_only` raises if a config file
sets it false.

Both structural guards were mutation-tested: adding `import requests` to
`broker.py` failed the import scan, and adding a `place_order` method failed
two tests. Both mutations were reverted and the suite re-confirmed green.

The data layer is deliberately excluded from the import scan. Fetching market
data over the network is legitimate; order execution borrowing that access
transitively is not, which is why the paper package may not reach the data
layer even though the runner is handed a warehouse object.

## 12. A negative result is the deliverable

No strategy demonstrated an edge over buy-and-hold. On the held-out test
window every baseline underperformed it and two of them *are* it. Nothing
survives the multiple-testing correction: the best out-of-sample Sharpe
anywhere in the project is 0.676, and the expected best of five random tries
on this data is 0.347.

That is reported as the result, first, at the top of
`reports/STRATEGY_RESULTS.md`, rather than buried under the diagnostics that
produced it. The alternative, which is what makes most backtest write-ups
useless, is to keep trying variants until one looks good and report that one.
The registry exists to make the count of variants tried visible, and
`family_wise_summary` exists because the per-strategy view answered the wrong
question: the denominator for "did any of the things I tried beat the
benchmark" is the number of things tried.
