"""Synthetic worlds for the Assay tests: insiders with no edge, or some with a real one.

Prices are random walks with a common market factor; SPY is the market. Each
insider belongs to one company and buys on random days. A skilled insider's
buys are followed by extra drift over the next month, starting the day after
the copy would enter, so only a strategy that can identify them from their
past can earn it.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta

import numpy as np

from signalforge.insiders.events import Purchase
from signalforge.insiders.study import PricePanel

MONTH = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def world(*, n_days=2400, n_symbols=60, n_owners=320, buys=(3, 12), skilled_share=0.0, skill=0.04,
          seed=1, start=date(2006, 1, 2)):
    rng = np.random.default_rng(seed)
    days = business_days(start, n_days)
    syms = [f"S{i:03d}" for i in range(n_symbols)]
    market = rng.normal(0.0003, 0.010, n_days)
    rets = market[:, None] + rng.normal(0.0002, 0.016, (n_days, n_symbols))
    owners = []
    for k in range(n_owners):
        owners.append((str(100000 + k), int(rng.integers(0, n_symbols)), bool(rng.random() < skilled_share)))
    purchases = []
    acc = 0
    for cik, col, skilled in owners:
        for sig in np.sort(rng.choice(np.arange(30, n_days - 70), size=int(rng.integers(*buys)), replace=False)):
            if skilled:  # extra drift from the day after the copy's entry, for a month
                rets[sig + 2:sig + 23, col] += skill / 21
            acc += 1
            purchases.append((f"0000{acc:06d}-06-{acc:06d}", int(sig), cik, col))
    levels = 50 * np.exp(np.cumsum(rets, axis=0))
    spy = 100 * np.exp(np.cumsum(market, axis=0))
    adj = np.column_stack([levels, spy])
    panel = PricePanel(days=tuple(days), symbols=tuple(syms + ["SPY"]), adj=adj, close=adj.copy())
    out = []
    for accn, sig, cik, col in purchases:
        trade = sig - 1
        out.append(Purchase(
            accession=accn, filing_date=days[sig], trans_date=days[trade], issuer_cik=str(col + 1),
            symbol=syms[col], owner_cik=cik, is_director=True, is_officer=False, is_ten_pct=False,
            value=float(1000 * levels[trade, col]), price=float(levels[trade, col]),
        ))
    return out, panel, {cik: skilled for cik, _, skilled in owners}


def sec_date(d: date) -> str:
    return f"{d.day:02d}-{MONTH[d.month - 1]}-{d.year}"


def sec_zip(submissions, owners, trans, *, extra_sub_cols=()) -> bytes:
    """A quarterly zip in the SEC layout: header row, tab-separated, DD-MON-YYYY dates."""
    sub_cols = ["ACCESSION_NUMBER", "FILING_DATE", "PERIOD_OF_REPORT", "DOCUMENT_TYPE", "ISSUERCIK",
                "ISSUERNAME", "ISSUERTRADINGSYMBOL", *extra_sub_cols]
    own_cols = ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"]
    tr_cols = ["ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE",
               "EQUITY_SWAP_INVOLVED", "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD",
               "DIRECT_INDIRECT_OWNERSHIP"]

    def table(cols, rows):
        return "\n".join(["\t".join(cols)] + ["\t".join(str(r.get(c, "")) for c in cols) for r in rows]) + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", table(sub_cols, submissions))
        z.writestr("REPORTINGOWNER.tsv", table(own_cols, owners))
        z.writestr("NONDERIV_TRANS.tsv", table(tr_cols, trans))
        z.writestr("FOOTNOTES.tsv", "ACCESSION_NUMBER\tFOOTNOTE_ID\tFOOTNOTE_TXT\n")
    return buf.getvalue()
