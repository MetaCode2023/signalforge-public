# Shut down the bot, keep the engine

**Verdict: No.** More data won't let a solo operator build a bot that beats the market. That holds for large U.S. stocks especially, and those are what SignalForge holds. **Recommendation: kill the "beat the market" goal now.** Freeze SignalForge, and point what's useful in it (data checks, honest testing, overfitting controls) at real-estate underwriting and investor data. In that market you know things other people don't. Put the money you might have traded into a zero-hours setup instead: index funds or direct indexing with tax-loss harvesting. Two reasons. First, SignalForge's own tests found no edge; that result is correct and matches the research. Second, at your tax bracket a short-term trading bot would need to beat buy-and-hold by about **5 points a year before tax** just to break even. Fewer than 1 in 5 professional fund managers beat the index at all in 2025, before taxes. This is decision support, not personalized investment advice.

## Your own platform already gave the answer

SignalForge was built carefully, which is why its negative result can be trusted.

| Test (NASDAQ Composite, 1971–2026) | Buy-and-hold | Best challenger |
|---|---|---|
| Walk-forward, positive folds | **26/38** | 24/38 (MA trend 50/200) |
| Walk-forward Sharpe | **0.68** | 0.62 |
| One-time held-out test, 2015–2026 | Beat or tied all 4 | Momentum tied only because it *was* buy-and-hold |
| Correcting for 5 strategies tried | — | **Nothing survives** (best Sharpe 0.676 vs 0.347 expected from luck alone) |

Source: SignalForge `STRATEGY_RESULTS.md`.

The test had blind spots, and they cut against the bot. The index data has no dividends, which understates buy-and-hold. The cross-section of individual stocks was never properly tested. The new data has its own problems:

- **Survivors only.** All 493 stocks are companies that still exist today, so backtests on them look better than reality.
- **Untriaged errors.** 473 of the 493 haven't been checked for split errors. Two missing Chevron splits were enough to flip a 34-year result.
- **No resale.** Tiingo's free tier is "internal use only," so you can't sell anything built on it.

Running the 493-stock cross-section would most likely produce a flattering, biased number rather than a real edge.

## Why "more data" doesn't create an edge

**Professionals lose too.** 83% of U.S. large-cap funds trailed the S&P 500 over 10 years, and 92.6% over 20 years ([SPIVA Mid-Year 2026](https://www.spglobal.com/spdji/en/documents/spiva/spiva-us-mid-year-2026.pdf)). Hedge-fund indexes returned about 3–6% a year over the last decade, against about 15% for the S&P 500 ([LambdaFin/UBS](https://www.lambdafin.com/articles/average-hedge-fund-return)).

**The famous exception doesn't help you.** Renaissance's Medallion fund made 39.9% net. It is closed to outsiders and capped at $10–15B. In 2020 Medallion made +76% while Renaissance's own funds open to outsiders lost 23–34% ([Institutional Investor](https://www.institutionalinvestor.com/article/2bswms7wco7as686o8ikg/portfolio/renaissances-medallion-fund-surged-76-in-2020-but-funds-open-to-outsiders-tanked)).

**Your kind of data is the most picked-over.** Machine learning finds only tiny predictability, about 0.3–0.7% R² a month ([Gu-Kelly-Xiu](https://www.aqr.com:443/-/media/AQR/Documents/Journal-Articles/Empirical-Asset-Pricing-in-Machine-Learning.pdf?sc_lang=en)). Most of the profit sits in microcaps and distressed stocks, and it fades after costs ([Avramov-Cheng-Metzker](https://papers.ssrn.com/abstract=3450322)). Published signals lose 58% of their return after publication. Signals built only on price and volume, which is what you have, lose the most ([McLean-Pontiff via CXO](https://www.cxoadvisory.com/big-ideas/effects-of-market-adaptation)).

**Volume of data isn't the same as evidence.** 4.1M bars of 493 correlated stocks amount to far fewer independent bets than the row count suggests. Across 888 crowd-built trading bots, backtest results explained only **1–2%** of live results, and the people who ran more backtests did worse live ([Quantopian study via CXO](https://www.cxoadvisory.com/big-ideas/in-sample-vs-out-of-sample-performance-of-888-trading-strategies)).

**Better data costs institutional money.** Useful alternative data typically runs $25k–$150k+ a year ([Institutional Investor/Neudata](https://www.institutionalinvestor.com/article/b1mf7j9918j9lg/How-Much-Are-Managers-Paying-for-Data)). The GPT-on-headlines signal sounds tempting, but its Sharpe ratio fell from 6.5 to 1.2 in about 2.5 years. It also stops being profitable at 20 bps of trading cost and needs a real-time news feed ([Lopez-Lira & Tang](https://arxiv.org/html/2304.07619v6)).

## The after-tax break-even math

Short-term gains are taxed as ordinary income. Buy-and-hold defers its gains and pays the lower long-term rate. Under the 2026 rates, the combined federal + 3.8% NIIT + Arizona's flat 2.5% looks like this ([NerdWallet](https://www.nerdwallet.com/taxes/learn/2026-tax-brackets), [Kiplinger](https://www.kiplinger.com/taxes/irs-updates-capital-gains-tax-thresholds), [IRS NIIT](https://www.irs.gov/taxtopics/tc559), [AZ 2.5%](https://www.breakeven.tax/brackets/2026/arizona)):

| Your bracket | Short-term / long-term rate | Pre-tax edge needed **just to tie** |
|---|---|---|
| 37% (top) | 43.3% / 26.3% | **4.9 pts/yr** |
| 32% | 38.3% / 26.3% | **3.7 pts/yr** |
| 24% | 26.5% / 17.5% | 2.3 pts/yr |

What a real edge would be worth after tax, per hour, assuming 150–300 hours a year of upkeep. This flatters the bot because it ignores the tax deferral it gives up:

| Capital | 1% edge | 3% edge | 5% edge |
|---|---|---|---|
| $250k | $5–9/hr | $14–28/hr | $24–47/hr |
| $1M | $19–38/hr | $57–113/hr | $94–189/hr |

**Including the lost deferral at the top bracket, even a real 5% edge on $1M comes out about −$100 a year compared with buy-and-hold.** Smaller edges lose thousands.

Model assumptions: 10% index return, 20-year deferral. A possible Arizona long-term capital gains subtraction is unverified, and it would widen buy-and-hold's lead. An IRA removes the tax problem, but contribution limits shrink the dollars back to trivial. Trading costs and the old $25k day-trading rule (eliminated in 2026) no longer matter much ([ThinkAdvisor](https://www.thinkadvisor.com/amp/2026/04/14/sec-approves-plan-removing-day-trading-limit-for-small-investors/)). Taxes and the scarcity of real alpha are what decide this.

## Options ranked, each with a kill rule

Several of these earn a **risk premium** (pay for bearing a risk) or act as a **diversifier**. Neither is alpha (skill-based outperformance). The table says which is which.

| # | Option | What the "edge" really is | Hours | Money / data | Kill criterion |
|---|---|---|---|---|---|
| 1 | **Clean shutdown + passive** | Tax alpha, not skill | ~0 ongoing | $100k+ taxable for direct indexing | n/a: this is the default |
| 2 | **Pivot engine to RE data** | Real information edge (proprietary) | 3–5/wk, time-boxed | Your own property-management/storage data + public permits/census | Below |
| 3 | Managed-futures sleeve | Diversifier / crisis hedge | 0 | ETF, ~0.85–0.9%/yr | Size it once; never trade it |
| 4 | Prediction-market sandbox | Possible small alpha, contested | 3–10/wk | $5–10k cap | Below |
| 5 | Microcap insider/PEAD bot | Marginal; net CI includes zero | 2–5/wk | Norgate $630–788/yr for survivor-free data | Don't start |

**1. Shutdown + passive.** Direct indexing with daily tax-loss harvesting has added about **0.5–0.7 pts a year after tax** in the first five years. That decays toward roughly 0.1 after about year six ([Wealthfront](https://www.wealthfront.com/blog/10-years-of-tax-loss-harvesting), [Alpha Architect](https://alphaarchitect.com/tax-loss-harvesting/)). The harvested losses may also offset gains from syndication exits or property sales; confirm that with your CPA. This beats any plausible bot result per hour.

**2. Real-estate data pivot.** This is where you actually have an edge. You have actual leases, delinquency and street-rate responses across about 182 units and 470 storage units. You also operate in secondary markets that CoStar ($1.79B of commercial real estate revenue in 2025) covers thinly ([CoStar FY2025](https://qnow.quantisnow.com/insight/costar-group-full-year-2025-revenue-increased-19-year-over-year-net-6409698)). It fits the investor-intelligence and market-radar work you already started.

The main payoff is **better acquisition underwriting**. One bad deal avoided likely beats years of plausible trading alpha. Selling it as a product is unproven: no data was found on demand for local CRE reports, Radius+ and Yardi Matrix pricing is quote-only, and the newsletter math is illustrative (at ~5% conversion, 1,000 readers ≈ $12–30k/yr) ([Simon Owens](https://simonowens.substack.com/p/whats-a-realistic-conversion-rate)).

*Kill rule:* within 90 days, either (a) the tool changes or prices at least one real underwriting decision, or (b) 25+ LPs or outside readers use it monthly. If neither happens, stop.

**3. Managed futures.** This is a hedge, not a way to beat the market. DBMF and KMLM made money in the early-2026 selloff ([24/7 Wall St](https://247wallst.com/investing/2026/04/10/the-3-managed-futures-etfs-that-made-money-while-the-sp-500-crashed/)). Trend following also fell **20%** from May 2024 to May 2025 ([Cambridge Associates](https://www.cambridgeassociates.com/insight/does-trend-followings-recent-struggle-signal-that-the-strategy-is-structurally-broken/)). Building your own version adds nothing over the ETF.

**4. Prediction markets (Kalshi).** This is the one trading niche with documented mispricing and mostly unsophisticated traders. But **0.1% of accounts took about two-thirds of Polymarket profits**, and those accounts are increasingly organized teams ([WSJ via Hello Rookie](https://hellorookie.com/?p=55460)). Kalshi's ~3.5% fee at 50¢ is larger than the documented longshot bias ([Allium](https://allium.so/blog/kalshi-fees-why-the-cost-peaks-near-50-cents/), [Cardozo et al.](https://arxiv.org/pdf/2609.12878)). Federal appeals courts have split on whether these markets are legal ([JD Supra](https://www.jdsupra.com/legalnews/kalshiex-llc-v-flaherty-third-circuit-4581987/)). Only consider this if trading itself is the hobby.

*Kill rule:* 90 days, $5k cap, maker (limit) orders only. Stop if P&L after fees is ≤ 0, or if it takes more than 5 hours a week.

**5. Microcap insider buys / earnings drift.** The most recent study calls the insider-buy signal "economically marginal": about 2.2% net per trade, with a confidence interval that includes zero ([Zhao 2026](https://arxiv.org/pdf/2602.06198)). Earnings drift outside microcaps has been gone since about 2006 ([UCLA Anderson](https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again/)). Testing it properly would need new survivor-free data. Skip it.

**Avoid outright:** intraday or HFT-style trading, leveraged volatility selling (LJM lost about 80% in 48 hours; [autopsy](https://hedgefundalpha.com/news/ljm-the-autopsy)), crypto funding/basis (now roughly a T-bill yield plus exchange risk; [BitMEX](https://www.bitmex.com/blog/2025q3-derivatives-report)), and LLM-on-news.

## What shutting down means for the code and data

**Do:**
- **Freeze the repo.** Tag a final release, write a one-page "results and why we stopped" note, and archive it on GitHub.
- **Stop the daily Tiingo-to-D1 sync.** Keep the database read-only or delete it. The data is licensed for internal use only, so it has no resale value.
- **Port the parts that still pay off** into the RE engine and the company's books/underwriting: point-in-time data checks, quarantine rules, the held-out-test registry and deflated Sharpe (the correction for trying many strategies).
- **Use it as a case study.** Pitch it as "institutional-grade rigor that caught its own false positives," not as returns.

**Don't:**
- Rerun the held-out window.
- Loosen the risk caps.
- Point the engine at the 473 untriaged stocks "just to see."

That is how drift starts again.

## Conclusion

The project didn't fail. It got the right answer cheaply. You built something that refused to fool you, and it told you the edge isn't in public large-cap price data. Your information edge is in rents, vacancies and storage street rates in markets the big data vendors barely cover. Use the rigor there, and leave the stock market to index funds.
