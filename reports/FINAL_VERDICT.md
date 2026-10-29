# SIGNALFORGE — Final Verdict

Written 2026-10-29. Decision: **freeze the "beat the market" goal.**

## The question

Can a data-heavy trading bot, built and run by one person, beat buy-and-hold
U.S. equities? If not, stop spending time on it.

## The answer: no

**This platform answered it first.** On 55 years of NASDAQ Composite closes,
none of the four baseline strategies beat buy-and-hold:

| Test | Buy-and-hold | Best challenger |
|---|---|---|
| Walk-forward, positive folds (of 38) | **26** | 24 (MA trend 50/200) |
| Walk-forward aggregate Sharpe | **0.68** | 0.62 |
| Held-out test 2015–2026 (one look) | beat or tied every strategy | momentum tied only because, on one instrument, it *is* buy-and-hold |

Details: `STRATEGY_RESULTS.md`. Every known blind spot (no dividends in the
index, a survivor-only stock universe) flatters the strategies, not the
benchmark, so correcting them would widen the gap.

**The outside evidence agrees.** Full report with sources:
`RESEARCH_VIABILITY_2026-10-29.md`. In short:

- 92.6% of U.S. large-cap funds trailed the S&P 500 over 20 years (SPIVA
  mid-2026).
- Machine-learning return predictability is small and concentrated in
  microcaps; published signals lose about 58% of their return after
  publication, and pure price/volume signals (what this platform has) decay
  most.
- Across 888 crowd-built algorithms, backtest results explained 1–2% of live
  results.
- After tax at a high bracket, a frequently trading strategy needs roughly 4–5
  points a year of pre-tax edge just to tie buy-and-hold.

## What this does not mean

The platform did its job. It was built to refuse to fool its owner, and it
refused. The negative result is the product.

## What is frozen

- No new strategies, no new symbols, no widening of the universe.
- The held-out window stays spent. It is never re-run.
- Risk caps are not loosened.
- The code stays green and reproducible; nothing is deleted.

## What carries forward

The parts that paid off move to real-estate underwriting, where the company has
an information edge (its own rents, occupancy, delinquency and expenses):

- base rates and evidence labels instead of point estimates;
- refusing a verdict on too few observations;
- point-in-time thinking and holdout discipline for any screen or forecast;
- every number reproducible from a stated query.

That work lives in the `xray/` package on the `pivot/underwriting-xray`
branch.

## Decisions left to the owner

These change running systems or delete things, so they are not done here:

1. **Stop the daily Tiingo collection and D1 mirror sync** in
   `scripts/signalforge-daily.cmd` / Windows Task Scheduler. The data is
   licensed for internal use only and has no further research use.
2. **Keep or delete** the `signalforge-market-data` D1 database (~1.2 GB).
3. **Archive the GitHub repo** once the pivot branch has been moved or merged
   (archiving makes it read-only).

## If this is ever reopened

Only with all three: survivorship-bias-free data (e.g. Norgate, ~$630–790/yr),
a hypothesis written down and committed before any data is looked at, and a
fresh held-out window. Anything less repeats this result more slowly.
