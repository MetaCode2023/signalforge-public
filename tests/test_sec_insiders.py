"""SEC insider data: polite download, strict parse, every dropped row counted.

No network: the downloader is driven by a stand-in session, and the parser
reads zips built in the SEC layout (header row, tabs, DD-MON-YYYY dates).
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from signalforge.core.errors import ValidationError
from signalforge.data import sec_insiders as sec
from tests.insider_support import sec_zip

ACC = "0001234567-10-000001"


def _sub(acc, filed="05-MAR-2010", doc="4", symbol="AAA", cik="0000320193", **extra):
    return {"ACCESSION_NUMBER": acc, "FILING_DATE": filed, "PERIOD_OF_REPORT": filed, "DOCUMENT_TYPE": doc,
            "ISSUERCIK": cik, "ISSUERNAME": "Alpha Co", "ISSUERTRADINGSYMBOL": symbol, **extra}


def _own(acc, cik="0001000001", rel="Director"):
    return {"ACCESSION_NUMBER": acc, "RPTOWNERCIK": cik, "RPTOWNERNAME": "Owner", "RPTOWNER_RELATIONSHIP": rel}


def _tr(acc, code="P", ad="A", shares="1000", price="20.50", traded="03-MAR-2010", title="Common Stock", swap="0"):
    return {"ACCESSION_NUMBER": acc, "NONDERIV_TRANS_SK": "1", "SECURITY_TITLE": title, "TRANS_DATE": traded,
            "TRANS_CODE": code, "EQUITY_SWAP_INVOLVED": swap, "TRANS_SHARES": shares,
            "TRANS_PRICEPERSHARE": price, "TRANS_ACQUIRED_DISP_CD": ad, "DIRECT_INDIRECT_OWNERSHIP": "D"}


def write(tmp_path, subs, owners, trans, name="2010q1_form345.zip", **kw):
    path = tmp_path / name
    path.write_bytes(sec_zip(subs, owners, trans, **kw))
    return path


class TestParse:
    def test_keeps_an_open_market_purchase_and_sums_fills(self, tmp_path):
        path = write(tmp_path, [_sub(ACC)], [_own(ACC)], [
            _tr(ACC, shares="1000", price="20"), _tr(ACC, shares="500", price="23", traded="04-MAR-2010"),
        ])
        rows, info = sec.parse_zip(path)
        assert len(rows) == 1 and info.purchases == 1 and info.transaction_rows == 2
        r = rows[0]
        assert r["filing_date"] == date(2010, 3, 5) and r["trans_date"] == date(2010, 3, 4)
        assert r["shares"] == 1500 and r["value"] == 1000 * 20 + 500 * 23
        assert r["price"] == pytest.approx(r["value"] / 1500)
        assert r["issuer_cik"] == "320193" and r["owner_cik"] == "1000001" and r["is_director"]

    def test_every_dropped_row_is_counted_by_reason(self, tmp_path):
        rows = [
            ("a1", _sub("a1"), [_own("a1")], _tr("a1", code="S", ad="D"), "not an open-market purchase"),
            ("a2", _sub("a2", doc="4/A"), [_own("a2")], _tr("a2"), "amendment or not a Form 4"),
            ("a3", _sub("a3"), [_own("a3"), _own("a3", cik="2")], _tr("a3"), "joint or ownerless filing"),
            ("a4", _sub("a4"), [_own("a4")], _tr("a4", title="Series A Preferred"), "not common stock"),
            ("a5", _sub("a5"), [_own("a5")], _tr("a5", swap="1"), "equity swap"),
            ("a6", _sub("a6"), [_own("a6")], _tr("a6", price="0"), "no positive shares and price"),
            ("a7", _sub("a7"), [_own("a7")], _tr("a7", traded="09-MAR-2010"), "trade dated after its filing"),
            ("a8", _sub("a8"), [_own("a8")], _tr("a8", traded="31-FOO-2010"), "unreadable date"),
        ]
        path = write(tmp_path, [r[1] for r in rows], [o for r in rows for o in r[2]], [r[3] for r in rows])
        kept, info = sec.parse_zip(path)
        assert kept == []
        assert dict(info.rejected) == {r[4]: 1 for r in rows}

    def test_planned_trades_are_dropped_when_the_flag_exists(self, tmp_path):
        path = write(tmp_path, [_sub(ACC, AFF10B5ONE="1")], [_own(ACC)], [_tr(ACC)], extra_sub_cols=("AFF10B5ONE",))
        kept, info = sec.parse_zip(path)
        assert kept == [] and info.rejected == {"10b5-1 planned trade": 1}

    def test_a_missing_column_refuses_the_file_and_names_it(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("SUBMISSION.tsv", "ACCESSION_NUMBER\tFILING_DATE\n")
            z.writestr("REPORTINGOWNER.tsv", "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNER_RELATIONSHIP\n")
            z.writestr("NONDERIV_TRANS.tsv", "ACCESSION_NUMBER\n")
        path = tmp_path / "2010q1_form345.zip"
        path.write_bytes(buf.getvalue())
        with pytest.raises(sec.SourceFormatError, match="DOCUMENT_TYPE"):
            sec.parse_zip(path)

    def test_quoted_fields_and_a_byte_order_mark_still_parse(self, tmp_path):
        raw = sec_zip([_sub(ACC)], [_own(ACC)], [_tr(ACC)])
        src, buf = zipfile.ZipFile(io.BytesIO(raw)), io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in src.namelist():
                text = src.read(name).decode()
                if name == "NONDERIV_TRANS.tsv":
                    lines = text.splitlines()
                    text = "\n".join([lines[0]] + ["\t".join(f'"{v}"' for v in ln.split("\t")) for ln in lines[1:]]) + "\n"
                if name == "SUBMISSION.tsv":
                    text = "\ufeff" + text
                z.writestr(name, text.encode("utf-8"))
        path = tmp_path / "2010q1_form345.zip"
        path.write_bytes(buf.getvalue())
        kept, info = sec.parse_zip(path)
        assert len(kept) == 1 and kept[0]["shares"] == 1000 and not info.rejected

    def test_build_writes_purchases_and_a_manifest(self, tmp_path):
        import polars as pl

        raw = tmp_path / "raw"
        raw.mkdir()
        write(raw, [_sub(ACC)], [_own(ACC)], [_tr(ACC)])
        write(raw, [_sub("b1", filed="10-MAY-2010")], [_own("b1")], [_tr("b1", traded="07-MAY-2010", code="S", ad="D")],
              name="2010q2_form345.zip")
        manifest = sec.build(raw, tmp_path / "out")
        frame = pl.read_parquet(tmp_path / "out" / sec.PURCHASES_FILE)
        assert frame.height == 1 and manifest["purchases"] == 1 and manifest["transaction_rows"] == 2
        assert manifest["rejected"] == {"not an open-market purchase": 1}
        assert {q["file"] for q in manifest["quarters"]} == {"2010q1_form345.zip", "2010q2_form345.zip"}
        assert all(len(q["sha256"]) == 64 for q in manifest["quarters"])


class TestHelpers:
    def test_dates(self):
        assert sec.parse_sec_date("05-MAR-2024") == date(2024, 3, 5)
        assert sec.parse_sec_date("5-mar-2024") == date(2024, 3, 5)
        assert sec.parse_sec_date("2024-03-05") == date(2024, 3, 5)
        assert sec.parse_sec_date("31-FEB-2024") is None and sec.parse_sec_date("") is None

    def test_roles(self):
        assert sec.roles("Director,Officer") == (True, True, False)
        assert sec.roles("TenPercentOwner") == (False, False, True)
        assert sec.roles("OTHER") == (False, False, False)

    def test_quarters(self):
        assert sec.quarter_range("2006q3", "2007q1") == [(2006, 3), (2006, 4), (2007, 1)]
        with pytest.raises(ValidationError):
            sec.quarter_range("2005q4", "2006q1")


class StandIn:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def get(self, url, headers, timeout, allow_redirects):
        self.calls.append((url, headers["User-Agent"]))
        status, body = self.replies.pop(0)
        return type("R", (), {"status_code": status, "content": body})()


class TestDownload:
    def _good(self):
        return sec_zip([_sub(ACC)], [_own(ACC)], [_tr(ACC)])

    def test_falls_back_to_the_second_path_and_sends_the_contact(self, tmp_path):
        s = StandIn([(404, b""), (200, self._good())])
        out = sec.download(tmp_path, [(2026, 3)], s, "research research@example.com", sleep=lambda _: None)
        assert out[0].status == "downloaded" and out[0].sha256
        assert "structureddata" in s.calls[0][0] and "datastandardsinnovation" in s.calls[1][0]
        assert all("@" in ua for _, ua in s.calls)

    def test_an_existing_zip_is_never_fetched_again(self, tmp_path):
        (tmp_path / "2010q1_form345.zip").write_bytes(self._good())
        s = StandIn([])
        out = sec.download(tmp_path, [(2010, 1)], s, "x@y.z", sleep=lambda _: None)
        assert out[0].status == "present" and s.calls == []

    def test_a_non_zip_reply_is_discarded(self, tmp_path):
        s = StandIn([(200, b"<html>rate limited</html>"), (404, b"")])
        out = sec.download(tmp_path, [(2010, 1)], s, "x@y.z", sleep=lambda _: None)
        assert out[0].status == "failed" and not list(tmp_path.glob("*.zip*"))

    def test_refuses_without_a_contact_address(self, tmp_path):
        with pytest.raises(ValidationError, match="contact"):
            sec.download(tmp_path, [(2010, 1)], StandIn([]), "signalforge", sleep=lambda _: None)
