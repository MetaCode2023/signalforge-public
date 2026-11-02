# Pre-registration draft: copy-trading hypotheses for the one-shot holdout

DRAFT for the operator. Nothing here is registered and nothing has read the
test window (2015-01-01 to 2026-10-23). Rules are taken from the published
papers, not tuned on our validation run. Approve, edit or drop each one; the
approved set is then run once, together, through a holdout path that refuses
a second run.

## Common to every hypothesis

- Entry at the close of the first trading day after the filing date (the
  public cannot act earlier). Exit after 21 sessions (5 and 63 reported, not
  tested).
- Score: return minus SPY over the same days, after 5 bps each way.
- Nulls, as in Assay: random stock on the same days, and random insider buy
  (or random filing of the same kind) in the same months. Add a third: random
  stock matched on size decile and 252-day beta tercile on the same days, so a
  crash-rebound or high-beta tilt is not read as information.
- Family-wise control: Holm correction across the approved hypotheses, alpha
  0.05, one-sided (copying should beat the null).
- Price coverage: the warehouse's tiingo symbols at the time of the run. The
  share of events that could be priced is reported for each hypothesis; a
  hypothesis with fewer than 200 priced events is reported as
  INSUFFICIENT_EVIDENCE, not tested.

## H1. Opportunistic insiders (Cohen, Malloy, Pomorski 2012)

- Event: an officer or director open-market purchase (Form 4, code P, $25,000
  or more, not a 10b5-1 planned trade) by an insider labeled opportunistic
  for that calendar year.
- Label: at 1 January of year Y, an insider is eligible if they made at least
  one open-market purchase or sale in each of Y-1, Y-2 and Y-3. Eligible
  insiders who traded in the same calendar month in all three years are
  routine; all other eligible insiders are opportunistic. Built:
  `insider_cmp_class` (local and D1).
- Comparison: opportunistic minus routine, and opportunistic against the
  random-insider null.
- Prior: positive in 1986 to 2007; reported to have shrunk after Dodd-Frank
  (Gangopadhyay and Yook 2022).

## H2. Insider buys in a bear market

- Event: any H1-eligible insider purchase filed while SPY's close is 20% or
  more below its highest close of the prior 252 sessions.
- Rationale: the validation edge sat in 2008 and 2009. The 20% line is the
  conventional bear-market definition, chosen before looking.
- Expected n in the holdout: low (the 2020 crash and the 2022 bear market).
  Power is limited; say so in the result.

## H3. Tight insider clusters (Alldredge and Blank 2019)

- Event: two or more distinct officers or directors buying the same company on
  the same or consecutive trading days. Entry after the latest filing in the
  cluster. Built: `insider_buy_clusters`.
- Comparison: against single-insider buys in the same months.

## H4. Initial activist 13D filings (Brav, Jiang, Partnoy, Thomas 2008)

- Event: a first-time Schedule 13D (not an amendment) where the subject
  company has a priced ticker. Entry the day after the filing date.
- Note: since February 2024 the deadline is 5 business days, not 10; the
  pre-filing run-up is excluded by construction.
- Built: `stake_filings` and `stake_headers.tsv` (subject and filer from each
  filing's header; coverage grows, newest first).

## H5. Congressional leadership purchases (Wei and Zhou 2025, working paper)

- Event: a purchase disclosed by a member while in party leadership, entered
  after the report's filing date (not the trade date). Control: purchases by
  rank-and-file members in the same months.
- Gap: leadership roles by date are not yet in the database (the committee
  data is current only). Needs a dated leadership list before it can run.
- Prior: weak. Peer-reviewed post-2012 studies find no edge for Congress as a
  whole.

## Not proposed

- 13F best-idea cloning: live clone funds underperformed (GURU, ALFA) and the
  45-day lag removes most of the edge. Kept as a descriptive table only.
- Rank-and-file Congress trades: expected null; used as H5's control.
- The unconditional "every insider buy" rule: its validation edge was a
  2008 to 2009 effect; spending the holdout on it alone likely buys a clean
  null.

## What has to be built before the run

1. A one-shot insider/event holdout path with the `HoldoutViolation` guard
   used by `research_cli`.
2. The size and beta matched null.
3. Price coverage beyond the 493 warehouse symbols, or acceptance that most
   events cannot be priced.
4. For H5, a dated leadership list.
