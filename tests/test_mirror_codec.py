"""mirror codec: canonical values, literals, row hash, DDL, statement and file chunking."""

import hashlib
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from signalforge.mirror import codec
from signalforge.mirror.codec import BARS, CORPORATE_ACTIONS, SERIES

needs_strict = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 37), reason="STRICT tables need SQLite 3.37"
)


def bar_raw(i=0, **over):
    row = {
        "symbol": f"SYM{i % 7}", "trade_date": date(2024, 1, 1) + __import__("datetime").timedelta(days=i),
        "source": "tiingo", "open": Decimal("10.5"), "high": Decimal("11"), "low": Decimal("10"),
        "close": Decimal("10.75"), "adj_close": Decimal("10.7"), "volume": 1000 + i,
        "volume_known": True, "retrieved_at": datetime(2026, 10, 1, 12, 0, 0, 5),
        "adjustment_method": "split_div", "quality_status": "ok",
        "source_reference": "https://example.test/" + "x" * 150 + str(i),
    }
    row.update(over)
    return tuple(row[c] for c in BARS.names)


def test_decimal_renders_exactly_six_places():
    assert codec.canonical(Decimal("10.5"), "price") == "10.500000"
    assert codec.canonical(Decimal("3"), "price") == "3.000000"
    assert codec.canonical(Decimal("-0.000001"), "price") == "-0.000001"
    assert codec.canonical(Decimal("-0.000000"), "price") == "0.000000"
    assert codec.canonical(Decimal("1E+2"), "price") == "100.000000"


def test_decimal_refuses_more_than_six_places():
    with pytest.raises(ValueError):
        codec.canonical(Decimal("1.0000001"), "price")


def test_other_canonical_forms():
    assert codec.canonical(date(2024, 3, 5), "date") == "2024-03-05"
    assert codec.canonical(datetime(2026, 1, 2, 3, 4, 5), "ts") == "2026-01-02 03:04:05.000000"
    assert codec.canonical(True, "bool") == 1 and codec.canonical(False, "bool") == 0
    assert codec.canonical(None, "price") is None


def test_sql_literal_forms():
    assert codec.sql_literal(None) == "NULL"
    assert codec.sql_literal(7) == "7"
    assert codec.sql_literal("abc") == "'abc'"
    assert codec.sql_literal("o'brien") == "'o''brien'"
    assert codec.sql_literal("") == "''"


def test_sql_literal_refuses_nul():
    with pytest.raises(ValueError):
        codec.sql_literal("a\x00b")


def _expected_hash(table, fields):
    text = f"sfm1\n{table}\n"
    for f in fields:
        text += "N;" if f is None else f"{len(str(f))}:{f};"
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")


def test_row_hash_pinned_and_32bit():
    canon = codec.canonical_row(CORPORATE_ACTIONS, ("AAA", date(2024, 1, 2), "split", "manual", Decimal("2"), None))
    expected = _expected_hash(
        "corporate_actions", ["AAA", "2024-01-02", "split", "manual", "2.000000", None]
    )
    assert codec.row_hash(CORPORATE_ACTIONS, canon) == expected
    assert 0 <= expected < 2**32
    changed = codec.canonical_row(CORPORATE_ACTIONS, ("AAA", date(2024, 1, 2), "split", "manual", Decimal("3"), None))
    assert codec.row_hash(CORPORATE_ACTIONS, changed) != expected


def test_row_hash_distinguishes_none_from_empty_string():
    a = codec.canonical_row(BARS, bar_raw(adjustment_method=None))
    b = codec.canonical_row(BARS, bar_raw(adjustment_method=""))
    assert codec.row_hash(BARS, a) != codec.row_hash(BARS, b)


def test_row_hash_single_column_change():
    base = codec.row_hash(BARS, codec.canonical_row(BARS, bar_raw()))
    for col, val in [("close", Decimal("10.76")), ("volume", 1), ("volume_known", False),
                     ("quality_status", "bad")]:
        other = codec.row_hash(BARS, codec.canonical_row(BARS, bar_raw(**{col: val})))
        assert other != base, col


def test_schema_strict_without_rowid_no_index():
    for stmt in codec.schema_statements()[:4]:
        assert "STRICT, WITHOUT ROWID" in stmt
        assert "CREATE INDEX" not in stmt.upper() and "CREATE UNIQUE" not in stmt.upper()
    assert "PRIMARY KEY (symbol, trade_date, source)" in codec.table_ddl(BARS)
    assert codec.table_ddl(BARS).index("row_hash") > codec.table_ddl(BARS).index("source_reference")


def memdb():
    con = sqlite3.connect(":memory:")
    for stmt in codec.schema_statements():
        con.execute(stmt)
    return con


@needs_strict
def test_schema_executes_in_sqlite():
    con = memdb()
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"bars", "series", "corporate_actions", "mirror_state"} <= names
    state = dict(con.execute("SELECT key, value FROM mirror_state").fetchall())
    assert state == {"schema_version": "1", "hash_version": "sfm1"}
    for stmt in codec.schema_statements():
        con.execute(stmt)  # idempotent


def test_upsert_statements_respect_caps():
    rows = [codec.prepare_row(BARS, bar_raw(i)) for i in range(2000)]
    stmts = list(codec.upsert_statements(BARS, rows))
    assert stmts == list(codec.upsert_statements(BARS, rows))
    assert len(stmts) > 4
    total = 0
    for s in stmts:
        assert len(s.encode()) <= 90_000
        assert s.count("),(") + 1 <= 500
        total += s.count("),(") + 1
    assert total == 2000
    joined = "\n".join(stmts)
    for i in (0, 999, 1999):
        assert joined.count("x" * 150 + str(i) + "'") == 1


def test_oversized_single_row_refused():
    row = codec.prepare_row(BARS, bar_raw(source_reference="y" * 100_000))
    with pytest.raises(ValueError):
        list(codec.upsert_statements(BARS, [row]))


def test_files_split_at_rows_per_file():
    items = [("u", BARS, codec.prepare_row(BARS, bar_raw(i))) for i in range(25)]
    items += [("d", BARS, ("SYM1", "2020-01-01", "tiingo"))] * 3
    files = list(codec.render_files(items, 10))
    assert [n for _, n in files] == [10, 10, 8]


def test_no_transaction_or_pragma():
    items = [("u", BARS, codec.prepare_row(BARS, bar_raw(i))) for i in range(5)]
    items.append(("d", BARS, ("SYM1", "2020-01-01", "tiingo")))
    text = "".join(t for t, _ in codec.render_files(items, 100)).upper()
    for word in ("BEGIN", "COMMIT", "PRAGMA", "ROLLBACK"):
        assert word not in text


def apply(con, items):
    for text, _ in codec.render_files(items, 50_000):
        con.executescript(text)


@needs_strict
def test_sqlite_roundtrip_exact():
    con = memdb()
    raws = [bar_raw(0, adj_close=None, volume_known=False), bar_raw(1, close=Decimal("123456.123457"))]
    apply(con, [("u", BARS, codec.prepare_row(BARS, r)) for r in raws])
    rows = con.execute(
        "SELECT close, typeof(close), adj_close, volume_known, volume, retrieved_at FROM bars ORDER BY trade_date"
    ).fetchall()
    assert Decimal(rows[0][0]) == raws[0][BARS.names.index("close")]
    assert rows[0][1] == "text"
    assert rows[0][2] is None
    assert rows[0][3] == 0 and rows[1][3] == 1
    assert Decimal(rows[1][0]) == Decimal("123456.123457")
    assert rows[0][5] == "2026-10-01 12:00:00.000005"


@needs_strict
def test_upsert_twice_idempotent_and_applies_revision():
    con = memdb()
    first = codec.prepare_row(BARS, bar_raw(0))
    apply(con, [("u", BARS, first)])
    apply(con, [("u", BARS, first)])
    assert con.execute("SELECT count(*) FROM bars").fetchone()[0] == 1
    revised = codec.prepare_row(BARS, bar_raw(0, close=Decimal("99")))
    apply(con, [("u", BARS, revised)])
    close, h = con.execute("SELECT close, row_hash FROM bars").fetchone()
    assert close == "99.000000" and h == revised[-1]


@needs_strict
def test_delete_removes_only_named_keys():
    con = memdb()
    rows = [codec.prepare_row(BARS, bar_raw(i)) for i in range(3)]
    apply(con, [("u", BARS, r) for r in rows])
    apply(con, [("d", BARS, codec.pk_of(BARS, rows[1]))])
    left = con.execute("SELECT trade_date FROM bars ORDER BY trade_date").fetchall()
    assert [r[0] for r in left] == [rows[0][1], rows[2][1]]


@needs_strict
def test_apostrophe_in_value_roundtrips():
    con = memdb()
    row = codec.prepare_row(SERIES, ("O'Brien;--", date(2024, 1, 1), "fred", Decimal("-0.000001"), None))
    apply(con, [("u", SERIES, row)])
    assert con.execute("SELECT series_id, value FROM series").fetchone() == ("O'Brien;--", "-0.000001")


def test_row_hash_literal_regression_pin():
    raw = ("AAA", date(2024, 1, 2), "split", "manual", Decimal("2"), None)
    canon = codec.canonical_row(CORPORATE_ACTIONS, raw)
    assert codec.row_hash(CORPORATE_ACTIONS, canon) == 3423954422
