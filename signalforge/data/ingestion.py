from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from signalforge.data.providers import FetchResult
from signalforge.data.storage import Warehouse
from signalforge.data.validation import (
    QuarantinedRecord,
    validate_bar_records,
    validate_series_records,
)

PROVENANCE_KEYS = ("retrieved_at", "source_reference")


@dataclass
class IngestResult:
    run_id: str
    provider: str
    status: str = "ok"
    rows_in: int = 0
    rows_accepted: int = 0
    rows_quarantined: int = 0
    rows_suspect: int = 0
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    raw_inserted: int = 0
    rules_fired: Counter = field(default_factory=Counter)
    missing: dict[str, int] = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _payload(record: dict) -> str:
    body = {k: v for k, v in record.items() if k not in PROVENANCE_KEYS}
    return json.dumps(body, default=str, sort_keys=True)


def _raw_rows(result: FetchResult, run_id: str) -> list[dict]:
    rows = []
    for record in result.records:
        payload = _payload(record)
        rows.append({
            "source": result.source, "source_reference": result.source_reference,
            "payload": payload, "retrieved_at": result.retrieved_at,
            "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "ingest_run_id": run_id,
        })
    return rows


def _quarantine_rows(quarantined: list[QuarantinedRecord], result: FetchResult, now) -> list[dict]:
    rows = []
    for q in quarantined:
        r = q.record
        rows.append({
            "symbol": r.get("symbol") or r.get("series_id"),
            "trade_date": _as_date(r.get("trade_date") or r.get("observation_date")),
            "source": r.get("source") or result.source,
            "rule": q.rule, "detail": q.detail, "raw_payload": _payload(r), "quarantined_at": now,
        })
    return rows


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def ingest(provider, warehouse: Warehouse, *, clock=_utcnow, as_of: date | None = None,
           validation_options: dict | None = None, **fetch_kwargs) -> IngestResult:
    """Pull, store raw, validate, store clean/quarantine, record the run.

    Rate limiting lives in the provider (injectable `sleep`), where the requests are made.
    """
    started = clock()
    as_of = as_of or started.date()
    run_id = uuid.uuid4().hex
    out = IngestResult(run_id=run_id, provider=provider.name)
    options = validation_options or {}
    try:
        for result in provider.fetch(**fetch_kwargs):
            out.rows_in += len(result.records)
            out.raw_inserted += warehouse.insert_raw_records(_raw_rows(result, run_id))
            if result.kind == "series":
                _ingest_series(result, warehouse, out, run_id, as_of, started)
            else:
                _ingest_bars(result, warehouse, out, run_id, as_of, started, options)
    except Exception as exc:
        out.status = "failed"
        warehouse.record_run(
            run_id=run_id, provider=provider.name, started_at=started, finished_at=clock(),
            rows_in=out.rows_in, rows_accepted=out.rows_accepted,
            rows_quarantined=out.rows_quarantined, status="failed", error=str(exc),
        )
        raise
    warehouse.record_run(
        run_id=run_id, provider=provider.name, started_at=started, finished_at=clock(),
        rows_in=out.rows_in, rows_accepted=out.rows_accepted,
        rows_quarantined=out.rows_quarantined, status="ok", error=None,
    )
    return out


def _tally(out: IngestResult, upsert) -> None:
    out.inserted += upsert.inserted
    out.revised += upsert.revised
    out.unchanged += upsert.unchanged


def _ingest_series(result, warehouse, out, run_id, as_of, now) -> None:
    report = validate_series_records(result.records, as_of=as_of)
    warehouse.insert_quarantine(_quarantine_rows(report.quarantined, result, now))
    _tally(out, warehouse.upsert_series(report.accepted, run_id=run_id))
    out.rows_accepted += len(report.accepted)
    out.rows_quarantined += len(report.quarantined)
    for q in report.quarantined:
        out.rules_fired.update(q.rules)


def _ingest_bars(result, warehouse, out, run_id, as_of, now, options) -> None:
    warehouse.upsert_corporate_actions(result.corporate_actions)
    # Give the validator what is already stored, so the jump, stale-price and
    # adjustment rules can see across the batch boundary. Without this, the
    # first bar of every incremental pull is compared against nothing, which
    # is precisely where a split-sized overnight move lands.
    incoming = sorted({r.get("symbol") for r in result.records if r.get("symbol")})
    batch_sources = {r.get("source") for r in result.records if r.get("source")}
    batch_source = batch_sources.pop() if len(batch_sources) == 1 else None
    report = validate_bar_records(
        result.records,
        as_of=as_of,
        corporate_action_dates=warehouse.corporate_action_dates(),
        prior_bars=warehouse.latest_bars(incoming, source=batch_source) if incoming else None,
        prior_stale_runs=(
            warehouse.stale_runs(incoming, source=batch_source) if incoming else None
        ),
        **options,
    )
    warehouse.insert_quarantine(_quarantine_rows(report.quarantined, result, now))
    warehouse.insert_flags(
        {"symbol": f.symbol, "trade_date": f.trade_date, "source": f.source, "rule": f.rule,
         "detail": f.detail, "flagged_at": now}
        for f in report.flags
    )
    _tally(out, warehouse.upsert_bars(report.accepted, meta=report.meta, run_id=run_id))
    out.rows_accepted += len(report.accepted)
    out.rows_quarantined += len(report.quarantined)
    out.rows_suspect += len({(f.symbol, f.trade_date, f.source) for f in report.flags})
    out.rules_fired.update(report.rules_fired())
    for symbol, gaps in report.missing.items():
        out.missing[symbol] = out.missing.get(symbol, 0) + gaps
