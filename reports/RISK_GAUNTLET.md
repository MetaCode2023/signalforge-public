# SIGNALFORGE — Adversarial Risk Gauntlet

Six rounds completed. 33 real defects found and fixed, each with a failing
test written before the fix. Nothing below is marked fixed without a test that
failed first and passes now.

Final suite: **1161 passed, 1 skipped**.

## Round 1 — data integrity, bias and valuation

Ten failure modes identified, ranked by severity, then tested.

| # | Failure mode | Verdict | Evidence |
|---|---|---|---|
| 1 | **Adjusted-close anchoring.** A back-adjusted series' LEVEL at time T depends on corporate actions *after* T, so a level-dependent strategy silently reads the future | **Safe, now proven** | All five shipped strategies are scale-invariant: signals are identical when the whole series is multiplied by a constant. A deliberately level-dependent strategy fails the same check, which proves the test has teeth rather than passing vacuously |
| 2 | **Stale valuation of a dead holding.** `marks()` returns the last known close forever, so a delisted position was carried at its final price indefinitely and equity was silently overstated | **FIXED** | Raises a `stale_position_valuation` FLAG event once per symbol and records a limitation. No liquidation is invented — inventing a trade is worse than carrying a declared stale mark |
| 3 | **Survivorship bias** in the universe and in the cross-section snapshot | **Unfixable with this data; declared** | Cannot be fixed without a delisted-securities dataset. Test asserts the run declares it rather than omitting it |
| 4 | **Fingerprint ignored `volume_known`**, which decides whether two risk rules fire, so two materially different runs claimed the same dataset version | **FIXED** | `volume_known` now hashes; two otherwise-identical datasets fingerprint differently |
| 5 | **Non-positive series observations** coerced into a price | **Already correct; pinned** | Dropped, counted and declared, never coerced. An all-non-positive series raises |
| 6–10 | Duplicate bars, weekend-dated bars, future-dated bars, data revisions, timezone handling | **Already guarded** | `PriceHistory` raises on duplicates; validator quarantines weekend and future-dated rows; warehouse records revisions instead of overwriting; all dates are date-only with no implicit timezone |

## Round 2 — the gap between signal time and fill time

Every order is sized against the book as it stood when the signal fired and
fills a bar later. Each item here is a way that gap turns into a crash or a
wrong number on real data.

| # | Failure mode | Verdict | Evidence |
|---|---|---|---|
| 1 | **A buy whose cost rises above available cash before it fills.** Raised `AccountingError` from inside the event loop and **aborted the entire backtest**. On real data a gap up does this routinely | **FIXED — was critical** | Fills are reconciled against the book at fill time and reduced to what cash affords, with commission recomputed for the smaller size. The reduction is logged as `cash_available_at_fill` |
| 2 | **A sell of a position another order already closed.** Same crash | **FIXED — was critical** | Clamped to the held quantity, logged as `position_closed_before_fill`. The duplicate-intent guard already caught the single-strategy case; this covers the general one |
| 3 | **Pending orders consumed no limits.** Four orders on one bar, each inside `max_positions` and the single-name cap, together breached both — while the log recorded every one as approved | **FIXED — the cap was failing silently** | `Reservations` commits an accepted buy against `max_positions`, the single-name cap, the cluster cap, gross leverage and available cash until it fills or expires |
| 4 | Split buys of one symbol on one bar exceeding its exposure cap | **FIXED** (same mechanism) | 50-share ceiling holds however the order is sliced |
| 5 | **Corporate actions inside a holding period** are not modelled | **Declared on every run** | A share count never adjusted for a split, and dividends never credited, is now stated up front rather than discovered from a wrong P&L later |
| 6–10 | Partial-fill remainder, fees on partial quantity, Decimal rounding, replay duplication, missing-mark valuation | **Already guarded** | Remainder expires as a day order; fees computed from filled quantity; Decimal throughout; `fill_id` makes replay idempotent; a missing mark raises rather than valuing at zero |

## Round 3 — risk state, learning integrity and promotion

This round re-verified claims that delegated agents made about their own code.
A subagent's report is a claim, not evidence, and the promotion gate is the one
component where being wrong means acting on a strategy that was never shown to
work. All four re-verified claims held.

| # | Failure mode | Verdict | Evidence |
|---|---|---|---|
| 1 | **A strategy promoting itself, or a forged approval** | **Holds** | The gate refuses a strategy object as candidate, baseline *or* approver, and refuses the candidate's own name as approver. Without a named human the best candidate is only `PROPOSED`. `apply_promotion` raises `PromotionError` on anything not fully passing and approved |
| 2 | **`INSUFFICIENT_EVIDENCE` promoted anyway because the numbers look good** | **Holds** | A candidate with Sharpe 9.0 and a 0.1% drawdown is still REJECTED when flagged insufficient |
| 3 | **A better return bought with a far worse drawdown** | **Holds** | Rejected, with `max_drawdown` named as the failing check and its numbers recorded |
| 4 | **The hold-out evaluated more than once**, turning the only honest number into another tuning target | **Holds, and observed in production** | A second `--run-holdout` run was refused for all five strategies and logged with `violation=TRUE`. The validation window remains freely re-runnable; `tune()` refuses any window but train |
| 5 | **Risk state leaking between runs**, so one fold inherits the previous fold's peak equity or tripped breaker | **Holds** | A second run opens at zero drawdown with peak equity equal to opening equity. Risk events and reservations do not accumulate across runs |
| 6 | **Walk-forward folds overlapping, or reaching into the test window** | **Holds** | Every fold has `train_end < test_start`; a run bounded at `validation_end` never exceeds it; a short final fold is skipped with a stated reason |
| 7 | **An unseeded diagnostic**, making a reported interval irreproducible and therefore not evidence | **Holds** | Monte Carlo with the same seed gives identical median/p05/p95; a different seed differs |
| 8 | **Metrics hiding how little capital was deployed** | **FIXED — real gap** | `exposure` measures TIME IN MARKET and reads ~1.0 for a strategy holding 5% daily, so nothing revealed that 95% of the book was idle cash. Added `average_capital_deployed` and `peak_capital_deployed`, with a test proving the metric discriminates rather than always reading low |
| 9–10 | View immutability, cross-run determinism | **Already guarded** | `PointInTimeView` is frozen and returns tuples; no RNG in the engine; identical inputs give identical fills and equity curves |

## Round 4 — found by running the real per-symbol OHLCV data

One defect, and a quiet one. Running the AAPL sample was the first time the
real OHLCV path executed end to end, and it surfaced something neither the
engine's own limitation detector nor any earlier round had caught.

| Failure mode | Verdict | Evidence |
|---|---|---|
| **Two unrelated datasets silently concatenated.** The warehouse holds AAPL twice: 506 bars from a 2015-2017 sample and 4 bars from the 2026 cross-section snapshot. `ohlcv_history(warehouse, ["AAPL"])` returned both as one history with a **3,533-day hole** in it, and nothing said so. A 20-day moving average or a z-score computed across that hole is meaningless but is indistinguishable from a real number. Every individual bar is valid, which is why no per-bar validation rule caught it | **FIXED** | A gap of 365 days or more inside one symbol now raises, naming both sources and the gap length. A gap of 60 to 364 days loads but is declared as a DISCONTINUOUS HISTORY limitation. An ordinary holiday gap does not trip either. `sources=[...]` selects one dataset deliberately. Verified against the real warehouse: the 3,533-day gap is refused, and `sources=['plotly_aapl_sample']` loads 506 bars cleanly |

The lesson generalises: per-record validation cannot catch a defect that only
exists in the *relationship* between valid records. Coverage reporting had
already hit the same issue and solved it by grouping on `(symbol, source)`;
the history loader had not.

## Found outside the numbered rounds, by running real data

| Failure mode | Verdict | Evidence |
|---|---|---|
| **Buy-and-hold never bought.** It entered only on a symbol's first bar in all of history, so over any window starting later it emitted HOLD from day one and returned exactly **0.0%**. A benchmark stuck in cash flatters every strategy measured against it | **FIXED — was the worst defect found** | Now re-asserts the desired long state each bar, which also lets the entry survive a rejected first order. Caught only because the validation window returned a suspiciously round 0.0% |
| **Fabricated volume for an index series.** Inventing a volume would make the liquidity breaker and participation cap fictions that still looked like controls | **FIXED** | `Bar.volume_known` distinguishes "no volume published" from "zero traded". When unknown, both checks are SKIPPED and the run says so. A genuine zero print is still refused, so the guard stays intact for instruments that do report size |
| **A 14.7 MB dashboard** on a phone-first brief | **FIXED** | Charts downsampled to 600 points with min/max-preserving decimation so a one-day drawdown trough is never smoothed away; tables capped at 250 rows stating the true total; `count_link` caps its own `data-count`. 3.69 MB, 87 count links, 0 contract violations on the real page |

## Round 5 - independent adversarial code review

Rounds 1-4 were my own. Round 5 was an independent reviewer pointed at the
source with instructions to review the code and not my description of it. It
reproduced every finding with measured output rather than inferring from the
shape of the code, and it found **14 defects I had missed**, five of them
HIGH. No CRITICAL, and nothing was a security hole; they were accounting and
control defects.

Three of the five HIGHs were wrong in the direction that makes the system
*look* like it is working, which is the direction that matters most.

| Sev | Defect | Measured evidence | Fixed |
|---|---|---|---|
| HIGH | **The hold-out guard refused to RECORD a second look but still computed, printed and dashboarded the figures.** For a project whose credibility rests on one unspent number, "the registry refuses a second look" overstated what the code did | Two consecutive `--run-holdout` passes returned byte-identical out-of-sample figures; only the violation flag differed | Yes. The registry is consulted BEFORE the backtest via a read-only `holdout_spent()` probe; a spent window is absent from the output entirely. The README claim is corrected |
| HIGH | **The paper broker passed no `Reservations`**, so every cap was computed against filled positions only | Two strategies each granted the full single-name cap: 1.799x equity committed, `cash_cap` approving $179,900 against $100,000 with no margin | Yes |
| HIGH | **`settle()` had no fill-time reconciliation and no try/finally**, so an `AccountingError` skipped the queue reassignment; applied orders stayed queued and re-filled next session under a new `fill_id`, which replay protection does not recognise | One accepted 100-share order produced a 200-share position; the offending order never left the queue and re-raised every session forever | Yes. The queue is split before any fill is attempted, and the reconciliation is now shared with the engine (71 duplicated lines removed) |
| HIGH | **A cap-driven REDUCE permanently pinned a strategy's size** via `martingale_order_doubling`, because `record_submission` stored the APPROVED quantity | A reduction to 1 share left 1 as the only passing size, re-pinning the ceiling each time; the strategy was silently disabled and the log blamed "martingale" for risk's own decision | Yes. The rule compares against what the strategy ASKED for, and a closed position ends the campaign |
| HIGH | **Reservation cash released at a different mark than reserved**, stranding the difference when `fill_delay_days >= 2` | 9,060 of phantom committed cash on a 95,457 book: buying power understated 9.5% with zero orders outstanding, growing with trade count until every buy is rejected | Yes. Reservations are keyed by order id and release exactly what they committed; tested across delays 1, 2, 3, 5 |
| MED | **Parameter hashing was not canonical**, so the hold-out guard could be sidestepped | `Decimal("0.05")`, `Decimal("0.050")` and `0.05` gave three keys for one logical weight; a set-valued parameter hashed differently in every process | Yes |
| MED | **`fingerprint` was sensitive to Decimal scale**, so one dataset could fingerprint two ways | Same prices via a provider parse vs a `DECIMAL(18,6)` read gave different hashes; the fingerprint is part of the hold-out key | Yes |
| MED | **`volume_known` was lost on a warehouse round trip**, flipping "nobody told us" into "nothing traded" | The liquidity breaker began blocking every buy, the participation cap became 0, and the run stopped declaring the checks were skipped | Yes |
| MED | **`realized_pnl` omitted a partially closed position's realized leg** | `starting + realized + unrealized` missed equity by 48.67 on a position partially closed at the participation cap, reachable with the DEFAULT config | Yes |
| LOW | **Ledger truncation was undetectable** - dropping the tail leaves a valid chain | `verify()` returned True after deleting the last events | Yes. An append counter catches live truncation; a `head()` witness catches it across a restart |
| LOW | **A forged APPROVED promotion with an incomplete checklist was accepted** - absent checks read as passing | A proposal with one passing check constructed and `apply_promotion` accepted it | Yes. `REQUIRED_CHECKS` is enforced |
| LOW | **A pre-populated `Portfolio` had no replay protection** for the fills it already held | `_applied_fill_ids` was not derived from `fills` | Yes |
| LOW | **A quarantined record claimed its (symbol, date) key**, so a CORRECTED record arriving later was quarantined as a duplicate of the broken one it replaced | A valid bar following an `impossible_ohlc` bar for the same key was rejected | Yes |
| LOW | **`settle` ignored `apply_fill`'s return value**, so the ledger recorded fills the book had refused | 2 FILL events for 1 applied fill | Yes |

Deliberately not changed, with reasons: Decimal drift in `average_cost` is
bounded at ~1e-26, reset at full close and never reaches equity;
`_update_status` being quadratic in order count is performance with no
correctness effect; `PointInTimeView._history` being reachable by name was a
**docstring overclaim**, not a leak, so the docstring was corrected to state
exactly what is guaranteed rather than the code being restructured.

The review also independently confirmed clean, with evidence rather than
"looks fine": dashboard escaping at every call site (two hostile pages with
payloads planted in 39 data fields, 0 raw script tags and 0 raw-context
event handlers), the accounting core under 300 randomised fills with
non-terminating prices, `PointInTimeView` truncation on every accessor, the
limit-fill inequalities, `resize`/`affordable_quantity`, engine step
ordering, the `_decide` pipeline order, the buy-only breaker asymmetry,
provider secret handling, SQL parameterisation, the upsert revision logic,
and the chart downsampling boundaries.

A defect I introduced *while* fixing these was caught by my own regression
check rather than by review: adding the `volume_known` column broke every
pre-existing database, because `CREATE TABLE IF NOT EXISTS` leaves an
existing table alone. Fixed with an additive-only migration.

## Round 6 - boundary and hostile conditions

Eleven conditions nothing had attacked yet: a history one bar long, a window
with `start == end`, a strategy that returns None, raises, names a symbol
that does not exist, or emits the same signal three times, prices at 0.0001
and at 813,595, a book collapsing toward zero equity, a fill costing exactly
the cash on hand, one cent short of that, and `max_positions == 1` met by two
simultaneous entries.

Six defects. Three were silent; two stopped the engine dead; one was found by
grepping for the shape of another rather than waiting for it to hurt.

| # | Failure mode | Severity | Evidence |
|---|---|---|---|
| 1 | **`limits.cash_cap` searched for the answer one share at a time.** It started from `(cash - min_commission) / price`, an upper bound that ignores the per-share commission, and decremented until the total fitted. Steps scale as cash/price: invisible on a 100 stock, **980,382,157 decrements at a 0.0001 price** | **HIGH** | Measured on the real 6,799-symbol cross-section, 85 of whose symbols trade under a cent and whose minimum close is 0.000001: **442.094 of the run's 443.030 seconds, 99.8%, inside 41 calls, 10.8s per call**. cProfile could not finish the run (it instruments every call and the hot loop calls a helper), so the attribution is from deliberate instrumentation. Now closed form; the same run takes **0.930s** with byte-identical output: 85 orders, 36 fills, 41 risk events, final equity 99,061.157008. Cross-checked against a brute-force reference over 2,400 random price/cash pairs across 6 commission schedules, zero mismatches |
| 2 | **The same search lived a second time, in `FillModel.affordable_quantity`.** Bounded by the order size rather than by cash/price, so it could not run for minutes, but a measured **1.556s for one call** reducing a 10,000,000-share penny position. Defect 1's fix makes positions that large legitimately reachable, so the path got hotter | **MEDIUM** | Both call sites now share one solver, which lives on `ExecutionConfig` beside the commission schedule it inverts. A test asserts the two agree across four prices, because two copies of the same arithmetic had already drifted into two implementations |
| 3 | **A signal for a symbol the history has never held vanished.** `_order_from_signal` found no bar, returned None, and the run recorded nothing: no order, no rejection, no risk event, no error. A renamed or mistyped ticker produced a strategy that did nothing and a run that looked healthy. The risk engine has `check_price_available` for exactly this, but the order never reached it | **MEDIUM** | Now refused, naming the symbol, the strategy and what the history does hold. A symbol that IS in the history but has no bar yet stays a quiet no-op, because a late listing is a market fact rather than a programming error, and that case is tested separately |
| 4 | **A strategy exception aborted the run with no context.** A 38-fold walk-forward dying inside a fold reported bare `ZeroDivisionError` | **MEDIUM** | Aborting is kept deliberately: catching it and continuing would produce a complete equity curve and a full set of metrics with one session of decisions silently missing, which is a partial backtest presented as a finished one. The abort now names the strategy, the date and the window, and keeps the original as its cause. `LookAheadError` is exempt from the wrapper because its type is the finding, and that exemption is itself pinned |
| 5 | **A limitation note interpolated the whole symbol list.** All 6,799 tickers went into the "only N trading days available" string, producing a **52KB limitation** that travels into `BacktestResult.data_limitations`, the reports and the dashboard page | **LOW** | Now a count plus eight symbols. The same fix applies to the "no bars in the warehouse" refusal, which had the same shape |
| 6 | **`registry.record` was not atomic** (F-12 area, flagged in round 5). The experiment row and the `test_evaluations` row that spends the hold-out were two statements. A failure between them left the experiment recorded while the hold-out read as unspent, so the same strategy on the same data could be evaluated against the held-out window a second time and the registry would permit it | **MEDIUM** | Now one transaction. Verified by injecting a failure into the evaluation write: before, the experiment survived with the hold-out unspent; now neither row exists and a retry records cleanly. The guard deliberately runs OUTSIDE the transaction, because it logs a refused second look as a violation and then raises, and a rollback would have erased that audit record. Both requirements are tested |

Five conditions held and are kept as confirmations, because an omitted case
makes "no problem" indistinguishable from "not checked": a one-bar history
(order expires, no fill, no stranded reservation), `start == end` (one
session), a strategy returning None, three identical signals in one call (one
fill, two `duplicate_order` rejections), a signal dated before or after the
session (both refused by name), a price above the whole per-trade budget (no
order), a book collapsing to 0.045% of its starting equity (no crash,
drawdown below 1, metrics finite and gated), a fill costing exactly the cash
on hand (allowed, cash lands on zero), one cent short of it (REDUCE, never an
overdraft), and `max_positions == 1` with two simultaneous entries (the
second refused on the signal bar, before the first has filled).

Exactly-zero equity turned out to be unreachable, and that is the design:
reaching it would mean marking an open position at zero, and the book refuses
to value an unmarkable holding at nothing rather than understate a drawdown.
Tested as a refusal.

## Mutation checks

Three guards were deliberately broken to confirm the tests catch them, then
reverted:

| Mutation | Expected | Result |
|---|---|---|
| `import requests` added to `paper/broker.py` | import scan fails | **Failed as intended**, 1 failed / 6 passed |
| `place_order` method added to `PaperBroker` | vocabulary + method scan fail | **Failed as intended**, 2 failed / 5 passed |
| A level-dependent strategy run through the scale-invariance check | check fails | **Failed as intended** |

The strategies agent separately mutation-checked its own work (ignoring
`skip_days`; zeroing the zero-variance tolerance) and reported both caught.

## Remaining weaknesses the gauntlet did not resolve

These are limitations, not open bugs. They are carried in `OPEN_FINDINGS.md`.

1. **Survivorship bias** cannot be fixed without delisted-securities data.
2. **Cross-sectional claims are untestable** on a four-day cross-section.
3. **Incremental-ingest blind spot**: the jump, stale-price and adjustment
   rules only see the current batch, so a jump spanning two separate ingests
   would not be flagged.
4. **Keyed providers have never executed.** Tiingo and Alpha Vantage are
   fixture-tested only; their real response shapes are unverified.
5. **The index study is a proxy.** No ETF tracking error, expense ratio or
   spread is modelled, so even a positive result would overstate a tradeable
   one. It is also a PRICE index, so no dividend is ever earned: measured on
   the only real adjusted series in the project, that understatement is worth
   2.042766 percentage points a year. See `STRATEGY_RESULTS.md`.
6. **No strategy can ask for a limit order.** `Signal` has no limit-price
   field and both drivers construct `OrderType.MARKET` unconditionally, so the
   limit model is reachable only through `PaperBroker` by hand. It is tested,
   and now proven on real intraday highs and lows, and no backtest in this
   project can produce one. F-21.

Item 5 of this list previously read "deflated Sharpe and the multiple-testing
summary have not been run on the real results". They have been, twice: see
F-08, and the re-verification in `tests/test_diagnostics_verification.py`.
