"""SEC Insider Transactions Data Sets: download and parse open-market insider purchases.

Source: https://www.sec.gov/dera/data/form-345 . Quarterly zips flattened from
the XML of Forms 3, 4 and 5, from 2006 Q1 onward: tab-delimited UTF-8 files
SUBMISSION.tsv, REPORTINGOWNER.tsv and NONDERIV_TRANS.tsv (among others),
joined on ACCESSION_NUMBER. Dates are DD-MON-YYYY. Readme:
https://www.sec.gov/files/insider_transactions_readme.pdf

This is public data from a US government agency, with no licence restriction.
The SEC's fair-access policy asks every client to declare a User-Agent with a
contact address and to stay well under 10 requests a second; the downloader
does both and refuses to run without a contact address.

The raw zips are immutable: an existing, valid zip is never re-downloaded or
edited. Parsing keeps only what the study needs: original Form 4 open-market
purchases (transaction code P, shares acquired) of common stock by a single
reporting owner, outside a 10b5-1 plan. Every row dropped is counted by
reason in the manifest, so the funnel from "filings" to "events" is visible
rather than assumed.

Nothing here is repaired. A missing column refuses the whole file with the
column named; a row that fails a check is counted and left out.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from signalforge.core.errors import ValidationError

URL_BASES = (
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets",
    "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets",
)
FIRST_QUARTER = (2006, 1)
DEFAULT_RAW_DIR = Path("data") / "raw" / "sec_insiders"
DEFAULT_OUT_DIR = Path("data") / "insiders"
PURCHASES_FILE = "purchases.parquet"
MANIFEST_FILE = "manifest.json"

SUBMISSION_COLUMNS = ("ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERTRADINGSYMBOL")
OWNER_COLUMNS = ("ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP")
TRANS_COLUMNS = (
    "ACCESSION_NUMBER", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
    "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD",
)
MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
TRUTHY = {"1", "Y", "YES", "TRUE", "T"}
NOT_COMMON = ("PREFERRED", "WARRANT", "OPTION", "NOTE", "DEBENTURE", "UNIT", "RIGHT", "BOND", "SWAP")
_DMY = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_YMD = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_QUARTER = re.compile(r"^(\d{4})[qQ]([1-4])$")


class SourceFormatError(ValidationError):
    """The SEC file does not have the shape this parser was written for."""


# --------------------------------------------------------------------- quarters

def parse_quarter(text: str) -> tuple[int, int]:
    m = _QUARTER.match(text.strip())
    if not m:
        raise ValidationError(f"quarter must look like 2006q1, got {text!r}")
    year, q = int(m.group(1)), int(m.group(2))
    if (year, q) < FIRST_QUARTER:
        raise ValidationError(f"the SEC data sets start at 2006q1; {text} is earlier")
    return year, q


def quarter_range(start: str, end: str) -> list[tuple[int, int]]:
    a, b = parse_quarter(start), parse_quarter(end)
    if b < a:
        raise ValidationError(f"quarter range {start}..{end} is empty")
    out, (y, q) = [], a
    while (y, q) <= b:
        out.append((y, q))
        y, q = (y, q + 1) if q < 4 else (y + 1, 1)
    return out


def zip_name(year: int, q: int) -> str:
    return f"{year}q{q}_form345.zip"


def dataset_urls(year: int, q: int) -> list[str]:
    return [f"{base}/{zip_name(year, q)}" for base in URL_BASES]


# --------------------------------------------------------------------- download

@dataclass(frozen=True)
class DownloadOutcome:
    quarter: str
    status: str  # "downloaded" | "present" | "missing" | "failed"
    path: str | None = None
    bytes: int = 0
    sha256: str | None = None
    detail: str | None = None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            names = {n.upper().rsplit("/", 1)[-1] for n in z.namelist()}
            return {"SUBMISSION.TSV", "REPORTINGOWNER.TSV", "NONDERIV_TRANS.TSV"} <= names
    except (zipfile.BadZipFile, OSError):
        return False


def require_contact(user_agent: str) -> str:
    if not user_agent or "@" not in user_agent:
        raise ValidationError(
            "the SEC asks every automated client to declare a User-Agent with a contact "
            "email address; set user_agent in config/data_sources.yaml"
        )
    return user_agent


def download(dest: Path, quarters, session, user_agent: str, *, pause_s: float = 0.25,
             timeout_s: float = 180.0, sleep=time.sleep) -> list[DownloadOutcome]:
    """Fetch each quarterly zip once. Existing valid zips are left untouched."""
    require_contact(user_agent)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    outcomes = []
    for year, q in quarters:
        label = f"{year}q{q}"
        target = dest / zip_name(year, q)
        if target.exists() and _valid_zip(target):
            outcomes.append(DownloadOutcome(label, "present", str(target), target.stat().st_size, _sha256(target)))
            continue
        status, detail = "missing", "not published at either SEC path"
        for url in dataset_urls(year, q):
            sleep(pause_s)
            try:
                response = session.get(url, headers=headers, timeout=timeout_s, allow_redirects=True)
            except Exception as exc:  # network failure: report, try the other path
                status, detail = "failed", f"{type(exc).__name__}"
                continue
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                status, detail = "failed", f"HTTP {response.status_code}"
                continue
            part = target.with_suffix(".zip.part")
            part.write_bytes(response.content)
            if not _valid_zip(part):
                part.unlink(missing_ok=True)
                status, detail = "failed", "response was not a data set zip"
                continue
            part.replace(target)
            status, detail = "downloaded", None
            break
        if status == "downloaded":
            outcomes.append(DownloadOutcome(label, status, str(target), target.stat().st_size, _sha256(target)))
        else:
            outcomes.append(DownloadOutcome(label, status, detail=detail))
    return outcomes


# --------------------------------------------------------------------- parse

def parse_sec_date(text: str | None) -> date | None:
    if not text:
        return None
    t = text.strip()
    m = _DMY.match(t)
    try:
        if m:
            month = MONTHS.get(m.group(2).upper())
            return date(int(m.group(3)), month, int(m.group(1))) if month else None
        m = _YMD.match(t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return None


def _num(text: str | None) -> float | None:
    if text is None or not text.strip():
        return None
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if value == value and abs(value) < 1e15 else None


def _cik(text: str | None) -> str | None:
    t = (text or "").strip()
    return str(int(t)) if t.isdigit() else None


def _doc_type(text: str | None) -> str:
    return re.sub(r"\s+", "", (text or "").upper()).replace("FORM", "")


def is_common_stock(title: str | None) -> bool:
    t = (title or "").upper()
    if any(bad in t for bad in NOT_COMMON):
        return False
    return "COMMON" in t or "ORDINARY" in t or t.strip() in {"STOCK", "SHARES", "CLASS A", "CLASS B"}


def roles(relationship: str | None) -> tuple[bool, bool, bool]:
    """(director, officer, ten-percent owner) from RPTOWNER_RELATIONSHIP."""
    tokens = [tok.strip() for tok in re.split(r"[,;/|]+", (relationship or "").upper()) if tok.strip()]
    director = any("DIRECTOR" in tok for tok in tokens)
    officer = any("OFFICER" in tok for tok in tokens)
    ten = any(("TEN" in tok and "PERCENT" in tok) or "10%" in tok or "10 PERCENT" in tok for tok in tokens)
    return director, officer, ten


def _member(z: zipfile.ZipFile, name: str) -> str:
    for n in z.namelist():
        if n.upper().rsplit("/", 1)[-1] == name:
            return n
    raise SourceFormatError(f"{name} is missing from {Path(z.filename or '').name}")


def _rows(z: zipfile.ZipFile, name: str, required):
    member = _member(z, name)
    handle = z.open(member)
    # utf-8-sig drops a byte-order mark, which would otherwise hide the first column name.
    text = io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace", newline="")
    reader = csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
    header = {h.strip().strip('"').upper() for h in (reader.fieldnames or [])}
    missing = [c for c in required if c not in header]
    if missing:
        raise SourceFormatError(
            f"{name} in {Path(z.filename or '').name} lacks columns {missing}; the SEC format "
            "may have changed. Nothing from this file was used."
        )
    for row in reader:
        # Fields are read unquoted; a value wrapped in double quotes is unwrapped rather
        # than failing its number check and silently dropping the row.
        yield {(k or "").strip().strip('"').upper(): (v or "").strip().strip('"') for k, v in row.items() if k}


@dataclass
class QuarterParse:
    file: str
    sha256: str
    transaction_rows: int = 0
    purchases: int = 0
    rejected: Counter = field(default_factory=Counter)


def parse_zip(path: Path) -> tuple[list[dict], QuarterParse]:
    """Open-market common-stock purchases from one quarterly zip, one row per filing."""
    path = Path(path)
    result = QuarterParse(file=path.name, sha256=_sha256(path))
    with zipfile.ZipFile(path) as z:
        subs = {}
        for r in _rows(z, "SUBMISSION.TSV", SUBMISSION_COLUMNS):
            subs[r["ACCESSION_NUMBER"]] = r
        owners: dict[str, list[dict]] = {}
        for r in _rows(z, "REPORTINGOWNER.TSV", OWNER_COLUMNS):
            owners.setdefault(r["ACCESSION_NUMBER"], []).append(r)
        merged: dict[str, dict] = {}
        for r in _rows(z, "NONDERIV_TRANS.TSV", TRANS_COLUMNS):
            result.transaction_rows += 1
            reason = None
            acc = r["ACCESSION_NUMBER"]
            sub = subs.get(acc)
            if r.get("TRANS_CODE", "").upper() != "P":
                reason = "not an open-market purchase"
            elif r.get("TRANS_ACQUIRED_DISP_CD", "").upper() != "A":
                reason = "not an acquisition"
            elif sub is None:
                reason = "no submission row"
            elif _doc_type(sub.get("DOCUMENT_TYPE")) != "4":
                reason = "amendment or not a Form 4"
            elif sub.get("AFF10B5ONE", "").upper() in TRUTHY:
                reason = "10b5-1 planned trade"
            elif r.get("EQUITY_SWAP_INVOLVED", "").upper() in TRUTHY:
                reason = "equity swap"
            elif not is_common_stock(r.get("SECURITY_TITLE")):
                reason = "not common stock"
            elif len(owners.get(acc, [])) != 1:
                reason = "joint or ownerless filing"
            if reason is None:
                filing = parse_sec_date(sub.get("FILING_DATE"))
                trade = parse_sec_date(r.get("TRANS_DATE"))
                shares, price = _num(r.get("TRANS_SHARES")), _num(r.get("TRANS_PRICEPERSHARE"))
                issuer, owner = _cik(sub.get("ISSUERCIK")), _cik(owners[acc][0].get("RPTOWNERCIK"))
                if filing is None or trade is None:
                    reason = "unreadable date"
                elif trade > filing:
                    reason = "trade dated after its filing"
                elif not shares or shares <= 0 or not price or price <= 0:
                    reason = "no positive shares and price"
                elif issuer is None or owner is None:
                    reason = "unreadable CIK"
            if reason is not None:
                result.rejected[reason] += 1
                continue
            director, officer, ten = roles(owners[acc][0].get("RPTOWNER_RELATIONSHIP"))
            row = merged.get(acc)
            if row is None:
                merged[acc] = {
                    "accession": acc, "filing_date": filing, "trans_date": trade,
                    "issuer_cik": issuer, "symbol": (sub.get("ISSUERTRADINGSYMBOL") or "").upper().strip(),
                    "owner_cik": owner, "is_director": director, "is_officer": officer, "is_ten_pct": ten,
                    "shares": shares, "value": shares * price,
                }
            else:  # several fills in one filing: one purchase, summed
                row["shares"] += shares
                row["value"] += shares * price
                row["trans_date"] = max(row["trans_date"], trade)
    rows = list(merged.values())
    for row in rows:
        row["price"] = row["value"] / row["shares"]
    result.purchases = len(rows)
    return rows, result


def build(raw_dir: Path, out_dir: Path, quarters=None) -> dict:
    """Parse every zip present (or the given quarters) into one purchases file."""
    import polars as pl

    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    paths = sorted(raw_dir.glob("*_form345.zip")) if quarters is None else [
        raw_dir / zip_name(y, q) for y, q in quarters if (raw_dir / zip_name(y, q)).exists()
    ]
    if not paths:
        raise ValidationError(f"no SEC data set zips in {raw_dir}; run the download first")
    all_rows: dict[str, dict] = {}
    parses = []
    for p in paths:
        rows, info = parse_zip(p)
        for r in rows:
            all_rows.setdefault(r["accession"], r)
        parses.append(info)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        sorted(all_rows.values(), key=lambda r: (r["filing_date"], r["accession"])),
        schema={
            "accession": pl.Utf8, "filing_date": pl.Date, "trans_date": pl.Date, "issuer_cik": pl.Utf8,
            "symbol": pl.Utf8, "owner_cik": pl.Utf8, "is_director": pl.Boolean, "is_officer": pl.Boolean,
            "is_ten_pct": pl.Boolean, "shares": pl.Float64, "value": pl.Float64, "price": pl.Float64,
        },
    )
    frame.write_parquet(out_dir / PURCHASES_FILE)
    rejected = Counter()
    for info in parses:
        rejected.update(info.rejected)
    manifest = {
        "source": "SEC Insider Transactions Data Sets (Forms 3/4/5)",
        "source_url": "https://www.sec.gov/dera/data/form-345",
        "quarters": [{**asdict(i), "rejected": dict(i.rejected)} for i in parses],
        "transaction_rows": sum(i.transaction_rows for i in parses),
        "purchases": frame.height,
        "rejected": dict(rejected.most_common()),
        "first_filing": str(frame["filing_date"].min()) if frame.height else None,
        "last_filing": str(frame["filing_date"].max()) if frame.height else None,
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
