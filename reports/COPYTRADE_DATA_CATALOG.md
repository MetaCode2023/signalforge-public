# Copy-trading data pile: catalog

Built overnight 2026-11-01 to 11-02 on the operator's PC. Every source is free,
public and legal; nothing was bought, no login or bot check was bypassed. All
data lives under gitignored folders (`data/raw/`, `data/copytrade/`,
`data/congress/`, `data/insiders/`) and is rebuildable from the scripts noted.

## Databases (DuckDB, local)

| File | Size | What |
|---|---:|---|
| `data/copytrade/copytrade.duckdb` | ~4.5 GB | 13F, congress, Form 4 purchases, EDGAR index, gurus, stakes, legislators |
| `data/copytrade/insiders_full.duckdb` | ~2 GB | every Form 3/4/5 transaction 2006 to 2026 |
| `data/copytrade/market_extras.duckdb` | ~0.5 GB | SEC fails-to-deliver 2009 to 2026 |

## Tables

| Table / view | Rows | Coverage | Source |
|---|---:|---|---|
| `f13_infotable` (+ `f13_submission`, `f13_coverpage`, `f13_summary`) | 124,012,468 holdings in 322,917 filings | 2013q2 to 2026-08 | SEC Form 13F Data Sets |
| `f13_holdings` (view) | same | value normalised to dollars (pre-2023 sets report $000) | derived |
| `guru_13f_holdings` (view) | subset | 18 famous managers | derived |
| `guru_filings` | 15,946 | full EDGAR filing history of 18 managers | data.sec.gov submissions API |
| `edgar_filings_index` | 13,409,830 | every 13D/13G/13F/Form 3/4/5 filing, 1994 to 2026 | EDGAR full-index |
| `stake_filings` | 1.84M filings | 13D/13G grouped per filing, activist and initial flags | derived |
| `stake_headers.tsv` | growing | SUBJECT COMPANY and FILED BY for each 13D/13G (header reads, newest first) | EDGAR filing headers |
| `form4_purchases` | 328,643 | open-market officer/director purchases, 2006 to 2026q3 | SEC Insider Transactions Data Sets |
| `f345_nonderiv_trans` | 7,353,719 | every non-derivative insider transaction incl. sales, gifts, awards | same |
| `f345_deriv_trans` | 2,757,824 | options and other derivatives | same |
| `f345_submission`, `f345_owner`, `f345_nonderiv_holding` | 4.5M / 4.9M / 2.4M | filings, owners and roles, holdings | same |
| `house_ptr_raw` | 55,577 | House periodic transaction reports, 2015 to 2026 filings | parsed from official House Clerk PDFs |
| `senate_trades_raw` | 16,736 | Senate PTRs, 2012 to 2026-09 | MIT-licensed mirror of eFD (kmysaf/senate-data) |
| `senate_stock_watcher_raw` | 8,350 | Senate 2012 to 2020 (cross-check) | senate-stock-watcher-data |
| `capitol_stock_watch_recent` | 4,925 | recent both chambers, CC-BY 4.0 | capitol-stock-watch-open-data |
| `congress_trades` / `congress_trades_clean` (views) | 72,238 clean | unified House + Senate with disclosure lag | derived |
| `congress_member_terms` | 3,969 | every House and Senate term ending 2012 or later | unitedstates/congress-legislators (CC0) |
| `congress_committee_membership_current`, `congress_committees_current` | 3,895 / 230 | current committee seats, ranks, titles | same |
| `sec_tickers` | 10,423 | CIK to ticker to exchange | SEC company_tickers_exchange.json |
| `sec_fails_to_deliver` (market_extras) | 23,690,686 | 2009-07 to 2026-10, 60,215 symbols | SEC fails-to-deliver data |
| FINRA Reg SHO daily short volume (raw files) | downloading | 2009-08 to 2026-10 | FINRA public CDN |

## Known gaps and caveats

- **House paper filings.** 2,049 of 7,717 House PTRs are scanned paper with no
  text layer. OCR (Tesseract) runs at low priority; parsed paper rows will be
  marked `method=ocr` and are less reliable.
- **Amounts are ranges** ($1,001 to $15,000 and so on), not exact dollars.
  Any dollar total uses midpoints and is a rough guide.
- **Tickers.** 65% of House rows carry a ticker; the rest are mostly bonds,
  funds, private LLCs and older filings that omit it.
- **Senate after 2020 comes from a third-party mirror** of the official
  eFD site. eFD itself sits behind a bot check that was not bypassed.
- **13F** reports long positions only, 45 days after quarter end; values
  switch from thousands to dollars in January 2023 (handled in the views).
- **13D subject matching** by ticker covers only companies listed today;
  the header reads fix that, newest filings first.
- **Prices** are still the binding limit for testing anything: the warehouse
  holds 493 symbols.

## Example queries

```sql
-- Pelosi household trades with option details
SELECT txn_date, ticker, tx_type, amount_low, amount_high, owner, asset_name
FROM congress_trades_clean WHERE member ILIKE '%Pelosi%' ORDER BY txn_date DESC;

-- Ackman's latest 13F book
SELECT issuer, put_call, value_usd FROM guru_13f_holdings
WHERE manager LIKE 'Pershing%' AND period = (SELECT max(period) FROM guru_13f_holdings WHERE manager LIKE 'Pershing%')
ORDER BY value_usd DESC;

-- Members who file late (STOCK Act deadline is 45 days)
SELECT member, count(*) n, sum((disclosure_lag_days > 45)::INT) late
FROM congress_trades_clean GROUP BY 1 HAVING n >= 50 ORDER BY late DESC;
```
