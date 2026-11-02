# Assay verdict (DRAFT, validation window only)

Written 2026-11-02 overnight from the first real-data run. Draft for the
operator and the developer to review; nothing here touches the test window.

## One paragraph

On 2006 to 2014 SEC Form 4 data, copying every officer or director open-market
buy of $25,000+ earned +1.11% over SPY in the 21 sessions after the filing,
against +0.56% for a random warehouse stock on the same days (p 0.002). That
headline is real but narrow: almost all of it comes from 2008 and 2009, the
typical copied trade loses a little (median -0.03%), and the average is carried
by a small number of big winners. In 2010 to 2014 the edge over a random stock
is +0.03% (p 0.42), which is nothing. The filters that try to be clever
(clusters of insiders, insiders with a good past record) did not beat random
insider buys.

## The validation run

Source: `reports/insider_trials/assay-2005-01-01-2014-12-31.json`, seed
20261101, 500 draws per null, tiingo prices, 5 bps each way.

| Strategy | n | Excess 21d | Hit rate | vs its fair coin | Read |
|---|---:|---:|---:|---|---|
| Every insider buy | 3,076 | +1.11% | 50% | random stock p 0.002 | edge, see below |
| 2+ insiders buying | 534 | +1.15% | 49% | random insiders p 0.75 | nothing extra |
| Smart, judged on the past | 251 | +1.91% | 51% | random insiders p 0.20 | not proven, small n |
| Smart, judged with hindsight | 605 | +5.08% | 75% | random insiders p 0.002 | the cheat, as designed |

The hindsight arm behaves exactly as the synthetic checks predicted: choosing
winners after the fact turns no edge into a 75% win rate and a +45% a year
book. That is the copy-trading pitch, measured.

## Sensitivity of "Every insider buy" (same data, same parameters)

No new parameters were tried; these are slices of the one pre-registered arm.
Each row recomputes the random-stock coin on its own trades.

| Slice | n | Excess 21d | Median | Random stock | Edge | p |
|---|---:|---:|---:|---:|---:|---:|
| All (headline) | 3,076 | +1.11% | -0.03% | +0.56% | +0.55% | 0.002 |
| 2006 | 329 | -0.20% | -0.23% | +0.19% | -0.40% | 0.88 |
| 2007 | 382 | -0.38% | -0.88% | +0.01% | -0.39% | 0.87 |
| 2008 | 597 | +2.29% | +0.29% | +0.81% | +1.47% | 0.002 |
| 2009 | 329 | +5.22% | +1.49% | +1.90% | +3.33% | 0.002 |
| 2010 | 304 | +0.79% | +0.09% | +0.73% | +0.06% | 0.43 |
| 2011 | 376 | -0.05% | -0.27% | +0.31% | -0.36% | 0.85 |
| 2012 | 304 | +0.55% | -0.18% | +0.12% | +0.43% | 0.16 |
| 2013 | 267 | +0.93% | +0.45% | +0.27% | +0.66% | 0.03 |
| 2014 | 188 | -0.44% | -0.40% | +0.28% | -0.72% | 0.97 |
| 2006-2009 | 1,637 | +1.75% | +0.04% | +0.73% | +1.02% | 0.002 |
| 2010-2014 | 1,439 | +0.38% | -0.06% | +0.36% | +0.03% | 0.42 |
| Drop 5 most-bought companies | 2,816 | +1.16% | -0.01% | +0.54% | +0.62% | 0.002 |
| Drop 25 most-bought companies | 2,273 | +1.15% | +0.22% | +0.53% | +0.61% | 0.002 |
| First buy per company only | 371 | +0.67% | -0.43% | +0.31% | +0.36% | 0.20 |
| Drop top and bottom 1% outcomes | 3,014 | +0.70% | -0.03% | +0.55% | +0.15% | 0.19 |

What the table says:

- **It is a crash-rebound effect.** Insiders who bought into the 2008 to 2009
  collapse were right, and copying them paid. Outside those two years the edge
  is zero or negative in six of seven years.
- **Not a few names.** Dropping the 5, 10 or 25 most-bought companies leaves
  the edge unchanged, so no single repeat buyer is driving it.
- **Fat right tail.** Trimming the best and worst 1% of outcomes cuts the
  edge from +0.55% to +0.15% (p 0.19). The trimmed rows trim only the
  strategy, not the coin, so they understate the edge somewhat; treat them as
  a direction, not a number.

## Limits the number carries

- **Coverage is thin.** 176,450 qualifying purchases, but only 5,895 (3.3%) are
  at companies in the price warehouse, and 3,076 trades at 372 companies make
  it to the end. The warehouse holds only companies alive in 2026, which
  flatters every arm; the random-stock coin corrects for that, the coverage
  gap it cannot.
- **Entry waits a full session** after the filing date. Published work finds
  most of the reaction in the first day, which this study cannot capture.
- **Validation only.** The test window has not been read.

## Should the holdout be spent on "Every insider buy"?

Recommendation: **not on the unconditional rule.** The test window in
`config/research.yaml` is 2015-01-01 to 2026-10-23, so the 2015 to 2026 SEC
quarters downloaded overnight are the holdout's own data. (Only filings were
counted; no holdout prices or returns were read.) The window holds two real
drawdowns, March 2020 and 2022, so a crash-conditional rule can be tested on
it, though with few events.

1. **Pre-register a regime rule before anything reads the holdout.** The
   validation evidence says "insider buys pay after a crash". Write the rule
   down from convention, not from tuning on validation: for example, copy
   only purchases filed while SPY closes 20% or more below its prior 252-day
   high (the usual bear-market line). Fix the horizon (21 sessions), the
   costs and the coins as they are now. Register it, then spend the holdout
   once on it and the unconditional rule side by side.
2. **Widen price coverage first if possible.** The warehouse holds 493
   symbols, so under 5% of holdout purchases can be scored (4,694 officer or
   director buys at 416 companies). Crash-period events will be a few hundred
   at most. More symbols, ideally including delisted companies, make the one
   shot worth more.

There is no command for it yet. `research_cli --run-holdout` spends the test
window for the price strategies only; it has no insider path, and
`insider_cli trial` refuses `--window test` by design. Spending the holdout
on Assay needs a one-shot insider holdout path built first (same
`HoldoutViolation` guard as research_cli). That build, and the decision to
use it, belong to the operator and the developer.
