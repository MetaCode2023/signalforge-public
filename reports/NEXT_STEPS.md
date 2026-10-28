# SIGNALFORGE — Next Steps

Generated 2026-10-26, revised 2026-10-27 after the overnight session.
Ordered by what unlocks the most, not by effort.

Two items need Austin before anything else can move. Everything after them is
work I can do without asking.

## Needs Austin first

### 0. Update 2026-10-28
- **Item 1 is DONE.** The Tiingo key was issued; 493 symbols of daily OHLCV
  are in the warehouse (see `DATA_COVERAGE.md`).
- **Phase 2 is built** (commits f4e38cc, 939bf7e, a42bf58, b9b3581): the paper
  broker books splits, spinoff adjustments and dividends on raw prices,
  refuses stale prices and unexplained jumps, and runs stock books with
  `paper_cli --symbols` / `--universe`.
- **Needs Austin:** approve a strategy for the stock book
  (`paper_cli --universe --approve <strategy>`); rotate the Tiingo key, which
  was pasted into a chat session; triage the 12 "review" jumps before widening
  the universe beyond the 20 names.

### 1. Decide on a free market-data key (DONE 2026-10-28)
**Blocks:** F-01, F-06, and the entire cross-sectional half of the research
programme.

Per-symbol daily OHLCV with adjusted closes, splits and dividends is the one
missing ingredient. No keyless, terms-clean source provides it. The Tiingo
adapter is written and fixture-tested; the moment `TIINGO_API_KEY` exists,
ingestion runs and the 23-name universe becomes real.

It is free and takes no payment details, but it creates an account in your
name, so I have not done it. Alpha Vantage is the fallback at 25 requests/day
(20 days to fill 500 symbols) versus Tiingo's ~1000/day.

**If you would rather not:** say so and I will write up what the system can
and cannot conclude from index-level data alone, and stop presenting the
universe in `config/research.yaml` as an intention.

### 2. Confirm or replace the promotion thresholds
**Blocks:** F-12, and therefore any future promotion.

`PromotionCriteria` currently requires: walk-forward consistency ≥ 0.60, at
least 3 folds, max drawdown ≤ 1.25× baseline + 0.01, and Sharpe strictly above
baseline. A delegated agent chose those numbers. They decide what would ever
be allowed to trade paper money, so they should be yours.

## I can do these without asking

### 3. Resolve the capital-dilution question (F-03, F-05)
Add a single-instrument research profile whose single-name cap equals the
intended allocation, so portfolio CAGR and Sharpe read directly instead of
needing the `capDep` column as a decoder — and so the vol-targeting layer
actually takes effect instead of being clamped away. **The production risk
config stays as it is**; this would be a separate, clearly-labelled research
profile, not a loosened limit.

### 4. Give cross-sectional momentum an absolute-momentum option (F-04)
A `require_positive_return` parameter so it can stay flat rather than always
holding the least-bad name. Needs your word before it goes near a real
universe, since it changes the strategy's definition.

### 5. Stand up a scheduled paper-trading loop (F-15)
`PaperBroker` is driven explicitly today. A daily runner would open a session,
settle pending orders, ask the active strategy for signals, and write a
statement — with the active strategy still requiring explicit promotion, so
the loop cannot start trading something that was never approved.

### 6. Close the incremental-ingest blind spot (F-07)
Make the jump, stale-price and adjustment rules read the prior stored bar
rather than only the current batch. Harmless today, real the moment a daily
pull starts.

### 7. DONE 2026-10-26 — multiple-testing correction on the real registry (F-08)
Run, and it strengthened the negative result: nothing in the project survives
it. Best out-of-sample Sharpe 0.676 against an expected best-of-five under the
null of 0.347.

### 8. Housekeeping (F-14, F-16)
F-13 is done. Still open: document the two trade-count definitions at both
call sites, and type the dashboard's Learning Lab query against the real
registry schema.

### 9. Decide whether a strategy should be able to ask for a limit order (F-21)
`Signal` has no limit-price field and both drivers hardcode
`OrderType.MARKET`, so no backtest can produce a limit order even though the
limit model is tested and now proven on real intraday range. The measured
trade-off is not small: a limit buy at the previous close pays 4.5 cents less
and misses 19% of bars, and a naive limit round trip loses money through
adverse selection rather than through costs. Adding the field is small; the
question is whether limit-order research is wanted at all, because the answer
changes what the strategies are.

### 10. Re-run the zscore diagnostics into the reports, or delete them (F-23)
Its cost sweep, Monte Carlo and walk-forward figures in
`STRATEGY_RESULTS.md` were the delegated agent's and did not reproduce; the
tables are now corrected to today's measured values. The remaining work is to
pin them in a test like ma_trend's, which needs either patience (248s rolling,
403s anchored) or a faster path.

## Deliberately not next

- **Cloudflare migration.** The brief defers it, and there is nothing worth
  deploying until there is a result worth looking at remotely.
- **Machine learning.** The brief is explicit: find out whether straightforward
  strategies survive realistic testing first. They did not beat buy-and-hold,
  so adding model complexity now would be fitting noise with more parameters.
- **Any move toward live execution.** The honest read of
  `STRATEGY_RESULTS.md` is that there is no edge to execute. The structural
  no-live-path guards should stay exactly as they are.

## The thing worth saying plainly

The build works, is tested, and has found thirty-three real defects in its own
foundations across six adversarial rounds. What it has not found is a strategy. Four baselines ran against a
55-year index history and every one underperformed buy-and-hold on the held-out
window; two of them turned out to *be* buy-and-hold in disguise.

That is a legitimate research outcome, and the system behaved correctly in
producing it: the benchmark was fixed when it was caught returning a
suspiciously round 0.0%, the hold-out was spent once and then defended, and
nothing was promoted. The most valuable next step is better data, not better
strategies — with the data that exists, no honest test can distinguish a real
edge from noise.

Two things learned overnight sharpen that. The negative result survives a
stricter test than it was given: under a stationary block bootstrap that keeps
the serial dependence daily returns actually have, not one strategy's Sharpe
is distinguishable from zero on the train window, and neither is
buy-and-hold's. And the one correction that would move the numbers moves them
against the result rather than for it: nothing in this project earns a
dividend, and buy-and-hold is the strategy a missing dividend understates
most, so a correct total-return benchmark would have been harder to beat.
