"""Insider purchases as events: load, map to warehouse tickers, select."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from signalforge.core.errors import ValidationError


@dataclass(frozen=True, slots=True)
class Purchase:
    accession: str
    filing_date: date
    trans_date: date
    issuer_cik: str
    symbol: str
    owner_cik: str
    is_director: bool
    is_officer: bool
    is_ten_pct: bool
    value: float
    price: float


def load_purchases(path: str | Path) -> list[Purchase]:
    import polars as pl

    path = Path(path)
    if not path.is_file():
        raise ValidationError(
            f"no purchases file at {path}. Run `insider_cli download` then `insider_cli build`."
        )
    frame = pl.read_parquet(path)
    needed = set(Purchase.__slots__)
    missing = needed - set(frame.columns)
    if missing:
        raise ValidationError(f"{path} lacks columns {sorted(missing)}; rebuild it")
    return [Purchase(**{k: row[k] for k in needed}) for row in frame.iter_rows(named=True)]


def normalize_ticker(symbol: str) -> str:
    """SEC filings write BRK.B, BRK/B or BRKB; compare on a common spelling."""
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def resolve_symbols(purchases, known_symbols) -> tuple[list[Purchase], Counter]:
    """Map each issuer to today's ticker, so a rename (FB to META) keeps its history.

    An issuer's ticker is the one on its most recent filing. When two issuers
    have used the same ticker, it belongs to whichever used it last, and the
    other issuer's purchases are dropped rather than credited to the wrong
    company. The mapping uses filing metadata only, never prices or outcomes.
    """
    known = {normalize_ticker(s): s for s in known_symbols}
    latest: dict[str, tuple[date, str]] = {}
    for p in purchases:
        if p.symbol and (p.issuer_cik not in latest or p.filing_date >= latest[p.issuer_cik][0]):
            latest[p.issuer_cik] = (p.filing_date, normalize_ticker(p.symbol))
    holder: dict[str, tuple[date, str]] = {}
    for cik, (when, sym) in latest.items():
        if sym not in holder or when > holder[sym][0]:
            holder[sym] = (when, cik)
    out, dropped = [], Counter()
    for p in purchases:
        if p.issuer_cik not in latest:
            dropped["no ticker on any filing"] += 1
            continue
        sym = latest[p.issuer_cik][1]
        if holder[sym][1] != p.issuer_cik:
            dropped["ticker now belongs to another company"] += 1
        elif sym not in known:
            dropped["company not in the price warehouse"] += 1
        else:
            out.append(replace(p, symbol=known[sym]))
    return out, dropped


def select(purchases, *, min_value: float = 25_000.0, include_ten_pct: bool = False) -> tuple[list[Purchase], Counter]:
    """Officers and directors buying at least `min_value` of stock.

    Ten-percent owners who are not officers or directors are usually funds,
    whose purchases are investment decisions rather than an insider's read
    of their own company, so they are left out unless asked for.
    """
    out, dropped = [], Counter()
    for p in purchases:
        if not (p.is_officer or p.is_director or (include_ten_pct and p.is_ten_pct)):
            dropped["not an officer or director"] += 1
        elif p.value < min_value:
            dropped[f"under ${min_value:,.0f}"] += 1
        else:
            out.append(p)
    return out, dropped
