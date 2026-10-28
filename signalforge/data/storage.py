from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

import duckdb
import pyarrow as pa

from signalforge.core.errors import DataQualityError, ValidationError
from signalforge.core.types import Bar, CorporateAction, QualityStatus

DEFAULT_PATH = Path("data") / "warehouse" / "signalforge.duckdb"

TABLES = (
    "raw_records", "bars", "quarantine", "series", "corporate_actions",
    "ingest_runs", "data_revisions", "quality_flags",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_records (
    source VARCHAR NOT NULL,
    source_reference VARCHAR,
    payload VARCHAR NOT NULL,
    retrieved_at TIMESTAMP,
    content_sha256 VARCHAR NOT NULL,
    ingest_run_id VARCHAR
);
CREATE UNIQUE INDEX IF NOT EXISTS raw_records_content
    ON raw_records (source, source_reference, content_sha256);
CREATE TABLE IF NOT EXISTS bars (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open DECIMAL(18,6) NOT NULL,
    high DECIMAL(18,6) NOT NULL,
    low DECIMAL(18,6) NOT NULL,
    close DECIMAL(18,6) NOT NULL,
    adj_close DECIMAL(18,6),
    volume BIGINT NOT NULL,
    volume_known BOOLEAN NOT NULL DEFAULT TRUE,
    source VARCHAR NOT NULL,
    retrieved_at TIMESTAMP,
    adjustment_method VARCHAR,
    quality_status VARCHAR,
    source_reference VARCHAR,
    PRIMARY KEY (symbol, trade_date, source)
);
CREATE TABLE IF NOT EXISTS quarantine (
    symbol VARCHAR,
    trade_date DATE,
    source VARCHAR,
    rule VARCHAR NOT NULL,
    detail VARCHAR,
    raw_payload VARCHAR,
    quarantined_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS quality_flags (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    source VARCHAR NOT NULL,
    rule VARCHAR NOT NULL,
    detail VARCHAR,
    flagged_at TIMESTAMP,
    PRIMARY KEY (symbol, trade_date, source, rule)
);
CREATE TABLE IF NOT EXISTS series (
    series_id VARCHAR NOT NULL,
    observation_date DATE NOT NULL,
    value DECIMAL(18,6) NOT NULL,
    source VARCHAR NOT NULL,
    retrieved_at TIMESTAMP,
    PRIMARY KEY (series_id, observation_date, source)
);
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol VARCHAR NOT NULL,
    effective_date DATE NOT NULL,
    action_type VARCHAR NOT NULL,
    ratio DECIMAL(18,6),
    cash_amount DECIMAL(18,6),
    source VARCHAR NOT NULL,
    PRIMARY KEY (symbol, effective_date, action_type, source)
);
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id VARCHAR,
    provider VARCHAR,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    rows_in BIGINT,
    rows_accepted BIGINT,
    rows_quarantined BIGINT,
    status VARCHAR,
    error VARCHAR
);
CREATE TABLE IF NOT EXISTS data_revisions (
    entity VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    source VARCHAR NOT NULL,
    field VARCHAR NOT NULL,
    old_value VARCHAR,
    new_value VARCHAR,
    detected_at TIMESTAMP,
    ingest_run_id VARCHAR
);
"""

BAR_KEYS = [("symbol", "VARCHAR"), ("trade_date", "DATE"), ("source", "VARCHAR")]
BAR_VALUES = [
    ("open", "DECIMAL(18,6)"), ("high", "DECIMAL(18,6)"), ("low", "DECIMAL(18,6)"),
    ("close", "DECIMAL(18,6)"), ("adj_close", "DECIMAL(18,6)"), ("volume", "BIGINT"),
    # volume_known decides whether the liquidity breaker and the
    # participation cap apply at all, so losing it on a round trip turned
    # "nobody told us" into "nothing traded".
    ("volume_known", "BOOLEAN"),
]
BAR_EXTRAS = [
    ("retrieved_at", "TIMESTAMP"), ("adjustment_method", "VARCHAR"),
    ("quality_status", "VARCHAR"), ("source_reference", "VARCHAR"),
]
SERIES_KEYS = [("series_id", "VARCHAR"), ("observation_date", "DATE"), ("source", "VARCHAR")]
SERIES_VALUES = [("value", "DECIMAL(18,6)")]
SERIES_EXTRAS = [("retrieved_at", "TIMESTAMP")]


@dataclass(frozen=True)
class UpsertResult:
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0


def _utc_naive(value) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _s(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc_naive(value).isoformat(sep=" ")
    if isinstance(value, QualityStatus):
        return value.value
    return str(value)


def _arrow(columns: list[str], rows: list[tuple]) -> pa.Table:
    return pa.table(
        {c: pa.array([r[i] for r in rows], type=pa.string()) for i, c in enumerate(columns)}
    )


def _weekdays_between(a: date, b: date) -> int:
    full_weeks, extra = divmod((b - a).days + 1, 7)
    n = full_weeks * 5
    for i in range(extra):
        if (a + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            n += 1
    return n


class Warehouse:
    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)
        self._migrate()

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves
    # an existing table alone, so a new column has to be added explicitly or
    # every pre-existing database fails to open.
    #
    # ADDITIVE ONLY, by rule: add a missing column with a default and do
    # nothing else. No drops, no renames, no type changes, no backfill that
    # could rewrite a stored value. A migration that can lose data is worse
    # than a schema mismatch, because the mismatch is loud and the loss is not.
    _ADDED_COLUMNS = (
        ("bars", "volume_known", "BOOLEAN NOT NULL DEFAULT TRUE"),
    )

    def _migrate(self) -> None:
        for table, column, definition in self._ADDED_COLUMNS:
            present = {
                row[0]
                for row in self.con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = ?",
                    [table],
                ).fetchall()
            }
            if not present:
                continue  # the table itself does not exist; SCHEMA will own it
            if column in present:
                continue
            self.con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.con.close()

    def count(self, table: str) -> int:
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}")
        return self.con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]

    def table_counts(self) -> dict[str, int]:
        return {t: self.count(t) for t in TABLES}

    def _stage(self, name: str, columns: list[str], rows: list[tuple]) -> None:
        self.con.register("_arrow_in", _arrow(columns, rows))
        try:
            self.con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS SELECT * FROM _arrow_in")
        finally:
            self.con.unregister("_arrow_in")

    def _upsert(self, table, entity, keys, values, extras, rows, run_id, detected_at) -> UpsertResult:
        if not rows:
            return UpsertResult()
        cols = keys + values + extras
        names = [c for c, _ in cols]
        seen = set()
        for r in rows:
            k = tuple(r[: len(keys)])
            if k in seen:
                raise DataQualityError(f"duplicate key in one {table} batch: {k}")
            seen.add(k)
        self._stage("stg_raw", names, rows)
        casts = ", ".join(f"CAST({n} AS {t}) AS {n}" for n, t in cols)
        self.con.execute(f"CREATE OR REPLACE TEMP TABLE stg AS SELECT {casts} FROM stg_raw")
        key_names = [c for c, _ in keys]
        val_names = [c for c, _ in values]
        extra_names = [c for c, _ in extras]
        join = " AND ".join(f"s.{k} = b.{k}" for k in key_names)
        select = ", ".join(
            [f"s.{c}" for c in key_names + val_names + extra_names] + [f"b.{c}" for c in val_names]
        )
        matched = self.con.execute(f"SELECT {select} FROM stg s JOIN {table} b ON {join}").fetchall()
        nk, nv, ne = len(key_names), len(val_names), len(extra_names)
        revisions, updates, unchanged = [], [], 0
        stamp = _s(detected_at)
        for row in matched:
            key = row[:nk]
            new, extra, old = row[nk : nk + nv], row[nk + nv : nk + nv + ne], row[nk + nv + ne :]
            changed = [i for i in range(nv) if new[i] != old[i]]
            if not changed:
                unchanged += 1
                continue
            for i in changed:
                revisions.append((
                    entity, key[0], _s(key[1]), key[2], val_names[i],
                    None if old[i] is None else str(old[i]),
                    None if new[i] is None else str(new[i]), stamp, run_id,
                ))
            updates.append((list(new) + list(extra) + list(key)))
        if revisions:
            self._stage(
                "stg_rev",
                ["entity", "symbol", "trade_date", "source", "field", "old_value",
                 "new_value", "detected_at", "ingest_run_id"],
                revisions,
            )
            self.con.execute(
                "INSERT INTO data_revisions SELECT entity, symbol, CAST(trade_date AS DATE), "
                "source, field, old_value, new_value, CAST(detected_at AS TIMESTAMP), "
                "ingest_run_id FROM stg_rev"
            )
        if updates:
            sets = ", ".join(f"{c} = ?" for c in val_names + extra_names)
            where = " AND ".join(f"{k} = ?" for k in key_names)
            self.con.executemany(f"UPDATE {table} SET {sets} WHERE {where}", updates)
        cols_sql = ", ".join(names)
        s_cols = ", ".join(f"s.{n}" for n in names)
        before = self.count(table)
        self.con.execute(
            f"INSERT INTO {table} ({cols_sql}) SELECT {s_cols} FROM stg s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} b WHERE {join})"
        )
        inserted = self.count(table) - before
        return UpsertResult(inserted=inserted, revised=len(updates), unchanged=unchanged)

    def upsert_bars(
        self,
        bars: Iterable[Bar],
        meta: dict | None = None,
        run_id: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> UpsertResult:
        meta = meta or {}
        now = _utc_naive(retrieved_at)
        rows = []
        for b in bars:
            m = meta.get((b.symbol, b.trade_date, b.source), {})
            rows.append((
                b.symbol, _s(b.trade_date), b.source,
                _s(b.open), _s(b.high), _s(b.low), _s(b.close), _s(b.adj_close), _s(b.volume),
                "true" if b.volume_known else "false",
                _s(_utc_naive(m.get("retrieved_at") or now)),
                m.get("adjustment_method"),
                _s(m.get("quality_status", QualityStatus.OK)),
                m.get("source_reference"),
            ))
        return self._tx(
            lambda: self._upsert("bars", "bar", BAR_KEYS, BAR_VALUES, BAR_EXTRAS, rows, run_id, now)
        )

    def upsert_series(self, rows: Iterable[dict], run_id: str | None = None) -> UpsertResult:
        now = _utc_naive(None)
        data = [
            (
                r["series_id"], _s(r["observation_date"]), r["source"], _s(r["value"]),
                _s(_utc_naive(r.get("retrieved_at"))),
            )
            for r in rows
        ]
        return self._tx(
            lambda: self._upsert(
                "series", "series", SERIES_KEYS, SERIES_VALUES, SERIES_EXTRAS, data, run_id, now
            )
        )

    def transaction(self, fn):
        """Run `fn` inside one transaction. Roll back and re-raise on failure.

        Public because the learning layer needs it too: `ExperimentRegistry`
        writes an experiment row and the row that spends the hold-out, and a
        failure between them left the experiment recorded while the hold-out
        read as unspent. F-13 was this same shape -- `closed_trades` was
        private and imported across modules anyway -- so this is named rather
        than reached into. `_tx` stays as an alias for the existing callers.
        """
        self.con.execute("BEGIN")
        try:
            out = fn()
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")
        return out

    # The original private name, kept so existing call sites do not churn.
    _tx = transaction

    def insert_raw_records(self, rows: Iterable[dict]) -> int:
        data = [
            (r["source"], r.get("source_reference"), r["payload"], _s(_utc_naive(r.get("retrieved_at"))),
             r["content_sha256"], r.get("ingest_run_id"))
            for r in rows
        ]
        if not data:
            return 0
        self._stage(
            "stg_in",
            ["source", "source_reference", "payload", "retrieved_at", "content_sha256", "ingest_run_id"],
            data,
        )
        before = self.count("raw_records")
        # A rolling provider URL changes its start/end dates even when a
        # returned record has not changed. Deduplicate by source and content,
        # retaining the first retrieval's immutable provenance. Including the
        # request URL here duplicated unchanged history on every new window.
        self.con.execute(
            "INSERT INTO raw_records SELECT source, source_reference, payload, "
            "CAST(retrieved_at AS TIMESTAMP), content_sha256, ingest_run_id FROM ("
            "SELECT *, row_number() OVER (PARTITION BY source, content_sha256 "
            "ORDER BY retrieved_at) AS rn FROM stg_in) s WHERE rn = 1 AND NOT EXISTS ("
            "SELECT 1 FROM raw_records r WHERE r.source = s.source "
            "AND r.content_sha256 = s.content_sha256)"
        ).fetchall()
        return self.count("raw_records") - before

    def insert_quarantine(self, rows: Iterable[dict]) -> int:
        data = [
            (r.get("symbol"), _s(r.get("trade_date")), r.get("source"), r["rule"], r.get("detail"),
             r.get("raw_payload"), _s(_utc_naive(r.get("quarantined_at"))))
            for r in rows
        ]
        if not data:
            return 0
        self._stage(
            "stg_in",
            ["symbol", "trade_date", "source", "rule", "detail", "raw_payload", "quarantined_at"],
            data,
        )
        before = self.count("quarantine")
        self.con.execute(
            "INSERT INTO quarantine SELECT symbol, CAST(trade_date AS DATE), source, rule, detail, "
            "raw_payload, CAST(quarantined_at AS TIMESTAMP) FROM ("
            "SELECT *, row_number() OVER (PARTITION BY symbol, trade_date, source, rule, raw_payload "
            "ORDER BY quarantined_at) AS rn FROM stg_in) s WHERE rn = 1 AND NOT EXISTS ("
            "SELECT 1 FROM quarantine q WHERE q.symbol IS NOT DISTINCT FROM s.symbol "
            "AND q.trade_date IS NOT DISTINCT FROM CAST(s.trade_date AS DATE) "
            "AND q.source IS NOT DISTINCT FROM s.source AND q.rule = s.rule "
            "AND q.raw_payload IS NOT DISTINCT FROM s.raw_payload)"
        ).fetchall()
        return self.count("quarantine") - before

    def insert_flags(self, rows: Iterable[dict]) -> int:
        data = [
            (r["symbol"], _s(r["trade_date"]), r["source"], r["rule"], r.get("detail"),
             _s(_utc_naive(r.get("flagged_at"))))
            for r in rows
        ]
        if not data:
            return 0
        self._stage("stg_in", ["symbol", "trade_date", "source", "rule", "detail", "flagged_at"], data)
        before = self.count("quality_flags")
        self.con.execute(
            "INSERT INTO quality_flags SELECT symbol, CAST(trade_date AS DATE), source, rule, detail, "
            "CAST(flagged_at AS TIMESTAMP) FROM ("
            "SELECT *, row_number() OVER (PARTITION BY symbol, trade_date, source, rule "
            "ORDER BY flagged_at) AS rn FROM stg_in) s WHERE rn = 1 AND NOT EXISTS ("
            "SELECT 1 FROM quality_flags q WHERE q.symbol = s.symbol "
            "AND q.trade_date = CAST(s.trade_date AS DATE) AND q.source = s.source AND q.rule = s.rule)"
        ).fetchall()
        return self.count("quality_flags") - before

    def upsert_corporate_actions(self, rows: Iterable[dict]) -> int:
        data = [
            (r["symbol"], _s(r["effective_date"]), r["action_type"], _s(r.get("ratio")),
             _s(r.get("cash_amount")), r["source"])
            for r in rows
        ]
        if not data:
            return 0
        self._stage(
            "stg_in", ["symbol", "effective_date", "action_type", "ratio", "cash_amount", "source"], data
        )
        before = self.count("corporate_actions")
        self.con.execute(
            "INSERT INTO corporate_actions SELECT symbol, CAST(effective_date AS DATE), action_type, "
            "CAST(ratio AS DECIMAL(18,6)), CAST(cash_amount AS DECIMAL(18,6)), source FROM ("
            "SELECT *, row_number() OVER (PARTITION BY symbol, effective_date, action_type, source "
            "ORDER BY symbol) AS rn FROM stg_in) s WHERE rn = 1 AND NOT EXISTS ("
            "SELECT 1 FROM corporate_actions c WHERE c.symbol = s.symbol "
            "AND c.effective_date = CAST(s.effective_date AS DATE) "
            "AND c.action_type = s.action_type AND c.source = s.source)"
        ).fetchall()
        return self.count("corporate_actions") - before

    def corporate_action_dates(self) -> set[tuple[str, date]]:
        rows = self.con.execute("SELECT symbol, effective_date FROM corporate_actions").fetchall()
        return {(s, d) for s, d in rows}

    def load_corporate_actions(
        self,
        symbols: Iterable[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        source: str | None = None,
    ) -> list[CorporateAction]:
        where, params = [], []
        if symbols is not None:
            symbols = list(symbols)
            if not symbols:
                return []
            where.append(f"symbol IN ({', '.join('?' * len(symbols))})")
            params += symbols
        if start is not None:
            where.append("effective_date >= ?")
            params.append(start)
        if end is not None:
            where.append("effective_date <= ?")
            params.append(end)
        if source is not None:
            where.append("source = ?")
            params.append(source)
        sql = "SELECT symbol, effective_date, action_type, ratio, cash_amount, source FROM corporate_actions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY effective_date, symbol, action_type, source"
        actions: dict[tuple, tuple[tuple, CorporateAction]] = {}
        for symbol, effective_date, action_type, ratio, cash, src in self.con.execute(sql, params).fetchall():
            if action_type == "split":
                if ratio is None:
                    raise ValidationError(f"{symbol} {effective_date}: split stored with no ratio")
                action = CorporateAction.split(symbol, effective_date, ratio, src)
            elif action_type == "dividend":
                action = CorporateAction.dividend(symbol, effective_date, cash, src)
            else:
                raise ValidationError(f"{symbol} {effective_date}: unknown action type {action_type!r}")
            values = (ratio, cash)
            seen = actions.get((symbol, effective_date, action_type))
            if seen is None:
                actions[(symbol, effective_date, action_type)] = (values, action)
            elif seen[0] != values:
                raise ValidationError(
                    f"{action.action_id}: sources disagree ({seen[1].source} {seen[0]} "
                    f"vs {src} {values})"
                )
        return [action for _, action in actions.values()]

    def record_run(
        self, *, run_id, provider, started_at, finished_at, rows_in, rows_accepted,
        rows_quarantined, status, error=None,
    ) -> None:
        self.con.execute(
            "INSERT INTO ingest_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, provider, _utc_naive(started_at), _utc_naive(finished_at), rows_in,
             rows_accepted, rows_quarantined, status, error],
        )

    def load_bars(
        self,
        symbols: Iterable[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        source: str | None = None,
    ) -> list[Bar]:
        where, params = [], []
        if symbols is not None:
            symbols = list(symbols)
            if not symbols:
                return []
            where.append(f"symbol IN ({', '.join('?' * len(symbols))})")
            params += symbols
        if start is not None:
            where.append("trade_date >= ?")
            params.append(start)
        if end is not None:
            where.append("trade_date <= ?")
            params.append(end)
        if source is not None:
            where.append("source = ?")
            params.append(source)
        sql = ("SELECT symbol, trade_date, open, high, low, close, volume, adj_close, "
               "source, volume_known FROM bars")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY symbol, trade_date, source"
        return [
            Bar(symbol=r[0], trade_date=r[1], open=r[2], high=r[3], low=r[4], close=r[5],
                volume=r[6], adj_close=r[7], source=r[8], volume_known=bool(r[9]))
            for r in self.con.execute(sql, params).fetchall()
        ]

    def latest_bars(
        self, symbols: list[str] | None = None, source: str | None = None
    ) -> dict[str, Bar]:
        """The most recent stored bar per symbol.

        Feeds `validate_bar_records(prior_bars=...)` so an incremental pull's
        first bar is still compared against something. Without it the jump,
        stale-price and adjustment rules silently skip the batch boundary,
        which is exactly where a split-sized overnight move would land.
        """
        params: list = []
        clauses: list[str] = []
        if symbols:
            clauses.append(f"symbol IN ({', '.join('?' * len(symbols))})")
            params += symbols
        if source is not None:
            # Continuity is per (symbol, source). A bar from a different
            # provider is not the predecessor of this batch's first bar:
            # treating it as one turned a 9.5-year dataset boundary into
            # 2,500 "missing observations" for AAPL.
            clauses.append("source = ?")
            params.append(source)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT symbol, trade_date, open, high, low, close, volume, adj_close, "
            "source, volume_known FROM bars" + where + " QUALIFY ROW_NUMBER() OVER "
            "(PARTITION BY symbol ORDER BY trade_date DESC, source) = 1"
        )
        return {
            r[0]: Bar(
                symbol=r[0], trade_date=r[1], open=r[2], high=r[3], low=r[4],
                close=r[5], volume=r[6], adj_close=r[7], source=r[8],
                volume_known=bool(r[9]),
            )
            for r in self.con.execute(sql, params).fetchall()
        }

    def stale_runs(
        self, symbols: list[str] | None = None, source: str | None = None
    ) -> dict[str, int]:
        """Length of the trailing run of identical closes, per symbol.

        A run of four stored identical closes plus one more arriving is five,
        and the stale-price rule should say so. Counting only within the new
        batch would never reach the threshold on a daily pull.
        """
        params: list = []
        clauses: list[str] = []
        if symbols:
            clauses.append(f"symbol IN ({', '.join('?' * len(symbols))})")
            params += symbols
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.con.execute(
            "SELECT symbol, trade_date, close FROM bars" + where
            + " ORDER BY symbol, trade_date",
            params,
        ).fetchall()
        runs: dict[str, int] = {}
        last_close: dict[str, Decimal] = {}
        for symbol, _, close in rows:
            if symbol in last_close and last_close[symbol] == close:
                runs[symbol] = runs.get(symbol, 1) + 1
            else:
                runs[symbol] = 1
            last_close[symbol] = close
        return runs

    def load_series(self, series_id: str, source: str | None = None) -> list[tuple[date, Decimal]]:
        sql = "SELECT observation_date, value FROM series WHERE series_id = ?"
        params: list = [series_id]
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY observation_date, source"
        return [(d, v) for d, v in self.con.execute(sql, params).fetchall()]

    def coverage_report(self) -> list[dict]:
        out = []
        bar_rows = self.con.execute(
            "SELECT symbol, source, count(*), min(trade_date), max(trade_date), "
            "count(DISTINCT trade_date) FILTER (WHERE dayofweek(trade_date) BETWEEN 1 AND 5), "
            "max(retrieved_at) FROM bars GROUP BY symbol, source ORDER BY symbol, source"
        ).fetchall()
        for symbol, source, rows, lo, hi, weekday_present, last in bar_rows:
            out.append({
                "kind": "bars", "symbol": symbol, "source": source, "rows": rows, "min_date": lo, "max_date": hi,
                "missing_business_days": _weekdays_between(lo, hi) - weekday_present,
                "last_retrieved_at": last,
            })
        series_rows = self.con.execute(
            "SELECT series_id, source, observation_date, retrieved_at FROM series "
            "ORDER BY series_id, source, observation_date"
        ).fetchall()
        grouped: dict[str, list] = {}
        for sid, source, d, ts in series_rows:
            grouped.setdefault((sid, source), []).append((d, ts))
        for sid, source in sorted(grouped):
            items = grouped[(sid, source)]
            dates = sorted({d for d, _ in items})
            missing = None
            if len(dates) >= 2:
                gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
                if statistics.median(gaps) <= 4:
                    missing = _weekdays_between(dates[0], dates[-1]) - sum(
                        1 for d in dates if d.weekday() < 5
                    )
            out.append({
                "kind": "series", "symbol": sid, "source": source, "rows": len(items), "min_date": dates[0],
                "max_date": dates[-1], "missing_business_days": missing,
                "last_retrieved_at": max((ts for _, ts in items if ts is not None), default=None),
            })
        return out

    def fingerprint(
        self,
        symbols: Iterable[str] | None,
        start: date | None,
        end: date | None,
        source: str | None = None,
    ) -> str:
        h = hashlib.sha256()
        h.update(b"signalforge-bars-v1\n")
        for b in self.load_bars(symbols, start, end, source):
            adj = "" if b.adj_close is None else f"{b.adj_close:.6f}"
            line = (
                f"{b.symbol}|{b.trade_date.isoformat()}|{b.source}|{b.open:.6f}|{b.high:.6f}|"
                f"{b.low:.6f}|{b.close:.6f}|{adj}|{b.volume}\n"
            )
            h.update(line.encode("utf-8"))
        return h.hexdigest()
