"""Row encoding for the D1 market-data mirror.

Everything that decides what a mirrored row looks like lives here: the table
specs, the canonical text of a value, the SQL literals, the 32-bit row hash
(hash version "sfm1"), the D1 DDL and the statement and file chunking.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Iterator

HASH_VERSION = "sfm1"
SCHEMA_VERSION = "1"
MAX_STATEMENT_BYTES = 90_000
MAX_STATEMENT_ROWS = 500
ROWS_PER_FILE = 50_000
SIX_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class Column:
    name: str
    kind: str  # text | price | date | ts | bool | int
    nullable: bool = False


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[Column, ...]
    pk: tuple[str, ...]
    key_col: str
    date_col: str
    micro: tuple[str, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def pk_index(self) -> tuple[int, ...]:
        return tuple(self.names.index(c) for c in self.pk)

    @property
    def non_pk(self) -> tuple[str, ...]:
        return tuple(n for n in self.names if n not in self.pk) + ("row_hash",)

    def kind(self, name: str) -> str:
        return next(c.kind for c in self.columns if c.name == name)


def _c(name, kind, nullable=False):
    return Column(name, kind, nullable)


BARS = TableSpec(
    "bars",
    (
        _c("symbol", "text"), _c("trade_date", "date"), _c("source", "text"),
        _c("open", "price"), _c("high", "price"), _c("low", "price"), _c("close", "price"),
        _c("adj_close", "price", True), _c("volume", "int"), _c("volume_known", "bool"),
        _c("retrieved_at", "ts", True), _c("adjustment_method", "text", True),
        _c("quality_status", "text", True), _c("source_reference", "text", True),
    ),
    ("symbol", "trade_date", "source"), "symbol", "trade_date",
    ("open", "high", "low", "close", "adj_close", "volume"),
)
SERIES = TableSpec(
    "series",
    (
        _c("series_id", "text"), _c("observation_date", "date"), _c("source", "text"),
        _c("value", "price"), _c("retrieved_at", "ts", True),
    ),
    ("series_id", "observation_date", "source"), "series_id", "observation_date",
    ("value",),
)
CORPORATE_ACTIONS = TableSpec(
    "corporate_actions",
    (
        _c("symbol", "text"), _c("effective_date", "date"), _c("action_type", "text"),
        _c("source", "text"), _c("ratio", "price", True), _c("cash_amount", "price", True),
    ),
    ("symbol", "effective_date", "action_type", "source"), "symbol", "effective_date",
    ("ratio", "cash_amount"),
)
SPECS = (BARS, SERIES, CORPORATE_ACTIONS)


def canonical(value, kind: str):
    """The one text (or int) a value has on D1 and inside the hash."""
    if value is None:
        return None
    if kind == "price":
        exact = Decimal(value)
        q = exact.quantize(SIX_PLACES)
        if q != exact:
            raise ValueError(f"price {exact} needs more than 6 decimal places")
        if q == 0:
            q = abs(q)
        return format(q, "f")
    if kind == "date":
        return value.isoformat() if isinstance(value, date) else str(value)
    if kind == "ts":
        if isinstance(value, datetime):
            return value.isoformat(sep=" ", timespec="microseconds")
        return str(value)
    if kind == "bool":
        return 1 if value else 0
    if kind == "int":
        return int(value)
    return str(value)


def canonical_row(spec: TableSpec, raw) -> tuple:
    return tuple(canonical(v, c.kind) for v, c in zip(raw, spec.columns))


def row_hash(spec: TableSpec, canon: tuple) -> int:
    parts = [f"{HASH_VERSION}\n{spec.name}\n"]
    for v in canon:
        if v is None:
            parts.append("N;")
        else:
            s = str(v)
            parts.append(f"{len(s)}:{s};")
    digest = hashlib.sha256("".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def prepare_row(spec: TableSpec, raw) -> tuple:
    """Canonical values in column order with the row hash appended."""
    canon = canonical_row(spec, raw)
    return canon + (row_hash(spec, canon),)


def pk_of(spec: TableSpec, prepared: tuple) -> tuple:
    return tuple(prepared[i] for i in spec.pk_index)


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(int(value))
    if "\x00" in value:
        raise ValueError("NUL byte in a string value")
    return "'" + value.replace("'", "''") + "'"


def _column_ddl(col: Column) -> str:
    sql_type = "INTEGER" if col.kind in ("bool", "int") else "TEXT"
    parts = [col.name, sql_type]
    if not col.nullable:
        parts.append("NOT NULL")
    if col.kind == "bool":
        parts.append(f"CHECK ({col.name} IN (0,1))")
    return " ".join(parts)


def table_ddl(spec: TableSpec) -> str:
    cols = [_column_ddl(c) for c in spec.columns] + ["row_hash INTEGER NOT NULL"]
    cols.append(f"PRIMARY KEY ({', '.join(spec.pk)})")
    return (
        f"CREATE TABLE IF NOT EXISTS {spec.name} (\n    "
        + ",\n    ".join(cols)
        + "\n) STRICT, WITHOUT ROWID;"
    )


STATE_DDL = (
    "CREATE TABLE IF NOT EXISTS mirror_state (\n"
    "    key TEXT PRIMARY KEY,\n    value TEXT\n) STRICT, WITHOUT ROWID;"
)


def state_upsert(key: str, value: str, *, replace: bool) -> str:
    action = "DO UPDATE SET value = excluded.value" if replace else "DO NOTHING"
    return (
        f"INSERT INTO mirror_state (key, value) VALUES ({sql_literal(key)}, "
        f"{sql_literal(value)}) ON CONFLICT(key) {action};"
    )


def schema_statements() -> list[str]:
    out = [table_ddl(s) for s in SPECS] + [STATE_DDL]
    out.append(state_upsert("schema_version", SCHEMA_VERSION, replace=False))
    out.append(state_upsert("hash_version", HASH_VERSION, replace=False))
    return out


def _upsert_frame(spec: TableSpec) -> tuple[str, str]:
    cols = ", ".join(spec.names + ("row_hash",))
    sets = ", ".join(f"{c} = excluded.{c}" for c in spec.non_pk)
    head = f"INSERT INTO {spec.name} ({cols}) VALUES "
    tail = f" ON CONFLICT({', '.join(spec.pk)}) DO UPDATE SET {sets};"
    return head, tail


def upsert_statements(spec: TableSpec, rows: Iterable[tuple]) -> Iterator[str]:
    head, tail = _upsert_frame(spec)
    fixed = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
    batch: list[str] = []
    size = fixed
    for row in rows:
        text = "(" + ",".join(sql_literal(v) for v in row) + ")"
        n = len(text.encode("utf-8")) + 1
        if fixed + n > MAX_STATEMENT_BYTES:
            raise ValueError("a single row exceeds the statement size cap")
        if batch and (size + n > MAX_STATEMENT_BYTES or len(batch) >= MAX_STATEMENT_ROWS):
            yield head + ",".join(batch) + tail
            batch, size = [], fixed
        batch.append(text)
        size += n
    if batch:
        yield head + ",".join(batch) + tail


def delete_statement(spec: TableSpec, pk: tuple) -> str:
    where = " AND ".join(f"{c} = {sql_literal(v)}" for c, v in zip(spec.pk, pk))
    return f"DELETE FROM {spec.name} WHERE {where};"


def _render_chunk(chunk: list) -> str:
    out: list[str] = []
    for (op, _), run in itertools.groupby(chunk, key=lambda it: (it[0], it[1].name)):
        run = list(run)
        spec = run[0][1]
        if op == "d":
            out.extend(delete_statement(spec, it[2]) for it in run)
        else:
            out.extend(upsert_statements(spec, [it[2] for it in run]))
    return "\n".join(out) + "\n"


def render_files(items: Iterable[tuple], rows_per_file: int) -> Iterator[tuple[str, int]]:
    """Yield (sql text, item count). Items are ("u", spec, prepared_row) or
    ("d", spec, pk). A file holds exactly rows_per_file items except the last.
    No BEGIN, COMMIT or PRAGMA: D1 import rejects them."""
    it = iter(items)
    while True:
        chunk = list(itertools.islice(it, rows_per_file))
        if not chunk:
            return
        yield _render_chunk(chunk), len(chunk)
