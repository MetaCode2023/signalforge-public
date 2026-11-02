# Overnight log, 2026-11-01 to 11-02 (paper only)

Machine: operator's main PC (4 cores, CPU only). Repo `.venv` Python 3.12.

## Done

- **Kronos setup.** torch 2.15 (CPU), pandas 3.1 and friends installed into
  `.venv`. Weights for Kronos-small and the Kronos-Tokenizer-base downloaded,
  upstream source cloned to `models/kronos-src` (gitignored).
  `kronos-check OK: 100 paths x 24 bars in 673.7s; 41/100 up, spread 4.0%`.
- **Kronos SPY trial started** 2026-11-01 22:01 (America/Phoenix), the
  pre-registered parameters unchanged. At about 674 s a forecast for 104
  forecasts it needs about 19.5 hours, finishing around 17:30 to 20:00 Monday.
- **Assay validation run**: 36 quarters (2006q1 to 2014q4), 3,779,598 rows to
  176,450 purchases, trial done. Scores pushed to `results-nov-2`.
- **Assay sensitivity** of "Every insider buy" by year, by company
  concentration and by outcome trimming: `ASSAY_VERDICT_DRAFT.md`. Headline:
  the edge is a 2008 to 2009 effect; 2010 to 2014 shows +0.03% over a random
  stock (p 0.42).
- **SEC 2015q1 to 2026q3 downloaded and built**, no code change needed
  (`download --start/--end`). 47 of 47 quarters, 3,574,121 rows to 152,193
  purchases (2015-01-02 to 2026-09-30); 4,779 planned 10b5-1 trades dropped.
  Built to `data/insiders/2015-2026q3/` (gitignored), separate from the
  validation inputs. **No trial was run on these years.**
- **Coverage measured for 2015+**: 6,582 purchases (4.3%) match the 493-symbol
  price warehouse; 4,694 officer or director buys of $25,000+ at 416 companies.
  139,156 dropped as "company not in the price warehouse", 6,455 as "ticker now
  belongs to another company".

## Waiting on a decision

1. **Holdout.** Not spent. The draft recommends against spending it on the
   unconditional rule, and notes there is no insider holdout path yet
   (`research_cli --run-holdout` covers price strategies only).
2. **2015+ is the holdout.** Correction: the registered test window is
   2015-01-01 to 2026-10-23, so this data belongs to the holdout. Only
   filings were counted; no holdout prices or returns were read. It includes
   the 2020 and 2022 drawdowns, so a pre-registered crash rule is testable.
3. **Price coverage.** The 493-symbol warehouse is the binding limit (under 5%
   of purchases scored). Adding more Tiingo symbols, ideally including
   delisted ones, would do more for Assay than any filter.

## Failures

- First Assay attempt used system Python (no duckdb); rerun in `.venv`.
- A dependency install into system Python was stopped and redone in `.venv`.

## Test suite

- Full pytest run in `.venv` on 2026-11-01: exit 0, no failures or errors (run alongside Kronos, so slow).
