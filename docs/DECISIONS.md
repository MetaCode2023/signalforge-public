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

## 13. The risk-off overlay: two gates, flatten to zero, no tuning

Two overlays exist, both decided before any result was seen and neither tuned
on the validation window. `vix30` is risk-off when VIXCLS is above 30, read
with a 1 session publication lag. `curve0` is risk-off when T10Y2Y is below 0
(an inverted curve), read with a 2 session lag. Each wraps `ma_trend` and
`xs_momentum`, giving four overlay strategies in all, named
`<strategy>_riskoff_<gate>`.

Both thresholds are folk conventions that were known with hindsight: "VIX 30
is fear" and "an inverted curve precedes recession" are the numbers everyone
quotes after the fact. Choosing them in advance does not make them free of
that, so a good overlay result is weaker evidence than the same result from a
threshold derived from data that predates it. Nothing here was searched; if a
third gate or a different threshold is ever tried, it counts as a new look and
the family-wise correction has to see it.

The engine only enters and exits, it never trims, so risk-off means flatten:
every ENTER_LONG becomes an EXIT_LONG while the gate is on. Re-entry happens on
the first risk-on session. Exits are never blocked by a breaker, so the
flatten always goes through.

The gate reads an observation published strictly before the session being
decided: the reference session is `lag_sessions` trading days back, and the
value used is the last non-null observation dated at or before it. An
observation older than 10 calendar days, or none at all, is "unknown" and is
treated as risk-on, with the count of unknown sessions reported beside the
risk-off and risk-on counts for every window. Duplicate observation dates are
refused. The series are the latest published values, not ALFRED vintages, so a
later revision is used as if it had always been known. That is a residual
look-ahead and is stated in every run's limitations.

## 14. The broad universe: breadth, eligibility, and what was left out

`config/universes/broad.yaml` lists every symbol with tiingo bars in the
warehouse (466 after removing the benchmark and the excluded names). It is a
separate file passed with `--universe-file`, not a key in `research.yaml`,
because `ResearchConfig` forbids unknown keys and the paper book reads
`research.universe`; the paper book is untouched. The file carries no
validation or test window. Validation and test come from the existing splits,
and the file may only raise `train_start`, to 1993-02-01 (SPY starts
1993-01-29), which must fall inside the research train window. The broad train
window is therefore 1993-02-01 to 2004-12-31, twelve years, and reports say
so. Bars are loaded from 1992-01-02 up to the validation window end only: test
window bars are never read, and the registry records the windows actually run.

Excluded names: the 24 ETFs (not companies), AES (an unverified 2001-09-26
halving) and USB (an unverified 1993-02-25 seven-to-one one-day drop). Both
are owner exclusions pending a second source for the corporate action, not
conclusions that the data is wrong.

Eligibility is applied point in time, each day, by a wrapper around the
strategy. A name is eligible if it is in the list, has at least 252 visible
sessions, has printed a bar within the risk engine's staleness limit (5 days,
taken from the risk config, so a frozen name cannot hold a slot the risk engine
refuses to fill), and the median of close times volume over its last 63 visible
sessions is at least 500,000 dollars. The floor is derived, not chosen: a 5%
position of 100,000 dollars is 5,000 dollars, and at the 1% participation cap
that needs 500,000 dollars of daily traded value to fill in one session.
Adjusted close times adjusted volume is the raw dollar volume, so the screen
does not move when a later split or dividend rewrites old prices. A name that
stops being eligible gets an explicit exit.

`ma_trend` is capped at 20 names (`max_names`), ranked by fast over slow
average, ties by symbol. The cap
limits the book to 20 names but does not make the risk cap irrelevant: the
risk engine counts unfilled orders toward `max_positions`, so a rotation (an
exit and an entry on the same signal day) fills one session late, and the cap
can still refuse an ENTER on the signal day. Without the cap, 466 names under a 20 position risk
limit would hold whichever 20 sorted first alphabetically and produce hundreds
of rejected orders a day. `xs_momentum` holds the top 20 at 5% each.

Dropped from the broad run: `vol_targeted_trend`, because the 5% single-name
cap binds before volatility scaling and it duplicates `ma_trend` exactly (the
21 name results show identical numbers); and `zscore_mean_reversion`, because
it is quadratic in history length, which is too slow at this scale. Dropping
them is a scale decision, not a result.

Survivorship bias grows with breadth. Every name here survived to 2026, so
more names means more survivors being treated as if they were knowable in
1993. The eligibility screen narrows who can be traded on a given day; it does
not bring back the companies that failed, were delisted or were acquired.
Read the broad figures as an upper bound on what the method could have done.

## 15. Every strategy is reported twice: as traded, and strategy only

The portfolio drawdown breaker (10% from peak equity) never resets. After one
deep drawdown it refuses every later entry, so the rest of a run is cash and
the table measures the breaker, not the strategy. On the 21 name run, ma_trend
was locked out from 1987-10-16 to the end of the train window.

Every research strategy therefore appears on two rows. "As traded" is exactly
the old behaviour with the default `RiskConfig`, breaker on, and its numbers
are byte-identical to before. "Strategy only" (label suffix " (strategy only)",
strategy id suffix `_nobreaker`) is the same strategy on an engine whose only
difference is the drawdown breaker; position cap, single name cap, daily loss
breaker, staleness, order caps and participation are all unchanged. The
buy-and-hold benchmark is not doubled, and the test window gets no second row,
because the single hold-out look is keyed to the as-traded specs.

The breaker is switched off by building a research-only `RiskConfig` with the
limit at 100% (`pipeline.strategy_only_risk`), not by adding an off switch to
`RiskConfig`. A 100% drawdown only trips if equity is gone. Defaults and
`risk.yaml` are untouched, and nothing in `signalforge/paper` or `paper_cli`
names the helper; a test fails if they do. The paper book keeps the breaker.

Neither row is the answer. As traded is what the guard would have allowed.
Strategy only shows what the signal did without the guard and will usually
look better, with larger drawdowns that the breaker exists to prevent. Lockout
notes stay on the as-traded rows.

## 16. A forecaster is put on trial before anything trades on it

`signalforge/forecasting/` adds probabilistic path forecasters (Kronos, a
block bootstrap, a driftless random walk) and a walk-forward trial that
scores their cones: 10-90 coverage, PIT histogram, CRPS, and directional hit
rate bucketed by path agreement, regime and spread.

The nulls are the point. A cone that covers about 80% and a hit rate that
rises with agreement both come free from a bootstrap of the last two years
of returns, and neither beats calling UP on every window (see
`reports/KRONOS_TRIAL.md`). A model's confidence display means something only
when it beats them on the same origins.

Choices that are not obvious:

- **Forecasters get a `ContextWindow`, never a view.** The window is cut from
  `history.view(origin)`, ends on the origin, owns a frozen copy of its
  arrays, and refuses a short or stale history. The trial reads realized
  closes itself and refuses any outcome dated after the window's end.
- **Kronos samples paths one at a time.** Upstream `predict(sample_count=N)`
  returns the mean of N samples, one line rather than a cone. The adapter
  repeats the context along the batch axis with `sample_count=1`, and
  `kronos-check` fails if the paths collapse.
- **Weights come from disk only.** No hub ids, so no code path in the
  package opens a connection; a test pins that no forecasting module imports
  a network library.
- **The test window is refused by the CLI.** Trials run on train or
  validation. The holdout is spent once, through `research_cli --run-holdout`.
- **The lift figure has an evidence floor.** TRENDING minus CHOPPY hit rate
  is reported only when both sides have 30 or more calls, the same floor as
  `min_trades_for_evidence`.
- **The cone strategy is research-only.** `ForecastConeStrategy` is an
  ordinary `BaseStrategy` that runs through the engine, but it lives outside
  `strategies/`, and a test fails if `paper/`, `strategies/` or `risk/`
  import the forecasting package.
- **The trial has its own dashboard tab, Crucible.** `forecast_cli trial` writes
  `dashboard/build/forecast.html`, linked from the operator page by a tab
  strip. It keeps the operator page's rules: no script, nothing from the
  network, a caption on every chart, every interpolation escaped (the
  scanner now covers it). The motion is CSS only and switches off under
  `prefers-reduced-motion`. `python -m signalforge.dashboard` writes a
  DATA UNAVAILABLE version when no trial has run, so the tab never 404s.
  It stays local: the Cloudflare dashboard Worker is not given price data.

## 17. Crucible reaches the phone as scores, never prices

The Cloudflare dashboard gets a Crucible tab fed by a new `forecast_trial`
report (`forecast_cli trial --report`). The report schema has no field that
can hold a price: trial scores, a forward cone as fractional returns from the
origin close, and simulated equity. Both validators (Python and the Worker)
reject unknown fields, so a price cannot be added without changing the
contract in two places, and `tests/test_crucible_report.py` runs the Python-
built report through the Worker's own code to catch drift between them.

Replays stay local-only: a realized path is a real price series, rebased or
not. The Worker source now lives in `cloudflare/` so the deployed code has a
reviewed home; deployment stays a manual step (README there), because this
repository holds no Cloudflare deploy credential and should not.

## 18. Assay: copy-trading is tested on what was knowable, next to the version that cheats

Copy-trading pitches pick "winners" by how their trades turned out, then show
what copying those same trades would have made. `signalforge/insiders/` tests
the stock-market version of the idea, copying company insiders' disclosed
open-market purchases (SEC Form 4), and puts the pitch's method beside the
honest one so the gap is visible.

- **The honest pick decides before each trade.** An insider is "smart" only
  on copied buys whose exit date is before their next buy's signal day: at
  least 3 of them, 60% or more winners, positive average. A test changes the
  outcome of a later trade and checks the earlier decision does not move.
- **The hindsight pick is kept, and labelled.** Same thresholds, judged on all
  of an insider's trades in the window. In a synthetic world where insiders
  have no edge at all it shows a 73% win rate and a 54% a year book while the
  honest pick matches random. That is the trap, measured.
- **Beating SPY is not the test.** The warehouse holds only companies that
  survived to 2026, so nearly anything bought in it beats SPY. Every arm is
  judged against two fair coins: random warehouse stocks on the same days,
  and random insider purchases filed in the same months.
- **Entry is the close after the filing day.** The data has no filing time,
  so a filing made after the close cannot be traded that day. Costs are 5 bps
  each way.
- **Rows are dropped, never repaired, and counted.** Amendments, joint
  filings, 10b5-1 planned trades, non-common stock, filed prices outside
  0.5x to 2x the market close, and unexplained >1.4x daily jumps while held
  (unless acknowledged in `price_move_acknowledgements`). The funnel from
  filings to trades is on the page.
- **Tickers map through the issuer's CIK.** An issuer's ticker is the one on
  its latest filing, so a rename keeps its history; a reused ticker belongs to
  its latest user.
- **Validation window only.** The SEC data starts in 2006, so the train window
  has none; the CLI refuses the test window, as `forecast_cli` does.
- **SEC data is public and its own source.** It is a new `filings` kind in
  `config/data_sources.yaml`. The downloader declares a contact User-Agent
  and pauses between files, per the SEC's fair-access policy.

## 19. Allocation: one trend rule on ten asset classes, against the same ten held forever

The stock work keeps finding no edge over buy-and-hold, which is what the
literature predicts for signals mined from daily prices on large caps. The
evidence that does hold up is time-series momentum across asset classes
(Moskowitz, Ooi and Pedersen 2012; Hurst, Ooi and Pedersen's century study),
and its value is in the crashes it sidesteps rather than in picking winners.
`config/allocations/assets10.yaml` tests that idea with what the warehouse
already has. Everything below was fixed before any result was seen.

- **The rule is the textbook one.** Hold an ETF while its own total return
  over the last 252 sessions is above zero, cash otherwise. No parameter was
  searched, and the 12-month lookback is the published default, not a choice
  made here. A second lookback, a vol-scaled weight or a short side would
  each be a new look for the family-wise correction.
- **Monthly, and only from what was known.** The decision is taken on the
  first session of each month from the previous session's close, because a
  session is only known to be the month's last once the next one has printed.
  The state is recomputed from the view every bar and re-asserted between
  decisions, so a crash in the middle of a month is not acted on until the
  next month. A test changes later bars and checks the decision does not move.
- **Ten ETFs, one or two per asset class.** SPY and IWM, EFA and EEM, IEF and
  TLT, LQD and HYG, GLD and VNQ. Sector funds are left out because they are
  slices of SPY. Where a class had a choice, the longest-listed fund was
  taken. Most list between 2001 and 2004 and HYG in 2007, so the train window
  (raised to 1993-02-01, SPY's start) is mostly SPY alone and validation,
  2005 to 2014 including 2008, is the window that means something.
- **The benchmark is the same ten, bought and never sold.** Beating SPY is
  the wrong test for a diversified book; the question is whether the timing
  adds anything over holding the same assets. A 100% SPY row is not possible
  here anyway: the per-trade risk budget caps one entry near 25% of equity.
- **One limit changes, for this run only.** The single-name cap is 10%
  instead of 5%, because ten asset classes at 5% leave half the book in cash
  and the result would measure the cash. `AllocationConfig.risk` replaces only
  that field, caps it at 25%, and refuses weights that add to more than the
  book. The per-trade risk budget, the drawdown and daily-loss breakers,
  staleness and every order cap are unchanged. `risk.yaml`, the defaults and
  the paper book are untouched, and a test fails if `signalforge/paper` or
  `paper_cli` mention the allocation.
- **The risk budget is left to bite.** It sizes an entry smaller when trailing
  volatility is high, so a re-entry after a crash is smaller than 10%. That is
  an existing control, kept, and it is stated on every row. The engine also
  never trims, so a position drifts with its price between decisions instead
  of being rebalanced to 10%.
- **Both rows, as before.** Every run reports the rule as traded (the 10%
  drawdown breaker, which never resets, will likely lock it out after 2008)
  and strategy only (decision 15).
- **The test window is refused.** `allocation_cli --run-holdout` exits without
  running. Spending the one honest look on this book is a separate decision,
  to be written here before it is made.

## 20. The allocation book paper-trades as its own book, under its file's cap

`paper_cli --allocation` runs `config/allocations/assets10.yaml` as a paper
book with the id `allocation:assets10`. It follows decision 19's rule and the
existing paper rules, with three points of its own:

- **Its own approval, from the file.** `--allocation --approve tsmom
  --approved-by <name> --reason <why>` records the strategy for that book only;
  no other book's approval authorises it, and it authorises no other book.
  The parameters (symbols, lookback, weight) come from the file, not a flag,
  and only `tsmom` and `buy_and_hold` can be approved for it. Changing the
  file's symbols makes the next run refuse until the book is re-approved, as
  for any OHLCV book.
- **One limit differs, chosen by the CLI.** `paper_cli` hands the runner the
  file's single-name cap (10%) for this book and `risk.yaml` for every other.
  The runner and broker cannot choose their own: a test fails if anything
  under `signalforge/paper` mentions the allocation. Every other limit holds,
  including the 10% portfolio drawdown breaker, which never resets (decision
  15 keeps it on paper books). A diversified book that is often 60 to 80%
  invested can still trip it in a crash before the monthly rule exits. If it
  trips, the book stops entering and says so; lifting it is the operator's
  decision, not the code's.
- **Signals on adjusted closes, trades on raw.** Like the universe book, the
  book runs on raw prices with splits and dividends booked by the broker. The
  rule reads adjusted closes, so a dividend or a split never looks like a
  trend change.

The freshness gate checks all ten ETFs against SPY's calendar, so the book
cannot trade until the daily collector keeps all ten current.

## 21. The Scoreboard: every idea against one bar, verdicts computed

The dashboard's first tab answers one question for every idea tested: did it
beat just buying and holding, once costs and risk are counted? It is built
from the evidence on every render, never typed in.

- **A bar is only ever measured on the same data.** Classic strategies come
  from the `experiments` table, grouped by dataset fingerprint and window; the
  bar is the buy-and-hold row in that same group, and a group without one is
  not scored. A rebuilt warehouse (same dataset, more bars) replaces the older
  group rather than adding a second table.
- **Validation and test only.** Train rows, walk-forward folds (descriptions
  carrying `limitations:`) and the strategy-only `_nobreaker` variants stay in
  the Learning Lab. The test window shows whatever its single look recorded.
- **Ties and thin wins are not wins.** Sharpe within 0.005 of the bar is a
  tie. A strategy ahead of the bar on `INSUFFICIENT_EVIDENCE` reads "ahead,
  too few trades" and is not counted as clearing it.
- **Forecasters beat the always-up rate only with their whole interval.** The
  low end of the 95% Wilson interval must clear the base rate; an interval
  that straddles it is "too close to call". Fewer than 30 calls is "too few to
  judge". With `--backtest`, the trial record now also stores each engine
  run's final equity and Sharpe (no curve), so the cone rule is scored against
  buy-and-hold on the same stocks.
- **Insider strategies face random insiders.** p < 0.05 against random
  insiders filing in the same months, or "no edge found". Hindsight picks are
  listed and never scored.
- **Queued is a verdict.** Kronos and the insider strategies show as queued
  until a record containing them exists, so the board never implies more was
  tested than was.
- **A win on the board is a lead, not a promotion.** The headline says so;
  promotion still needs the gate's walk-forward checks and a named approver.
- **It refreshes itself.** `python -m signalforge.dashboard` rebuilds it, and
  every `forecast_cli trial` and `insider_cli trial` rewrites it beside their
  own page. A refresh that fails (a locked warehouse, say) prints a note and
  never fails the trial.
