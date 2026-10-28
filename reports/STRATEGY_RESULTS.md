# SIGNALFORGE — Strategy Results

Generated 2026-10-26, diagnostics re-verified 2026-10-27.

## The result, stated first

**No strategy demonstrated an edge over buy-and-hold.** On the held-out test
window every one of the four baselines underperformed it, and two of them
*are* it. On the pre-test walk-forward, buy-and-hold had the best Sharpe, the
best consistency and the highest return of any candidate.

A negative result is a legitimate outcome, and this is one. Nothing here
should be promoted to paper trading on this evidence, and the promotion gate
agrees: it rejects both trend variants on risk-adjusted return against the
baseline.

## What every number below is, and is not

All figures come from **one instrument**: the NASDAQCOM index level, 14,048
daily closes, 1971-02-05 to 2026-10-23. Carried on every run:

- **PROXY STUDY, NOT A TRADEABLE RESULT.** An index cannot be bought. A real
  implementation would trade an ETF and pay tracking error, expense ratio,
  spread and creation/redemption friction, none of which are modelled.
- **Close-only.** Open, high and low are set to the published close, so there
  is no intraday range. Gap modelling degrades to close-to-close and limit
  orders cannot be evaluated against a real high or low.
- **No volume published.** The liquidity breaker and the participation cap are
  SKIPPED for this instrument, not passed. No volume was invented to satisfy
  them, so fill sizes are unconstrained by traded size.
- **No dividends.** NASDAQCOM is a price index, so total return is understated
  for buy-and-hold and for anything holding it. Now quantified on the only
  real adjusted series in the project: see §Dividends. It is worth 2.042766
  percentage points a year on one large-cap name, and the direction cuts
  against the negative result, because buy-and-hold is the strategy most
  understated by a missing dividend.
- **Capital deployed is small.** The 5% single-name cap on a one-symbol
  universe leaves roughly 95% of the book in cash. Portfolio-level CAGR and
  Sharpe therefore describe a small slice of a strategy diluted by idle cash.
  The `capDep` column is the number that makes every other column readable.

Costs applied: 5 bps slippage each way, $0.005/share commission with a $1.00
floor, signal at close T with the earliest fill at T+1.

## Train window — 1971-02-05 → 2004-12-31

Dataset fingerprint `eda78f442b95`.

| Strategy | Total ret | CAGR | Sharpe | Max DD | Trades | Cap deployed | Evidence |
|---|---:|---:|---:|---:|---:|---:|---|
| MA trend 50/200 | 13.0% | 0.36% | 0.45 | 3.3% | 18 | 3.1% | INSUFFICIENT_EVIDENCE |
| Cross-sectional momentum 126/21 | 92.9% | 1.96% | 0.28 | 54.9% | 0 | 21.4% | INSUFFICIENT_EVIDENCE |
| Mean reversion z=2 / 20d | **-3.2%** | -0.10% | **-0.30** | 3.9% | 63 | 0.4% | SUFFICIENT |
| Vol-targeted trend 10% ann. | 13.0% | 0.36% | 0.45 | 3.3% | 18 | 3.1% | INSUFFICIENT_EVIDENCE |
| **Buy and hold (benchmark)** | **103.7%** | **2.12%** | 0.28 | 56.6% | 0 | 22.9% | INSUFFICIENT_EVIDENCE |

## Validation window — 2005-01-01 → 2014-12-31

Dataset fingerprint `d75380b9bb31`.

| Strategy | Total ret | CAGR | Sharpe | Max DD | Trades | Cap deployed | Evidence |
|---|---:|---:|---:|---:|---:|---:|---|
| MA trend 50/200 | 2.4% | 0.23% | 0.38 | 1.2% | 6 | 3.1% | INSUFFICIENT_EVIDENCE |
| Cross-sectional momentum 126/21 | 5.3% | 0.51% | 0.52 | 3.1% | 0 | 5.3% | INSUFFICIENT_EVIDENCE |
| Mean reversion z=2 / 20d | 0.8% | 0.08% | 0.30 | 0.4% | 32 | 0.5% | SUFFICIENT |
| Vol-targeted trend 10% ann. | 2.4% | 0.23% | 0.38 | 1.2% | 6 | 3.1% | INSUFFICIENT_EVIDENCE |
| **Buy and hold (benchmark)** | **5.3%** | **0.51%** | **0.52** | 3.1% | 0 | 5.3% | INSUFFICIENT_EVIDENCE |

## Held-out test window — 2015-01-01 → 2026-10-23

Dataset fingerprint `076e55bef9e9`. **Evaluated once.** A second evaluation of
the same strategy and parameters on this dataset is refused by the registry
and logged as a violation; that refusal has since been observed in practice.

| Strategy | Total ret | CAGR | Sharpe | Max DD | Trades | Cap deployed | Evidence |
|---|---:|---:|---:|---:|---:|---:|---|
| MA trend 50/200 | 1.9% | 0.16% | 0.32 | 1.4% | 3 | 1.7% | INSUFFICIENT_EVIDENCE |
| Cross-sectional momentum 126/21 | 21.3% | 1.65% | 0.68 | 5.2% | 0 | 10.7% | INSUFFICIENT_EVIDENCE |
| Mean reversion z=2 / 20d | 0.7% | 0.06% | 0.31 | 0.3% | 7 | 0.1% | INSUFFICIENT_EVIDENCE |
| Vol-targeted trend 10% ann. | 1.9% | 0.16% | 0.32 | 1.4% | 3 | 1.7% | INSUFFICIENT_EVIDENCE |
| **Buy and hold (benchmark)** | **21.3%** | **1.65%** | **0.68** | 5.2% | 0 | 10.7% | INSUFFICIENT_EVIDENCE |

Read that table carefully. Cross-sectional momentum's figures are *identical*
to buy-and-hold's, to every decimal place. That is not a coincidence and not
a bug in the metrics: see the degeneracy finding below.

## Walk-forward — 38 folds, verified independently

Rolling, 1,260 train sessions / 252 out-of-sample / 252 step, bounded at
`validation_end` so it never reaches the test period. 38 folds covering
1976-02-03 → 2014-01-16; one final fold skipped with a stated reason (241
sessions remained, 252 needed).

I re-ran this myself through the corrected `volume_known=False` path rather
than accepting the delegated agent's numbers, which had used a fabricated
placeholder volume of 1,000,000,000 per bar. Every aggregate reproduced
exactly — the fabrication had put the participation cap at 10,000,000 shares,
far above a 50-share position, so it changed no result. The fix was about
honesty, not arithmetic.

Re-run in full on 2026-10-27. Three of the four rows reproduce exactly; the
zscore row does not and has been corrected in place.

| Strategy | Positive OOS folds | Consistency | Aggregate ret | Aggregate Sharpe | Trades | Evidence |
|---|---:|---:|---:|---:|---:|---|
| **buy_and_hold** | **26/38** | **68.42%** | **+24.8377%** | **0.6817** | 0 | INSUFFICIENT_EVIDENCE |
| ma_trend 50/200 | 24/38 | 63.16% | +17.0750% | 0.6163 | 22 | INSUFFICIENT_EVIDENCE |
| ma_trend 20/100 | 22/38 | 57.89% | +13.7834% | 0.5369 | 59 | SUFFICIENT |
| zscore mean reversion | **17/38** | **44.74%** | **-4.0116%** | **-0.2749** | **121** | SUFFICIENT |

Buy-and-hold wins on all three of consistency, return and Sharpe.

**The zscore row previously read 21/38, 55.3%, -3.8%, -0.26 and 132 trades.**
Those figures do not come out of the current code and the discrepancy is
recorded as F-23. The verdict is unchanged and in fact worse: zscore is now
positive in fewer than half its folds. zscore is the only strategy that trades
often enough for the round 5 risk fixes to change its order flow, which is the
most likely explanation.

Anchored (expanding-window) folds were also run, for the first time, and
produce identical out-of-sample results to the rolling ones for every strategy
in this table. See the diagnostics section and F-22: nothing refits per fold,
so the training window cannot influence the out-of-sample figure.

One older discrepancy, kept on the record: the delegated agent reported zscore
fold 28 as 1.69%, where the run at the time gave 0.169%. That looked like a
transcription slip rather than a different computation, which now reads
differently in light of F-23.

## Overfitting diagnostics

**All of these were re-run through the current code on 2026-10-27**, on the
honest `series_history` path (`volume_known=False`, no fabricated placeholder
volume). Both ma_trend variants and buy-and-hold reproduce the delegated
agent's figures to the decimal. **zscore mean reversion does not**, in any of
the three. The re-run numbers are below and are pinned in
`tests/test_diagnostics_verification.py`; the discrepancy is F-23.

**Cost sensitivity** (train window, slippage 0/5/10/25/50/100 bps, commission
0.005/share):

| Strategy | Breakeven slippage | Note |
|---|---|---|
| ma_trend 50/200 | none in range | 13.0869% at 0 bps → 10.6665% at 100 bps (reproduces) |
| ma_trend 20/100 | none in range | 9.5051% → 5.9151% (reproduces) |
| zscore mean reversion | **0 bps** | already negative with no costs at all. **-2.8768% → -8.0983%**, against the reported -2.55% → -7.90% |

The trend variants survive 100 bps, which is the one genuinely encouraging
result here. Mean reversion has no edge to erode. The sweep is also monotone
in cost at every level, which nothing previously asserted.

**Monte Carlo** (5,000 iterations, seed 20261026, bootstrap resampling of
realised trade returns, train window):

| Strategy | Trades | Terminal median | p05 | p95 | P(loss) |
|---|---:|---:|---:|---:|---:|
| ma_trend 50/200 | 18 | 112,392.89 | 103,146.98 | 123,849.12 | 1.16% |
| ma_trend 20/100 | 35 | 109,010.13 | 101,424.49 | 118,201.75 | 2.40% |
| zscore mean reversion | **63** | **96,939.62** | **92,901.76** | **100,331.77** | **92.80%** |

The two ma_trend rows reproduce exactly. zscore's reported row said 65 trades
and a median of 97,333.98. Note that the train table above already says 63
closed trades for zscore, so the report disagreed with itself before this
re-run.

Shuffling trade order cannot change terminal equity (it is the same sum), so
the spread comes from resampling with replacement. Confirmed on real data:
in shuffle mode the terminal median, p05 and p95 are all identical and equal
the actual terminal, while the drawdown quantiles still differ, because the
path varies and the sum does not.

**Parameter sensitivity**: both ma_trend variants come back `fragile=False`
on one-notch parameter moves, which reproduces the reported verdict. The
report never recorded the grid behind it, so the claim was not reproducible
from the report alone; the grid is now in the test
(`fast_window` 20/50/100, `slow_window` 100/200/300 for 50/200). zscore has no
positive base return, so the question does not arise for it, but one neighbour
does: **window=10 returns +0.7588% against a -3.177% base**, the only positive
zscore variant found anywhere in this project, and another trial against the
family-wise denominator.

**Anchored walk-forward, run here for the first time, is identical to rolling
out of sample.** On all four strategies, in every field: aggregate return,
Sharpe, max drawdown, trade count, fill count and per-fold out-of-sample
return. Only `train_start` and `train_sessions` differ, 1,260 fixed against
1,260 expanding to 10,584. Nothing refits per fold, so the training window
cannot influence the out-of-sample result; anchored costs about 4x the wall
clock for identical output. The consistency and aggregate figures are
therefore a time-stability measure on fixed parameters, not evidence about
re-selected ones. F-22.

**Bootstrap intervals on real returns, also run here for the first time.**
8,559 train sessions, 2,000 iterations, seed 20261026, 95%:

| Strategy | Ann. Sharpe | iid 95% | stationary (block 20) 95% |
|---|---:|---|---|
| ma_trend 50/200 | 0.44652159 | [0.1038, 0.8040] | [0.0771, 0.9057] |
| ma_trend 20/100 | 0.42667827 | [0.0823, 0.7929] | **[-0.0100, 0.9245]** |
| zscore mean reversion | -0.30146307 | [-0.6186, 0.0321] | [-0.6393, 0.0966] |
| **buy and hold** | 0.28446269 | **[-0.0643, 0.6293]** | **[-0.0550, 0.6662]** |

**The resampling scheme changes a verdict.** ma_trend 20/100's iid interval
excludes zero; the stationary block bootstrap, which keeps the serial
dependence that daily equity returns actually have, does not. The iid
interval over-claims, and it is the default. Buy-and-hold's own Sharpe
interval contains zero under both schemes, which is worth holding next to the
fact that it beat everything: the benchmark is not distinguishable from zero
on the train window either.

The floor behaves: a 14-session window reports no interval rather than a band
drawn around 14 points. Seed-reproducible, seed-sensitive, and the point
estimate is the metric itself rather than a resample.

**Multiple testing — now measured, and it is the strongest result here.**

Testing five strategies and reporting the best one is not a discovery. Run
against the real registry (15 recorded experiments, 5 distinct variants, 2,967
sessions in the test window):

| Window | Variants | Best Sharpe | E[best under null] | Deflated | Survives? |
|---|---:|---:|---:|---:|---|
| Train | 5 | 0.447 (vol-targeted trend) | 0.205 | 0.921 | **No** |
| Validation | 5 | 0.516 (xs momentum) | 0.377 | 0.669 | **No** |
| **Held-out test** | 5 | **0.676** (xs momentum) | **0.347** | **0.870** | **No** |

**Nothing in this project survives the multiple-testing correction.** The best
out-of-sample Sharpe observed anywhere is 0.676, and the expected best of five
random tries on this data is 0.347 — so the observed figure is not
distinguishable from having tried five things and kept the luckiest. It also
belongs to the degenerate momentum strategy that *is* buy-and-hold.

How sensitive is that to the trial count? For the same 0.676 over 2,967
sessions:

| Variants tried | E[best under null] | Deflated Sharpe | Significant |
|---:|---:|---:|---|
| 1 | 0.000 | 0.990 | Yes |
| 4 | 0.307 | 0.900 | No |
| 5 | 0.348 | 0.873 | No |
| 10 | 0.459 | 0.776 | No |
| 50 | 0.664 | 0.523 | No |

At four or more variants the best result in the project stops being
significant. Five were tried. The per-strategy view
(`multiple_testing_summary`) reports 1 variant each and no adjustment, which
is true but answers the wrong question: the denominator for "did any of the
things I tried beat the benchmark" is the number of things tried, which is
what `family_wise_summary` computes.

## Performance by market regime

Regimes are VIXCLS terciles, classified **point in time** with a 252-session
expanding warmup: each date is placed against the terciles of the
observations up to and including it, never against the whole history. A
boundary computed over all of 1990-2026 would move every historical label as
new data arrived, which is look-ahead bias dressed up as a regime definition.
9,050 labelled sessions, 1990-12-31 → 2026-10-23, distribution 3,366 low /
2,658 mid / 3,026 high.

Held-out test window (2015-2026), total return and Sharpe per regime:

| Strategy | Low vol (n=1088) | Mid vol (n=1137) | High vol (n=744) |
|---|---|---|---|
| MA trend 50/200 | +1.79% (0.85) | **-1.07% (-0.47)** | +1.18% (0.75) |
| Vol-targeted trend | +1.79% (0.85) | **-1.07% (-0.47)** | +1.18% (0.75) |
| Mean reversion | -0.06% (-0.22) | +0.39% (0.61) | +0.34% (0.38) |
| Cross-sectional momentum | +3.02% (0.52) | +4.97% (0.45) | **+12.15% (1.12)** |
| **Buy and hold** | +3.02% (0.52) | +4.97% (0.45) | **+12.15% (1.12)** |

Validation window (2005-2014) for comparison:

| Strategy | Low vol (n=1071) | Mid vol (n=697) | High vol (n=748) |
|---|---|---|---|
| MA trend 50/200 | +0.37% (0.18) | +1.39% (0.79) | +0.59% (0.27) |
| Mean reversion | -0.39% (-0.41) | +0.74% (0.91) | +0.44% (0.51) |
| **Buy and hold** | +1.83% (0.59) | +3.20% (1.25) | +0.16% (0.05) |

**This is where the trend strategies actually lose.** On the test window their
only negative regime is mid-volatility (-1.07%, Sharpe -0.47) — classic
whipsaw, where moves are large enough to trigger a crossover but not
persistent enough to pay for it. Meanwhile buy-and-hold earns most of its
test-window return in **high** volatility (+12.15%, Sharpe 1.12), which is
precisely when a trend filter has gone to cash. Trend following did not just
underperform; it was out of the market during the recoveries that produced the
benchmark's return.

Note the regime split flips between windows: buy-and-hold's worst regime in
2005-2014 was high volatility (+0.16%) and its best in 2015-2026 was high
volatility (+12.15%). A strategy tuned to one of those regimes' behaviour
would have been fitted to an artefact of the period, which is the argument for
not reading too much into either table.

## The real-OHLCV run — AAPL 2015-02-17 → 2017-02-16

This is the **only** run in which the gap model, liquidity breaker and
participation cap are actually exercised, because it is the only per-symbol
data with a real intraday range and real volume. The NASDAQCOM study cannot
test any of them.

This sentence previously also claimed the limit-fill logic. It did not:
`Signal` has no limit-price field and the engine constructs
`OrderType.MARKET` unconditionally, so **no backtest in this project can
produce a limit order at all**. The limit path is exercised separately below,
through `FillModel` and `PaperBroker` directly. F-21.

Data characteristics, measured: 506 bars, `volume_known=True`, volume from
11,475,900 to 162,206,300 (median 37,586,800), genuine high/low range on every
bar, adjusted/close ratio spanning 0.961474 to 1.000000 — about 4% of real
dividend adjustment over two years.

Strategy windows shortened to suit 506 sessions (20/60 instead of 50/200,
60/5 momentum) since the index parameters need more history than exists here.

| Strategy | Total ret | Sharpe | Max DD | Trades | Fills | Cap deployed | Evidence |
|---|---:|---:|---:|---:|---:|---:|---|
| MA trend 20/60 | 0.2% | 0.59 | 0.3% | 1 | 3 | 0.4% | INSUFFICIENT_EVIDENCE |
| Cross-sectional momentum 60/5 | 0.1% | 0.07 | 1.5% | 0 | 1 | 3.6% | INSUFFICIENT_EVIDENCE |
| Mean reversion z=2 / 20d | -0.2% | -0.18 | 0.7% | 2 | 4 | 0.3% | INSUFFICIENT_EVIDENCE |
| Vol-targeted trend 10% ann. | 0.2% | 0.59 | 0.3% | 1 | 3 | 0.4% | INSUFFICIENT_EVIDENCE |
| Buy and hold (benchmark) | 0.3% | 0.15 | 1.7% | 0 | 1 | 4.4% | INSUFFICIENT_EVIDENCE |

**Every row is INSUFFICIENT_EVIDENCE and none of these figures should be read
as performance.** 506 sessions is two years and the trade counts are 0 to 2.
The run's value is that it proves the OHLCV execution path works end to end on
real prices, not that it says anything about any strategy.

Risk rules that fired during these runs: `martingale_size_increase_after_loss`
437 times, `sustained_decline` once. The martingale control firing hundreds of
times on a two-year sample is itself informative: these strategies repeatedly
want to re-enter larger after a loss, and the control repeatedly refuses.

## Dividends: what every price-index figure above understates

The AAPL sample is the only series in the project with genuine adjusted
closes, so it is the only place this can be measured rather than argued. The
adjustment is real: **eight material ratio steps over the two years, one a
quarter** (May/Aug/Nov 2015, Feb/May/Aug/Nov 2016, Feb 2017), all positive,
which is AAPL's dividend schedule. The 491 sub-threshold steps are the
published `adj_close` being rounded to six decimals, not events.

Holding the series itself, no strategy and no costs, over 1.9986 years:

| | Cumulative | Annualised |
|---|---:|---:|
| Price return (close) | +5.882816% | +2.901391% |
| Total return (adj_close) | +10.125484% | +4.944156% |
| **Dividend contribution** | **+4.242668 pp** | **+2.042766 pp** |

Through the engine at the production 5% single-name cap, with real costs:

| Strategy | Price series | Total-return series |
|---|---:|---:|
| Buy and hold, total return | +0.297592% | +0.502028% |
| Buy and hold, Sharpe | 0.1455 | 0.2418 |
| MA trend 20/60, total return | +0.247100% | +0.662126% |

The price-only figure is **59% of the real one** and the Sharpe nearly halves.

**What that means for every NASDAQCOM result above.** `series_history` sets
`adj_close` equal to the close, because an index publishes no dividend, so
price return and total return are identical by construction. The engine also
fills and marks at `close` while strategies read adjusted closes only for
signals, so **nothing in this project ever earns a dividend**. Every return
and Sharpe in the index study is a price return reported as a return.

The size of that understatement is unknown here: the Nasdaq Composite's
dividend yield is in none of the data this project holds. The AAPL measurement
establishes that the mechanism is worth two points a year on one large-cap
name. It does **not** establish that it is worth two points on the index, and
it is not extrapolated to one.

Direction, though, is certain, and it cuts against the negative result rather
than for it: buy-and-hold is the strategy most understated by a missing
dividend, because it is invested the whole time. A correct total-return
benchmark would have been harder to beat, not easier.

## Limit orders on real intraday range

Never exercised against real highs and lows before. 505 bar transitions,
eleven limit offsets from -500 to +500 bps around the previous close, both
sides. All three branches fire at every offset, and **a fill never trades
through its own limit** across the whole grid, which is the clamp in
`_fill_price` doing its job: the limit is resolved first and slippage applied
after, so without it a 5 bps adverse move would push a fill past the price the
order was protecting.

At a limit set exactly at the previous close:

| Side | Fill at open | Fill at limit | No fill | Fills | Mean fill |
|---|---:|---:|---:|---:|---:|
| BUY | 251 | 158 | 96 | 409 | 112.9195 |
| SELL | 262 | 167 | 76 | 429 | 113.4028 |

Against a market order on the same bars: BUY fills 505 of 505 at 112.9641,
SELL 505 of 505 at 112.8485. So a limit buy pays **4.5 cents less and misses
19% of the bars**.

End to end through the paper broker, 199 sessions of a limit round trip (buy
50 bps under the previous close, sell 50 bps over it once held): 199 orders,
166 accepted, **33 refused by `sustained_decline`**, 105 fills, **60 expiries
where the limit was never reached**. Realised **-167.934215** over 52 round
trips, with a mean buy of 123.7146 against a mean sell of **123.5731**.

The sells average **below** the buys. The buy limit fills on days the price
fell and the sell limit on days it rose, so the side getting the better price
is systematically the wrong one. That is adverse selection, not costs, and it
is the kind of thing a limit-order study is for.

A first attempt submitted the same buy limit every session and had 53 of 59
orders refused by `martingale_averaging_down`. That proved the control works
rather than that the limit path does, and it is recorded because the second
shape is the one that tests what was meant.

## The cross-section at full width

The engine was run against the whole MarketParquet snapshot: **26,018 bars,
6,799 symbols, 4 trading days**, 2026-10-20 to 2026-10-23.

**The honest configuration produces nothing, and says so.** Cross-sectional
momentum at the baseline 126/21 with a 150-session minimum emits zero signals
over four sessions: zero orders, zero fills, zero risk events, final equity
unchanged at 100,000, and `INSUFFICIENT_EVIDENCE` naming both shortfalls
(0 closed trades against a minimum of 30, 4 observations against 252). It does
not rank on the three returns that happen to exist and report a 126-day
momentum figure, which is the failure that would have mattered.

**It completes at that width, now.** It did not before: a four-day run took
**461.231 seconds**, 99.8% of it inside one quantity cap that searched for its
answer one share at a time. The same run now takes **0.930 seconds** with
byte-identical output. See `RISK_GAUNTLET.md` round 6.

Driving real order flow across the full width (a one-day lookback, which four
sessions can satisfy, top 20) produces 85 orders, 36 fills and 41 risk events
in 0.930s. Still `INSUFFICIENT_EVIDENCE`: 16 closed trades against 30, 4
observations against 252. No cross-sectional claim is made or available.

## Two structural findings about the strategies themselves

### Cross-sectional momentum degenerates to buy-and-hold

On a one-symbol universe, ranking the cross-section and holding the top `n`
always holds that one symbol. There is no absolute-momentum filter, so the
strategy cannot decline to be invested. Its test-window figures are
byte-identical to buy-and-hold because it *is* buy-and-hold here.

This was first flagged by the strategies agent on the downtrend fixture (231
entries into a falling asset) and is confirmed on real data. It is not a bug
against the spec, which asked for relative ranking. It does mean the strategy
is untested as a strategy: evaluating it needs a real cross-section, which
needs the data in `DATA_COVERAGE.md`. Logged as F-04.

### Trend and vol-targeted trend produce identical results

Both read 13.0% / 0.45 Sharpe on train and 1.9% / 0.32 on test. The
vol-targeting layer scales a target weight that is then clamped by the 5%
single-name cap, so on this instrument the cap binds first and the volatility
scaling never takes effect. The two strategies are distinguishable in their
unit tests but not in this configuration. Logged as F-05.

## The 20-stock universe on real OHLCV (added 2026-10-28)

`python -m signalforge.research_cli --universe --benchmark SPY --source tiingo`
over the 20 names in `config/research.yaml`, Tiingo daily bars on the adjusted
basis, 1972-01-25 to 2026-10-27. The held-out test window was **not** run.

Train 1971-02-05 to 2004-12-31, dataset `89767552ae61`:

| Strategy | Total return | CAGR | Sharpe | Max DD | Trades | Capital deployed |
|---|---:|---:|---:|---:|---:|---:|
| MA trend 50/200 | 27.6% | 0.74% | 0.20 | 22.5% | 49 | 7.8% |
| Cross-sectional momentum 126/21 | 15.3% | 0.43% | 0.20 | 8.6% | 355 | 2.6% |
| Mean reversion z=2 / 20d | 26.6% | 0.72% | 0.33 | 7.6% | 550 | 3.5% |
| Vol-targeted trend 10% | 27.6% | 0.74% | 0.20 | 22.5% | 49 | 7.8% |
| Buy and hold SPY (benchmark) | 11.8% | 0.34% | 0.26 | 8.2% | 0 | 4.0% |

Validation 2005-01-01 to 2014-12-31, dataset `f1cc21909975`:

| Strategy | Total return | CAGR | Sharpe | Max DD | Trades | Capital deployed |
|---|---:|---:|---:|---:|---:|---:|
| MA trend 50/200 | 18.7% | 1.73% | 0.30 | 14.8% | 44 | 20.7% |
| Cross-sectional momentum 126/21 | 1.4% | 0.14% | 0.08 | 6.8% | 154 | 3.6% |
| Mean reversion z=2 / 20d | -8.1% | -0.84% | -0.32 | 11.7% | 164 | 3.9% |
| Vol-targeted trend 10% | 18.7% | 1.73% | 0.30 | 14.8% | 44 | 20.7% |
| Buy and hold SPY (benchmark) | 5.4% | 0.53% | 0.50 | 3.7% | 0 | 6.1% |

**Two bad data points flipped a 34-year result.** Before the CVX 1973 and
1981 splits were recorded (they are missing from Tiingo's adjusted close too),
the same run, dataset `db1856704111`, had MA trend 50/200 at **-1.1%** over the
train window on 23 trades. Each fake 50% crash put CVX below its 200-day
average and kept the trend rule out of it. Validation is unchanged because
both dates fall before 2005. This is why a raw jump beyond 1.4x with no
recorded split now refuses a paper run until a human resolves it.

Every figure in this section carries:

- **Survivorship bias.** The 20 names are today's surviving large caps.
- **Exposure mismatch.** The per-name strategies hold up to 5% per symbol
  (up to roughly 100% gross); the benchmark holds 5% in one ETF. MA trend's
  Sharpe (0.30) is below buy-and-hold's (0.50) over validation even though its
  return is higher.
- **Thin early coverage.** Most names are absent for much of the train window;
  META and TSLA have no bars in it.

## Promotion decisions

No strategy has been promoted. Nothing is running in paper trading.

The gate's verdict on the walk-forward evidence: both ma_trend variants fail
`risk_adjusted_return` against buy-and-hold's 0.68 Sharpe, and ma_trend 50/200
also fails the `evidence` check (22 closed trades against a 30 minimum).
Mean reversion fails on return and Sharpe. Promotion requires a named human
approver in addition to passing every check, and no check set passes.
