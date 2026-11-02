# SIGNALFORGE — Assay (copying insiders)

Written 2026-11-01. **No result on real data yet.** The SEC site is not
reachable from the build sandbox, so the first download and trial run on the
operator's machine (commands below). This page records the method and the
synthetic checks that show the study can tell an edge from the hindsight trap.

## The question

Copy-trading pitches say: find the traders who win, copy them. The stock
version is SEC Form 4. A company officer or director who buys their own
company's shares in the open market must report it within two business days,
and anyone can copy the trade after that. Does copying them beat the market?

The pitch's usual method is to pick the "winners" by how their trades turned
out, then show what copying those same trades would have made. That uses the
future. Assay runs the pitch's method and the honest one side by side.

## What is tested

Every copied trade enters at the close of the first trading day after the
filing date and exits 5, 21 or 63 sessions later (21 is the headline). Its
score is its return minus SPY's over the same days, after 5 bps each way.

| Strategy | Rule |
|---|---|
| Every insider buy | every qualifying purchase, one per company per day |
| 2+ insiders buying | two or more different insiders buying one company within 30 days |
| Smart, judged on the past | insiders with 3+ closed copied buys before this one, 60%+ winners, positive average |
| Smart, judged with hindsight | same thresholds on all their trades in the window, then copied over that window. **Uses the future.** |

Qualifying purchase: original Form 4, transaction code P (open market),
shares acquired, common stock, one reporting owner, not a 10b5-1 planned
trade, by an officer or director, at least $25,000.

Two fair coins decide whether anything was found:

- **Random stock, same days.** The warehouse holds only companies that
  survived to 2026, so nearly anything bought in it beats SPY. This measures
  how much that alone is worth.
- **Random insiders, same months.** As many purchases as a strategy made,
  drawn from all insider buys filed in the same months. A strategy that does
  not beat this has found nothing beyond "an insider bought".

## Synthetic checks (in the test suite)

A simulated market with a common factor, 60 stocks and 320 insiders, window
2008 onward, 500 random draws per coin (the CLI default). Reproduce with
`tests/insider_support.world(seed=7)` and `run(..., Params(draws=500))`.

**Insiders with no edge at all:**

| Strategy | Trades | Excess 21d | Winners | Book a year |
|---|---:|---:|---:|---:|
| Every insider buy | 1,730 | +0.88% | 53% | +17.1% |
| Smart, judged on the past | 501 | +1.08% | 55% | +16.4% |
| Smart, judged with hindsight | 648 | **+3.37%** | **73%** | **+53.9%** |

Honest picks against random insiders: p 0.24 (nothing found, correctly).
Hindsight picks: p < 0.01. A no-edge world produces a 73% win rate and a
54%-a-year book when the winners are chosen after the fact. The positive
"Every insider buy" figure is the simulated survivor drift; the random-stock
coin measures it (p 0.10).

**30% of insiders with a real edge** (an extra 6% over the month after their
buys, seed 11): honest picks make +4.71% a trade against +3.33% for every
insider buy, and beat random insiders with p < 0.01. The study can find a real
edge, not only refuse fake ones.

## How to run it

From the repo root on the trading computer:

```powershell
python -m signalforge.insider_cli download      # 36 quarterly SEC zips, 2006q1-2014q4; a few minutes
python -m signalforge.insider_cli build         # parse into data/insiders/ (gitignored)
python -m signalforge.insider_cli trial --window validation --source tiingo
python -m signalforge.dashboard --serve         # then open the Assay tab
```

`build` prints the funnel from transaction rows to purchases and every
reason a row was dropped. If it reports a missing column, the SEC format has
changed: nothing is used from that file and the message names the column.

## What every number will be, and is not

- **Survivorship.** Only companies alive in 2026 are in the warehouse. Their
  insiders' purchases at companies that later failed are missing, which
  flatters every strategy. The random-stock coin measures this; the
  random-insider coin compares like with like.
- **Ticker mapping.** Purchases are matched through the issuer's CIK to the
  ticker on its latest filing. Companies acquired or delisted before 2026 drop
  out. The funnel shows how many.
- **Filing time.** The data has a filing date, not a time, so entry waits for
  the next close. A published event study on this same data found about +0.5%
  after one session and +1.0% after five, with 21 and 63 sessions including
  zero ([QuantInsti](https://blog.quantinsti.com/sec-form-4-insider-trading-python-event-study/)).
  If that holds here, most of the edge is gone before a copier can act.
- **The book is idealised.** Equal weight across live copies, rebalanced
  daily, cash when there are none. Its alpha and t-statistic use only days
  with positions, which avoids the overlap that inflates per-trade averages.
- **Validation window only** (2006 to 2014 in practice). The test window is
  untouched.

Source: [SEC Insider Transactions Data Sets](https://www.sec.gov/dera/data/form-345),
[readme](https://www.sec.gov/files/insider_transactions_readme.pdf).
