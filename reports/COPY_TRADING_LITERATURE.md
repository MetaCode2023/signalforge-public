# Copy-Trading Signals: Literature and Practitioner Review

Compiled 2026-11-01 for SignalForge (paper-only research). Scope: corporate insiders (Form 4), US Congress (STOCK Act PTRs), and 13F / 13D fund cloning.

**How to read this file.** Every claim is tagged with where it came from:

- **[verified]**: I read the primary source (paper PDF, SEC page, or the data file itself) during this session.
- **[abstract]**: confirmed from the abstract or an index listing (RePEc, NBER page), not the full tables.
- **[memory]**: from prior knowledge and not re-checked this session. Check these before pre-registering anything that depends on them.

Peer-reviewed results, working papers, and marketing claims (ETF sponsors, Unusual Whales reports, vendor whitepapers) are kept separate.

---

## 0. Where our result sits

Our first result was Form 4 open-market purchases by officers and directors, $25k or more, 2006 to 2014, entering at the next close after filing. It earned +1.11% excess over 21 sessions against +0.56% for a random survivor on the same days. That edge was almost entirely 2008 to 2009, and the 2010 to 2014 edge was +0.03%. This matches the literature closely on four points:

1. **Insider buying is contrarian.** Insiders buy after price declines. Seyhun (1988) and Lakonishok and Lee (2001) found that aggregate insider buying predicts market returns. A crash is when insider buying is heaviest and when beaten-down, high-beta stocks rebound hardest. Benchmarking against SPY plus a random survivor does not remove a beta or reversal tilt. Part of our 2008 to 2009 edge may be beta and short-term reversal rather than private information. Use a beta-matched or characteristic-matched null before crediting any crash effect.
2. **The informed part of insider buying is a minority subset.** Cohen, Malloy and Pomorski (2012) found that about 64% of insider purchases are "routine" and carry no information. A filter on purchase size and role does not remove that routine buying.
3. **The post-2010 decline has already been documented.** Gangopadhyay and Yook (2022) found that profits on opportunistic purchases fell significantly after Dodd-Frank. Our 2010 to 2014 null is consistent with that.
4. **Our cluster null contradicts part of the literature.** Alldredge and Blank (2019) found clustered purchases earn more than 2% the next month. Their cluster is the same direction, same stock, on the same or consecutive trading days. Our "2+ insiders" rule is probably wider than that, so the rule definitions differ.

---

## 1. Corporate insider trading (Form 4)

### 1.1 Foundational results

| Paper | Sample | Key finding | Survived? |
|---|---|---|---|
| Seyhun, "Insiders' profits, costs of trading, and market efficiency," *J. Financial Economics* 16 (1986) [memory] | 1975 to 1981 | Insiders earn abnormal returns. Outsiders who imitate them after public disclosure earn little once trading costs are counted. Higher-ranked insiders (top executives, chairs) are more informed than officers or directors further down. | This set the template that "outsider profit after costs is small." |
| Rozeff and Zaman, "Market efficiency and insider trading: new evidence," *J. Business* 61 (1988) [memory] | 1973 to 1982 | Most outsider profits disappear after adjusting for size, earnings-to-price and a 2% round-trip cost. | The size adjustment matters: much of the raw edge is a small-cap effect. |
| Seyhun, "The information content of aggregate insider trading," *J. Business* 61 (1988) [memory] | 1975 to 1981 | Net insider buying across all firms predicts subsequent market returns. | Relevant to the crash result. This is market timing, and benchmarking against SPY strips it out. |
| Seyhun, "Overreaction or fundamentals: some lessons from insiders' response to the market crash of 1987," *J. Finance* 45 (1990) [memory] | Oct 1987 | Insiders bought in record amounts after the crash, especially stocks that fell most. This is consistent with them reading the drop as overreaction. | This is the closest US analogue to our 2008 to 2009 finding, and it is only one episode. |
| Lakonishok and Lee, "Are insider trades informative?" *Rev. Financial Studies* 14(1):79 to 111 (2001), [RePEc](https://ideas.repec.org/a/oup/rfinst/v14y2001i1p79-111.html) [abstract] | NYSE, AMEX and Nasdaq, 1975 to 1995 | There is very little price movement when the trade happens or when it is filed. Purchases predict returns and sales do not. The predictability is driven by small firms. Insiders as a group are contrarian, and they forecast market moves better than a simple contrarian rule does. | The small-firm concentration has held up in later work. |
| Jeng, Metrick and Zeckhauser, "Estimating the returns to insider trading: a performance-evaluation perspective," *Rev. Economics and Statistics* 85(2) (2003), [NBER w6913](https://www.nber.org/papers/w6913) [abstract + CMP citation] | 1975 to 1996 | A calendar-time purchase portfolio earns more than 6% abnormal per year (about 40 to 50 bp/month). Sales earn nothing. About a quarter of the purchase return comes in the first 5 days after the trade, and about half within the first month. | The front-loading is why speed of entry after the filing matters. |
| Cziraki and Gider, "The dollar profits to insider trading," *Rev. Finance* 25 (2021) [abstract via CLS Blue Sky summary] | US | The median insider earns about $464 a year in abnormal profit. Non-routine insiders earn higher returns, but not higher dollar profits. | Returns can be "significant" while the economic value is tiny. |

### 1.2 Cohen, Malloy and Pomorski (2012): routine vs opportunistic (KEY)

**Citation.** Cohen, L., Malloy, C., and Pomorski, L. "Decoding Inside Information." *Journal of Finance* 67(3):1009 to 1043 (2012). Working paper: [NBER w16454](https://www.nber.org/papers/w16454) (2010); [SSRN 1692517](https://papers.ssrn.com/abstract=1692517). [verified: definitions read from the NBER PDF, pp. 10 to 17]

**Data.** Thomson Reuters insider filings, January 1986 to December 2007. Open-market purchases and sales only; option exercises and private transactions are excluded. Merged with CRSP and Compustat. The median trade-to-report delay was 3 days.

**Exact definition (pp. 11 to 12, quoted):**
- Eligibility: "We require an insider to make at least one trade in each of the three preceding years in order to define her as either an opportunistic or a routine trader."
- Routine: "we define a routine trader as an insider who placed a trade in the same calendar month for at least three consecutive years."
- Opportunistic: "everyone else," meaning eligible insiders with no same-month pattern.
- Timing: insiders "are classified at the beginning of each calendar year, based on their past history of trades." Every trade later that year by a routine trader is a routine trade, and every trade by an opportunistic trader is an opportunistic trade.
- Footnote 9: back-windows of 1 to 5 years give similar results. Three years is the headline choice.
- An alternative trade-level rule (Appendix Table A3) applies the label only to trades in the routine month itself. A trader who is routine in January has only his January trades labeled routine. The buy spread is similar, at 94 bp.

**Ambiguities to pin down before pre-registering.** (a) The rule counts any open-market trade, buy or sell, toward the pattern. Our version should use buys and sells (transaction codes P and S) and say so explicitly. (b) An insider with fewer than 3 prior years of trades is unclassified and drops out. That is about two-thirds of all insider trades, and the classified sample tilts toward larger firms. (c) "Year" means calendar year. (d) Because our Form 4 data starts in 2006, the first year we can classify is 2009.

**Results.**
- Regression, next-month return: opportunistic buys add +90 bp (t=4.64) and routine buys add +14 bp (t=0.81).
- Portfolios, held for the calendar month after the trade and value-weighted: opportunistic buys earn about 82 bp/month abnormal, and routine trades earn roughly zero.
- About 64% of purchases are routine. The returns to opportunistic trades keep rising for about 6 months with no reversal.
- The result holds for both large and small stocks.

**Survival.** Gangopadhyay and Yook, "Profits to opportunistic insider trading before and after the Dodd-Frank act of 2010," *J. Financial Reporting and Accounting* (2022), [RePEc](https://ideas.repec.org/a/eme/jfrcpp/jfrc-02-2021-0018.html) [abstract]. They use the CMP classification. Opportunistic purchases were highly profitable before Dodd-Frank and significantly less profitable afterward. This is one study, in a lower-tier journal, and I read only the abstract. It is still the most direct prior evidence for our 2010 to 2014 null.

### 1.3 Other filters

- **Ali and Hirshleifer, "Opportunism as a managerial trait: predicting insider trading profits and misconduct," *J. Financial Economics* 126 (2017)** [abstract via search summary]. This paper defines "opportunistic" differently from CMP. It classifies insiders by how profitable their past trades were just before quarterly earnings announcements. Those insiders' later trades earn four-factor alphas above 1% a month. Their sample ends in 2014, and I found no post-2014 replication. Our "good past record" filter failed, but it was not their QEA-window rule.
- **Clustering.** Alldredge and Blank, "Do insiders cluster trades with colleagues? Evidence from daily insider trading," *J. Financial Research* 42(2):331 to 360 (2019), [RePEc](https://econpapers.repec.org/RePEc:bla:jfnres:v:42:y:2019:i:2:p:331-360) [abstract]. Clustered purchases are followed by abnormal returns above 2% in the following month. The strongest clusters are among close colleagues, for example a CEO and CFO buying within 2 days of each other. Practitioner sites (OpenInsider's "cluster buys") use a looser rule that does not follow the paper.
- **Role.** Seyhun (1986) and later work find higher-ranked insiders more informed [memory]. Wang, Shin and Francis, "Are CFOs' trades more informative than CEOs' trades?" *JFQA* 47 (2012), found CFO purchases outperform CEO purchases [memory]. Directors are generally weakest.
- **Size.** Lakonishok and Lee found the predictability concentrated in small firms. Our $25k floor, plus a survivor universe that may be skewed toward large caps, may be diluting the effect.
- **Filing delay and speed.** Before August 2002, Form 4 was due by the 10th of the following month. Since Sarbanes-Oxley it is due within 2 business days. Brochet, "Information content of insider trades before and after the Sarbanes-Oxley Act," *Accounting Review* 85 (2010), found filings became more informative and prices reacted faster after SOX [memory]. Our 2006+ sample is entirely post-SOX. Late filers (filed more than 2 business days after the trade) are a candidate subset to test, but I found no strong published US result to pre-register from.
- **10b5-1 plans.** Jagolinzer, "SEC Rule 10b5-1 and insiders' strategic trade," *Management Science* 55 (2009), found insiders start sale plans before bad news and stop them before good news [memory; cited in CMP]. The SEC amended the rule on 2022-12-14 [verified via law-firm summaries]. Directors and officers now face a cooling-off period of the later of 90 days or 2 business days after the next 10-Q or 10-K, capped at 120 days. Starting with Form 4s filed on or after 2023-04-01, there is a checkbox for 10b5-1(c) trades. This lets us exclude planned trades, but only from April 2023 onward, which covers only part of the holdout.
- **Crisis conditioning.** There is no clean peer-reviewed US test of insider purchase returns in bear markets versus calm markets.
  - Seyhun (1990) covers 1987.
  - Belgian evidence: Van Geyt, Van Cauwenberge and Vander Bauwhede, *J. Business Economics and Management* 14(2) (2013) [abstract]. Insider returns were significantly higher in the crisis years.
  - UK 2007 to 2008: Ozkan and coauthors (EFMA 2013 working paper) [abstract]. Purchase returns were stronger in the crisis.
  - COVID evidence is mixed. Insiders bought in record numbers after the February to March 2020 decline; this comes from a Seyhun-coauthored working paper found via a Bohrium index whose return results I could not verify, and from Adams and Balogh via [ProMarket](https://promarket.org/?p=17635). But a Finnish study (Ruska thesis, [Turku](https://www.utupub.fi/items/9a1d8ab5-caa3-4e92-baa3-afd87a0b3f0b/full); not peer-reviewed) found returns after crash-period purchases were *negative* at short horizons.
  - The prior is weak and mostly non-US.

---

## 2. US Congress trading (STOCK Act)

### 2.1 Academic results

| Paper | Sample | Key finding | Status |
|---|---|---|---|
| Ziobrowski, Cheng, Boyd and Ziobrowski, "Abnormal returns from the common stock investments of the U.S. Senate," *JFQA* 39:661 to 676 (2004) [abstract] | Senate, 1993 to 1998 | A portfolio mimicking purchases beats the market by 85 bp/month (about 10% a year). Sales lag the market by 12 bp/month. The "12% a year" figure in the press is not the paper's purchase number. | Pre-STOCK Act. Holdings were self-reported annually and the trade dates were imputed. |
| Ziobrowski, Boyd, Cheng and Ziobrowski, *Business and Politics* 13(1) (2011), [Cambridge](https://www.cambridge.org/core/journals/business-and-politics/article/abnormal-returns-from-the-common-stock-investments-of-members-of-the-us-house-of-representatives/BC6C6A524BBE96738BB94D37EF0FD1A5) [abstract] | House, 1985 to 2001, about 16,000 trades | Mimicking purchases beats the market by 55 bp/month (about 6% a year). | Pre-STOCK Act. |
| Eggers and Hainmueller, "Capitol losses: the mediocre performance of congressional stock portfolios," *J. Politics* 75(2) (2013) [memory] | 2004 to 2008 | Members as a group underperformed passive benchmarks by about 2 to 3% a year. Holdings in local firms and politically connected firms did better. | This directly contradicts Ziobrowski. |
| Belmont, Sacerdote, Sehgal and Van Hoek, "Do senators and house members beat the stock market? Evidence from the STOCK Act," *J. Public Economics* 207:104602 (2022), [NBER w26975](https://www.nber.org/papers/w26975.pdf), [RePEc](https://ideas.repec.org/a/eee/pubeco/v207y2022ics0047272722000044.html) [abstract] | 2012 to 2020, all members | Stocks bought by House members underperform by 26 bp over 6 months, and stocks they sell underperform by 11 bp. Even the 95th and 99th percentile returns are consistent with random picking. There is no evidence of skill in senators' stock picking or market timing. | Peer-reviewed. This is the strongest evidence against an edge. |
| Chen and Sacerdote, "Capital in the Capitol: congressional trades resemble uninformed retail trading," [NBER w35041](https://www.nber.org/papers/w35041) (April 2026) [verified page] | 2012 to 2023, members and families | After the STOCK Act, portfolios "underperform or, at best, match" benchmarks. Positions follow analyst recommendations and the timing follows retail social-media sentiment. | Working paper. It reinforces the null. |
| Wei and Zhou, "'Captain Gains' on Capitol Hill," [NBER w34524](https://www.nber.org/papers/w34524) (Nov 2025); [VoxEU summary](https://cepr.org/voxeu/columns/political-power-and-profitable-trades-us-congress) [verified page] | Mid-1990s to 2021 | Members who later become leaders perform like matched peers before ascending. Afterward they beat those peers by 47 percentage points a year. The edge is larger when their party controls the chamber and in trades of donor or home-state firms. Rank-and-file members match or trail the index. | Working paper. Only about 20 lawmakers trade both before and after ascending, so a huge point estimate rests on a tiny sample. Treat it as a hypothesis, not a fact. |

The consensus in peer-reviewed post-2012 data is that rank-and-file trades have no copyable edge. The only live claim is leadership trades, and it rests on one working paper with a tiny sample.

### 2.2 Practitioner products (marketing claims)

**NANC (Democrats) and KRUZ, now GOP (Republicans).** Subversive ETFs launched both on 2023-02-07; Unusual Whales now runs them. KRUZ became **GOP** on 2025-03-21 ([SEC 497](https://www.sec.gov/Archives/edgar/data/1742912/000199937125002790/kruz-497_031825.htm) [verified via search]).
- I computed total returns from Yahoo adjusted closes for 2023-02-08 to 2026-10-30 [verified]. SPY's figure matches our own Tiingo bars:

| Fund | Total return | Excess vs SPY |
|---|---|---|
| NANC | +101.8% | +8.9 points over about 3.7 years |
| SPY | +92.9% | |
| GOP (KRUZ) | +74.3% | -18.6 points |

- Over the trailing 1 year, NANC returned +7.9% against SPY's +11.1% ([stockanalysis](https://stockanalysis.com/etf/compare/nanc-vs-spy/)). Its expense ratio is about 0.72 to 0.75%.
- NANC's top holdings are NVDA (8.3%), MSFT, GOOG, AAPL and AMZN. Its lead is mostly a mega-cap tech overweight during a mega-cap tech rally, not trade timing. Without a factor-adjusted analysis it is indistinguishable from a QQQ tilt.
- The two funds together are an n=2, red-team-versus-blue-team experiment. One beat SPY and one lagged it.

**Unusual Whales annual "Congress trading reports"** (for example, 2025: Republicans +17.3% and Democrats +14.4% against roughly 16.6% for SPY; [Independent Institute summary](https://www.independent.org/article/2026/02/16/congress-beat-stock-market-2025/)). These are estimated annual portfolio returns built from disclosed value ranges. They have no risk adjustment, are measured from trade dates the public cannot act on, and report rankings that mostly reward tech concentration. Secondary sources even disagree on the headline numbers. Do not treat them as evidence.

### 2.3 Data quality issues (all confirmed in the source docs or a sample filing)

- **Lag.** The STOCK Act allows up to 30 days from notification and 45 days from the trade. In the sample PTR I parsed (House DocID 20035727), trades dated 2026-10-15 were reported on 2026-10-29. **Always enter on the filing date, never the transaction date.** Ziobrowski-style returns measured from the transaction date are not copyable.
- **Ranges, not amounts.** Amounts are reported in bands ($1,001 to $15,000, $15,001 to $50,000, ... up to over $50,000,000). Weighting by size is impossible; use equal weights or the midpoint of the band.
- **Owner.** Each line carries an owner code: SP for spouse, JT for joint, DC for dependent child. Pelosi trades are mostly her spouse's, often options. Many lines sit inside trusts and 401(k)s, as in the sample filing.
- **Options and non-stock assets** are mixed in with stock trades (the asset-type codes include [ST], [OP] and others). Filter to [ST].
- **Paper filings.** Older or paper PTRs are scanned images and need OCR. Electronically filed ones extract cleanly with `pdftotext` [verified].
- **Late filing is common.** The penalty is $200, often waived. Delinquent PTRs arrive months late, so the filing-date entry rule handles them correctly.

---

## 3. 13F, 13D and hedge-fund cloning

| Paper or product | Sample | Key finding | Survived? |
|---|---|---|---|
| Brav, Jiang, Partnoy and Thomas, "Hedge fund activism, corporate governance, and firm performance," *J. Finance* 63 (2008), [PDF](https://law.duke.edu/sites/default/files/centers/gfmc/session_3/2_brav_et_al-hedge_fund_activism-2008.pdf) [abstract] | 13D filings by activist hedge funds, 2001 to 2006 | Abnormal return of about 7 to 8% over the (-20, +20) day window around the 13D filing, with no reversal over the next year. The price and volume run-up starts 1 to 10 days before the filing. | Brav, Jiang and Kim, *Annual Review of Financial Economics* 7 (2015), extend this and find a similar effect [memory for the exact numbers]. A copier who enters after the public filing captures only the post-filing part of the window, and I have not verified how big that part is. Since 2024-02-05, initial 13Ds are due in 5 business days instead of 10 calendar days, which cuts the activist's quiet run-up period [memory]. |
| Agarwal, Jiang, Tang and Yang, "Uncovering hedge fund skill from the portfolios they hide," *J. Finance* 68(2) (2013) [memory] | 1999 to 2007 | Holdings filed under confidential treatment, and so disclosed late, earn higher abnormal returns than the holdings disclosed on time. | Implication: the public 13F systematically leaves out the best ideas. |
| Cohen, Polk and Silli, "Best Ideas," [SSRN 1364827](https://papers.ssrn.com/abstract=1364827) (2010) [memory] | US **mutual funds**, 1991 to 2005 | Managers' highest-conviction stocks (largest active weight) beat the market and the managers' other holdings by about 2.8 to 4.5% a year. | Not a hedge-fund 13F study. Using it on 13F data is an extension. |
| Martin and Puthenpurackal, "Imitation is the sincerest form of flattery: Warren Buffett and Berkshire Hathaway," SSRN (2008) [memory] | 1976 to 2006 | Copying Berkshire's purchases after public disclosure beat the S&P 500 by about 10.75% a year. | This is one manager and an in-sample result picked with hindsight about Buffett. Berkshire's own excess return since 2008 is small. |
| Frank, Poterba, Shackelford and Shoven, "Copycat funds," *J. Law and Economics* 47 (2004) [memory] | Mutual funds | Copying disclosed holdings, before expenses, matched or beat the funds being copied. | The effect weakens for hot funds whose holdings change fast. |
| **GURU** (Global X Guru Index ETF), launched 2012-06 | Live | It copies top 13F holdings, equal-weighted. CAGR from 2012-07 to 2026-10 was **11.5% against SPY's 14.65%** (Yahoo monthly adjusted closes) [verified]. Its expense ratio is 0.75%. | **The live record is about 3 points a year of underperformance over 14 years.** |
| **ALFA** (AlphaClone Alternative Alpha ETF) | Live, 2012 to 2022 | A 13F clone index. It was terminated in August 2022 (per the roic.ai listing, not re-checked against an SEC filing). The sister fund ALFI closed in 2018. | Shut down. This is a survivorship warning for the whole category. |
| Novus, Validea, WhaleWisdom backtests; Schroeder and Posch (2024) "Outperforming the market: portfolio strategy cloning from SEC 13F filings" | Vendor and working paper | Headline claims like "top-quartile clones beat the S&P by 24% a year" | They pick the top quartile using hindsight. I could not trace any of them to a peer-reviewed source. Treat as marketing. |

The consensus: activist 13D filings are the only fund-disclosure event with robust peer-reviewed announcement returns, and most of that return arrives at or before the filing. Broad 13F cloning has a failed live record (GURU, ALFA) despite favorable backtests.

---

## 4. Data sources

| Dataset | URL | Format, coverage | Gotchas | Verified |
|---|---|---|---|---|
| SEC Insider Transactions Data Sets (Forms 3, 4, 5) | https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets | Quarterly ZIPs of flattened XML; 2006 Q1 to 2026 Q3 | Contains only the XML-era filings. Released quarterly, so a live run needs EDGAR daily indexes. | Yes, page fetched |
| SEC Form 13F Data Sets | https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets | ZIPs of tables extracted from the XML; **2013 Q2** to the Jun to Aug 2026 file | Since March 2024 each file covers a 3-month window ending in Feb, May, Aug or Nov, so the files do not line up with calendar quarters; key on period of report and filing date. VALUE was reported in $ thousands until the January 2023 rule change and in whole dollars after it [memory; check the readme]. Data before 2013 needs EDGAR text 13F parsing. Confidential-treatment holdings show up later as amendments. | Yes, page fetched |
| EDGAR submissions API | https://data.sec.gov/submissions/CIK##########.json | JSON filing history per filer | Requires a descriptive User-Agent; rate limit about 10 requests/s | Yes, HTTP 200 (Pershing Square CIK 0001336528) |
| EDGAR full-text search (13D, 13G) | https://efts.sec.gov/LATEST/search-index?q=...&forms=SC%2013D | JSON; 2001 onward | **The form type changed: before about December 2024 it is `SC 13D`; after that it is `SCHEDULE 13D` (structured XML)**. Query both. Use `/A` only for amendments. | Yes. Returned 623 Pershing `SC 13D` hits and 59 `SCHEDULE 13D` hits after December 2024. |
| House Clerk financial disclosure | https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip | ZIP containing `{YEAR}FD.txt` (TSV) and `.xml` index: name, FilingType, StateDst, FilingDate, DocID. PTR filing type is `P`. | The index has no trades. Fetch each PTR PDF at `https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf` and parse it. Scanned filings need OCR. | Yes. The 2026 ZIP has 1,822 rows, 447 of them PTRs, latest filed 2026-10-29; a sample PTR was parsed with pdftotext. |
| Senate eFD | https://efdsearch.senate.gov/search/ | HTML search behind an agreement gate (session cookie plus CSRF token); 2012 onward | No bulk download. Many older PTRs are scanned images. | Partial. The page loads with the agreement gate; I did not script a search. |
| House and Senate Stock Watcher S3 mirrors | `house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json`, `senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json` | JSON | **Both returned HTTP 403 today. Treat them as dead.** The senate GitHub repo was last pushed in 2021. | Yes, 403 |
| Quiver Quantitative | https://www.quiverquant.com/congresstrading/ | Web page is free; API is paid | Derived data with unknown cleaning; use it only to cross-check | Page HTTP 200 |
| Capitol Trades | https://www.capitoltrades.com/trades | Web only, no official bulk download | Rate-limited | HTTP 429 (exists, throttled) |
| OpenInsider | http://openinsider.com/latest-cluster-buys | HTML tables, **http only** | Its own cluster and filter definitions; the 1d, 1w, 1m and 6m return columns are look-ahead, so never use them as features | Yes, HTTP 200. Columns: filing date, trade date, ticker, insider, type, price, qty, owned, change in ownership, value. |

---

## 5. Implications for SignalForge

**Multiple-testing rule.** The holdout (2015-01-01 to 2026-10-23) can be used once. Pre-register every hypothesis below together, with Holm correction across the family, before opening any holdout data. Development work stays on 2006 to 2014 data. Every test uses the same entry rule (next close after the public filing timestamp) and the same costs (5 bp), plus a **beta-matched random null**: same days, random stocks drawn from the same market-cap tercile and the same trailing-60-day beta tercile. The SPY-only null is what let a crash rebound pass for an edge.

Ranked by prior plausibility after publication:

1. **H1: CMP opportunistic insider purchases.** Universe: transaction code P by officers and directors; no dollar floor in the main test, with $25k as a secondary check. At each 1 January, classify every insider with at least one open-market P or S trade in each of the 3 prior calendar years. "Routine" means a trade in the same calendar month in each of those 3 consecutive years; every other eligible insider is "opportunistic." Test opportunistic minus routine purchases on (a) 21-session excess return and (b) CMP's own design, a calendar-month hold. First classifiable year is 2009. *Prior: moderate to weak.* This is the best-documented filter, but the one post-2010 study finds it decayed.
2. **H2: Crash-conditioned insider buying.** The same purchase universe, restricted to filings dated when SPY's close is at least 20% below its trailing 252-session high. Compare against the beta-matched null on the same days. The holdout contains about 2 regimes (March to April 2020 and 2022). *Prior: plausible but low power,* with mostly non-US evidence. Pre-register the low power and state that a null here is uninformative, not disconfirming.
3. **H3: Alldredge and Blank tight clusters.** At least 2 distinct insiders at the same firm make open-market purchases on the same or consecutive trading days. Enter at the next close after the *second* filing. Hold 21 sessions. *Prior: moderate.* Our wider cluster rule failed, so this tests the paper's exact definition.
4. **H4: Activist 13D initial filings.** Forms `SC 13D` and `SCHEDULE 13D`, not amendments, filed by a filer list frozen in advance (for example the activist list in Brav, Jiang et al., plus Pershing Square). Enter at the next close after EDGAR acceptance. Hold 21 and 252 sessions. *Prior: moderate for a small post-filing drift.* Much of the effect is pre-filing, and the 2024 five-day deadline shrinks it further.
5. **H5: Congressional leadership purchases.** STOCK Act PTR purchases, asset type [ST] only. Members holding a leadership post on the filing date: Speaker, majority and minority leaders, and whips, per Wei and Zhou's definition (confirm it from their paper first). Enter at the next close after the PTR *filing* date. Hold 21 and 126 sessions. Run rank-and-file members as a pre-registered *expected null* control. *Prior: weak.* It rests on one working paper with about 20 switchers.
6. **H6: 13F best-idea cloning.** For a frozen list of 10 large, low-turnover filers, take the largest new position (or the largest percentage weight increase) in each 13F. Enter at the next close after the filing date. Hold until the next 13F. *Prior: weakest.* GURU's 14-year live record (-3 points a year against SPY) and ALFA's closure say this is arbitraged or simply does not exist after the 45-day lag.

**Most likely already arbitraged, per the literature:** broad 13F cloning (H6), rank-and-file Congress trades (the H5 control), routine insider purchases, and loose cluster rules. **Least arbitraged but least powered:** crash-conditioned buying (H2) and leadership trades (H5). The single most informative test is H1. If CMP's exact rule shows no edge in 2015 to 2026, the Form 4 line is effectively closed apart from the crash regime.

---

*Provenance notes.* Rows marked [memory] were not re-fetched this session. Check the journal, volume, sample period and headline number of each before citing it in a pre-registration. The NANC, GOP, GURU and SPY returns were computed from Yahoo chart API adjusted closes on 2026-11-01. SPY's price agreed with the local Tiingo `bars` table to within $0.02.
