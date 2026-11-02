# SIGNALFORGE — Kronos Trial

Generated 2026-10-31. Validation window only (2005-01-01 to 2014-12-31); the
held-out test window was not touched.

## The result, stated first

**Kronos has not been run.** The build environment could not reach Hugging
Face, so this report sets the bar instead: what two forecasters with no edge
at all score on the same quantities a Kronos demo puts on screen. A Kronos
trial is a number only when it beats these.

The headline: **a cone that looks calibrated and a confidence reading that
"tracks accuracy" both come free, and neither beats calling UP every time.**

## Why a trial at all

The Kronos demo reads 100 sampled 24-bar futures as: tight spread means
confident, paths that agree mean TRENDING, scattered paths mean CHOPPY. Each
of those is a testable claim about probability, and the test needs a null.

- `random_walk`: Gaussian log returns at the window's volatility, zero drift.
  It has no directional view by construction.
- `block_bootstrap`: the last 511 daily log returns resampled in 5-day
  blocks. It inherits the trailing drift, so it is a momentum forecaster in
  disguise: after a strong two years it puts most paths up.

## Results

8 symbols (SPY, QQQ, AAPL, MSFT, JPM, XOM, KO, WMT), horizon 24 sessions, 100
paths, context 512 bars, one forecast every 24 sessions (no overlap), seed
20261031, TRENDING at 65% path agreement.

| Forecaster | n (indep.) | 10-90 coverage | Mean CRPS | Hit rate | Up base rate | TRENDING share | Hit TRENDING | Hit CHOPPY | Lift |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| random_walk | 832 (832) | 82.9% | 0.0382 | 50.7% | 61.2% | 0.4% | 100.0% (n=3) | 50.5% | n/a (too few calls) |
| block_bootstrap | 832 (832) | 81.0% | 0.0379 | 54.9% | 61.2% | 26.4% | 58.6% (n=220) | 53.6% | +5.0% |

CRPS skill of the bootstrap over the random walk: 0.8%.

Hit rate by path agreement, block bootstrap:

| Agreement | n | Hit rate | 95% interval |
|---|---:|---:|---|
| 50-60% | 463 | 51.8% | 47.3%-56.4% |
| 60-70% | 274 | 58.0% | 52.1%-63.7% |
| 70-80% | 92 | 59.8% | 49.6%-69.2% |
| 80-100% | 3 | too few | |

### Reading it

1. **Coverage is free.** Both nulls land 81-83% of outcomes inside a band
   that claims 80%. A demo showing realized prices inside the cone shows
   that the cone is about as wide as recent volatility, nothing more.
2. **Confidence tracks accuracy, and it still does not beat a constant.**
   On this seed the bootstrap's hit rate climbs with agreement (51.8% to
   59.8%), which is the screenshot a demo would post, and every bucket sits
   under the 61.2% that calling UP on every window scores. Across four more
   seeds the top bucket moves between 60% and 65%, always inside its
   interval: never clearly above the always-up line. The +5.0 point "lift" of
   TRENDING over CHOPPY comes from CHOPPY calls being near coin flips, not
   from TRENDING being informative.
3. **The regime label carries no information.** When the bootstrap read
   TRENDING UP (209 calls) the window went up 59.3% of the time; when it read
   CHOPPY (612), 61.9%. Under seeds 1 to 4 the TRENDING UP figure is 60.8% to
   61.4% and CHOPPY 60.9% to 61.2%: the label and the base rate agree to
   within a point. A regime call that matches the base rate is not a call.
4. **The demo's rule loses to holding.** Through the real engine (signal at
   close, fill next bar, 5 bps slippage against the order, commissions, the
   5% single-name cap, the risk engine), "long only while TRENDING UP" took
   $100,000 to $110,689 (Sharpe 0.46). Buy-and-hold took it to $260,447
   (Sharpe 0.86). The random-walk version barely traded: $100,479.

## What Kronos has to do to pass

All four, on the same origins:

1. Mean CRPS below the bootstrap's 0.0379.
2. Windows it calls TRENDING UP rise more often than 61.2%, by more than the
   Wilson interval.
3. 10-90 coverage near 80% with a flat PIT histogram.
4. The TRENDING-only rule beats buy-and-hold's 0.86 Sharpe after costs.

## How to run it

On a machine that can import pandas (Kronos's `model` package needs it; the
build machine blocks it):

```bash
pip install torch einops huggingface_hub safetensors
huggingface-cli download NeoQuasar/Kronos-small --local-dir models/kronos-small
huggingface-cli download NeoQuasar/Kronos-Tokenizer-base --local-dir models/kronos-tokenizer-base
git clone https://github.com/shiyu-coder/Kronos models/kronos-src

python -m signalforge.forecast_cli kronos-check --model-dir models/kronos-small \
    --tokenizer-dir models/kronos-tokenizer-base --kronos-src models/kronos-src

python -m signalforge.forecast_cli trial --forecaster kronos,bootstrap,random-walk \
    --symbols SPY --window validation --source tiingo --model-dir models/kronos-small \
    --tokenizer-dir models/kronos-tokenizer-base --kronos-src models/kronos-src
```

`kronos-check` fails if the sampled paths collapse to one line, which is what
happens if anything calls upstream `predict()`: it averages its samples.
Expect hours on CPU for all eight symbols; start with SPY (104 forecasts).

## What every number above is, and is not

- **Data.** Tiingo dividend-adjusted closes from the D1 mirror, 2002-11-04 to
  2014-12-31, extracted as daily returns and verified against four D1
  checksums (count, sum, position-weighted sum, sum of squares) per symbol.
  Every daily move is under 26%, so the 1.4x corporate-action rule had
  nothing to refuse.
- **Close-only bars** in the engine run: open, high and low equal the close,
  volume unknown, so the liquidity breaker and participation cap were
  skipped, not passed.
- **Survivorship.** All eight names survived to 2026 and were picked in
  2026. That flatters buy-and-hold most, and AAPL alone carries much of its
  return.
- **Exposure.** The 5% single-name cap holds buy-and-hold to about 57% of
  capital on average and the TRENDING rule to about 10%, so the dollar gap
  is partly an exposure gap. Sharpe is the fairer comparison and the rule
  still loses it.
- **Seeds.** The table is seed 20261031. Seeds 1 to 4 were rerun for the
  bootstrap: coverage 80.2% to 81.5%, overall hit rate 54.9% to 57.3%
  (always below the 61.2% base rate), TRENDING UP windows rising 59.3% to
  61.4%. Individual buckets move by several points between seeds, which is
  why none of them is read as evidence on its own.

Aggregates: `reports/forecast_trial_2005-2014_nulls.json`. Per-forecast
detail is licensed-derived and lives under `data/forecast_trials/`
(gitignored).
