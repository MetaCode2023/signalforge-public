"""Report events for the deployed ingest API, plus a strict local validator.

The validator mirrors the Worker's contract so a bad event is refused before
anything is sent. Builders read the warehouse read-only. Only train and
validation experiments are ever exported; the held-out test window is not.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from signalforge import __version__

NAMESPACE = uuid.UUID("5f1d2c7e-8a34-4b6d-9e0f-51617ea9c0de")
MAX_BODY_BYTES = 65536
STALE_DAYS = 4
EXPORT_WINDOWS = ("train", "validation")
REJECTION_REASONS = (
    "overfit", "insufficient_trades", "drawdown_limit", "underperforms_baseline", "data_quality",
    "risk_rule_violation", "lookahead_bias", "cost_sensitivity", "unstable_parameters", "manual_review",
)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RUN_ID = _SLUG
_SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_OCCURRED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{32,}")
_SECRET_PATTERNS = (
    re.compile(r"[0-9a-fA-F]{48,}"),
    re.compile(r"(?i)(?<![a-z0-9])(sk|pk|rk)[-_][A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)bearer\s+\S{8,}"),
)
_ENVELOPE_KEYS = {"schema_version", "paper_only", "event_id", "event_type", "run_id", "occurred_at", "payload"}


class EventInvalid(ValueError):
    """A report event that the ingest API would reject. Messages name fields, never values."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ serialize

def _default(value):
    if isinstance(value, Decimal):
        return round(float(value), 6)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def serialize(event) -> bytes:
    """The one and only serialization; the signature covers exactly these bytes."""
    return json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default,
    ).encode("utf-8")


# ------------------------------------------------------------------ validator

def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _slug(value) -> bool:
    return isinstance(value, str) and bool(_SLUG.match(value))


def _iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        if _OCCURRED.match(value):
            return datetime.fromisoformat(value[:-1])
    except ValueError:
        return None
    return None


def _entropy(text: str) -> float:
    counts = {c: text.count(c) for c in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def _looks_secret(text: str) -> bool:
    if any(p.search(text) for p in _SECRET_PATTERNS):
        return True
    for run in _ALNUM_RUN.findall(text):
        if run != run.lower() and run != run.upper() and _entropy(run) >= 3.0:
            return True
    return False


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _keys(payload, required, optional, problems, where="payload"):
    for k in required:
        if k not in payload:
            problems.append(f"{where}.{k} is required")
    for k in payload:
        if k not in required and k not in optional:
            problems.append(f"{where}.{k} is not allowed")


def _check(problems, ok, message):
    if not ok:
        problems.append(message)


def _payload_heartbeat(p, problems):
    _keys(p, ("status",), ("engine_version", "uptime_s", "active_strategies", "risk_checks_ok"), problems)
    _check(problems, p.get("status") in ("running", "idle", "stopped", "error"), "payload.status invalid")
    if "engine_version" in p:
        _check(problems, _slug(p["engine_version"]), "payload.engine_version must be a slug")
    if "uptime_s" in p:
        _check(problems, _int(p["uptime_s"]) and p["uptime_s"] >= 0, "payload.uptime_s must be an int >= 0")
    if "risk_checks_ok" in p:
        _check(problems, isinstance(p["risk_checks_ok"], bool), "payload.risk_checks_ok must be a bool")
    if "active_strategies" in p:
        a = p["active_strategies"]
        ok = isinstance(a, list) and len(a) <= 50 and all(_slug(x) for x in a) and len(set(a)) == len(a)
        _check(problems, ok, "payload.active_strategies must be <= 50 unique slugs")


def _payload_paper_session(p, problems):
    _keys(p, ("strategy_id", "as_of", "equity", "cash", "realized_pnl", "unrealized_pnl"),
          ("max_drawdown_pct", "trades", "positions"), problems)
    _check(problems, _slug(p.get("strategy_id")), "payload.strategy_id must be a slug")
    _check(problems, _iso(p.get("as_of")) is not None, "payload.as_of must be ISO")
    equity, cash = p.get("equity"), p.get("cash")
    _check(problems, _num(equity) and equity >= 0, "payload.equity must be >= 0")
    _check(problems, _num(cash) and cash >= 0, "payload.cash must be >= 0")
    if _num(equity) and _num(cash):
        _check(problems, cash <= equity + 0.01, "payload.cash must not exceed equity")
    for k in ("realized_pnl", "unrealized_pnl"):
        _check(problems, _num(p.get(k)), f"payload.{k} must be a number")
    if "max_drawdown_pct" in p:
        _check(problems, _num(p["max_drawdown_pct"]) and 0 <= p["max_drawdown_pct"] <= 100,
               "payload.max_drawdown_pct must be 0..100")
    if "trades" in p:
        _check(problems, _int(p["trades"]) and p["trades"] >= 0, "payload.trades must be an int >= 0")
    if "positions" in p:
        positions = p["positions"]
        if not isinstance(positions, list) or len(positions) > 100:
            problems.append("payload.positions must be a list of <= 100")
            return
        for i, pos in enumerate(positions):
            if not isinstance(pos, dict):
                problems.append(f"payload.positions[{i}] must be an object")
                continue
            _keys(pos, ("symbol", "qty", "avg_cost", "market_value"), (), problems, f"payload.positions[{i}]")
            _check(problems, isinstance(pos.get("symbol"), str) and bool(_SYMBOL.match(pos["symbol"])),
                   f"payload.positions[{i}].symbol invalid")
            _check(problems, _int(pos.get("qty")) and pos["qty"] >= 0, f"payload.positions[{i}].qty invalid")
            for k in ("avg_cost", "market_value"):
                _check(problems, _num(pos.get(k)) and pos[k] >= 0, f"payload.positions[{i}].{k} invalid")


def _payload_backtest(p, problems):
    _keys(p, ("strategy_id", "dataset_version", "commit_sha", "return_pct", "max_drawdown_pct", "trades",
              "period_start", "period_end", "status"),
          ("sharpe", "baseline_strategy_id", "rejection_reason"), problems)
    _check(problems, _slug(p.get("strategy_id")), "payload.strategy_id must be a slug")
    _check(problems, _slug(p.get("dataset_version")), "payload.dataset_version must be a slug")
    _check(problems, isinstance(p.get("commit_sha"), str) and bool(_SHA.match(p["commit_sha"])),
           "payload.commit_sha must be 7-40 lowercase hex")
    r = p.get("return_pct")
    _check(problems, _num(r) and -1000 <= r <= 1e5, "payload.return_pct out of range")
    d = p.get("max_drawdown_pct")
    _check(problems, _num(d) and 0 <= d <= 100, "payload.max_drawdown_pct must be 0..100")
    _check(problems, _int(p.get("trades")) and p["trades"] >= 0, "payload.trades must be an int >= 0")
    start, end = _iso(p.get("period_start")), _iso(p.get("period_end"))
    _check(problems, start is not None and end is not None and start < end,
           "payload.period_start/period_end must be ISO with start < end")
    _check(problems, p.get("status") in ("candidate", "rejected", "verified"), "payload.status invalid")
    if "sharpe" in p:
        _check(problems, _num(p["sharpe"]), "payload.sharpe must be a number")
    if "baseline_strategy_id" in p:
        _check(problems, _slug(p["baseline_strategy_id"]), "payload.baseline_strategy_id must be a slug")
    if p.get("status") == "rejected":
        _check(problems, p.get("rejection_reason") in REJECTION_REASONS,
               "payload.rejection_reason required and must be an allowed reason when rejected")
    elif "rejection_reason" in p:
        problems.append("payload.rejection_reason only allowed when rejected")


def _payload_data_health(p, problems):
    _keys(p, ("dataset_version", "rows", "last_observation_at", "stale"), (), problems)
    _check(problems, _slug(p.get("dataset_version")), "payload.dataset_version must be a slug")
    _check(problems, _int(p.get("rows")) and p["rows"] >= 0, "payload.rows must be an int >= 0")
    _check(problems, _iso(p.get("last_observation_at")) is not None, "payload.last_observation_at must be ISO")
    _check(problems, isinstance(p.get("stale"), bool), "payload.stale must be a bool")


_PAYLOADS = {
    "heartbeat": _payload_heartbeat,
    "paper_session": _payload_paper_session,
    "backtest": _payload_backtest,
    "data_health": _payload_data_health,
}


def validate_event(event) -> None:
    """Raise EventInvalid unless the Worker would accept this event. Values are never echoed."""
    if not isinstance(event, dict):
        raise EventInvalid("event must be an object")
    try:
        raw = serialize(event)
    except (TypeError, ValueError):
        raise EventInvalid("event is not serializable") from None
    data = json.loads(raw)
    problems: list[str] = []
    if len(raw) > MAX_BODY_BYTES:
        problems.append(f"body exceeds {MAX_BODY_BYTES} bytes")
    _keys(data, tuple(_ENVELOPE_KEYS), (), problems, "event")
    _check(problems, type(data.get("schema_version")) in (int, float) and data["schema_version"] == 1,
           "schema_version must be 1")
    _check(problems, data.get("paper_only") is True, "paper_only must be true")
    try:
        uuid.UUID(str(data.get("event_id")))
        _check(problems, isinstance(data.get("event_id"), str), "event_id must be a uuid")
    except ValueError:
        problems.append("event_id must be a uuid")
    _check(problems, isinstance(data.get("run_id"), str) and bool(_RUN_ID.match(data["run_id"])),
           "run_id invalid")
    occurred = data.get("occurred_at")
    stamp = _iso(occurred) if isinstance(occurred, str) and _OCCURRED.match(occurred) else None
    if stamp is None:
        problems.append("occurred_at must be YYYY-MM-DDTHH:MM:SS(.sss)Z")
    elif stamp.replace(tzinfo=timezone.utc) > utcnow() + timedelta(seconds=300):
        problems.append("occurred_at is more than 5 minutes in the future")
    kind = data.get("event_type")
    payload = data.get("payload")
    if kind not in _PAYLOADS:
        problems.append("event_type has no local validator")
    elif not isinstance(payload, dict):
        problems.append("payload must be an object")
    else:
        _PAYLOADS[kind](payload, problems)
    if any(_looks_secret(s) for s in _strings(data)):
        problems.append("a string looks like a secret")
    if problems:
        raise EventInvalid("; ".join(problems))


# --------------------------------------------------------------------- build

def event_id_for(event_type: str, key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{event_type}:{key}"))


def research_run_id(now: datetime) -> str:
    return f"research-{now:%Y%m%d}"


def smoke_run_id(now: datetime) -> str:
    return f"e2e-test-{now:%Y-%m-%d}"


def slugify(text: str) -> str:
    out = re.sub(r"[^a-z0-9._-]+", "-", str(text).lower())
    return re.sub(r"^[^a-z0-9]+", "", out)[:64]


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(event_type, run_id, now, payload, key):
    return {
        "schema_version": 1, "paper_only": True, "event_id": event_id_for(event_type, key),
        "event_type": event_type, "run_id": run_id, "occurred_at": _stamp(now), "payload": payload,
    }


def build_heartbeat(now, run_id, active_strategies=()):
    payload = {
        "status": "idle", "engine_version": slugify(__version__),
        "active_strategies": sorted({slugify(s) for s in active_strategies}),
    }
    return _envelope("heartbeat", run_id, now, payload, f"{run_id}|{_stamp(now)}")


def _has_table(con, name) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0])


def approved_strategies(wh) -> list[str]:
    if not _has_table(wh.con, "active_strategy"):
        return []
    rows = wh.con.execute(
        "SELECT strategy_id FROM active_strategy "
        "QUALIFY row_number() OVER (PARTITION BY book_id ORDER BY seq DESC) = 1").fetchall()
    return sorted({slugify(r[0]) for r in rows})


def build_data_health(wh, now, run_id):
    con = wh.con
    if not _has_table(con, "bars"):
        return None, ["data_health skipped: no bars table"]
    last = con.execute("SELECT max(trade_date) FROM bars").fetchone()[0]
    if last is None:
        return None, ["data_health skipped: no bars in the warehouse"]
    rows = sum(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
               for t in ("bars", "series", "corporate_actions") if _has_table(con, t))
    version = f"tiingo-{last:%Y%m%d}"
    payload = {
        "dataset_version": version, "rows": rows,
        "last_observation_at": f"{last:%Y-%m-%d}T00:00:00Z",
        "stale": (now.astimezone(timezone.utc).date() - last).days > STALE_DAYS,
    }
    return _envelope("data_health", run_id, now, payload, version), []


def _rejection_reason(text) -> str:
    t = (text or "").lower()
    for needle, reason in (
        ("insufficient", "insufficient_trades"), ("few trades", "insufficient_trades"),
        ("overfit", "overfit"), ("drawdown", "drawdown_limit"),
        ("baseline", "underperforms_baseline"), ("underperform", "underperforms_baseline"),
        ("data quality", "data_quality"), ("lookahead", "lookahead_bias"),
        ("look-ahead", "lookahead_bias"), ("cost", "cost_sensitivity"),
        ("unstable", "unstable_parameters"), ("risk", "risk_rule_violation"),
        ("manual", "manual_review"),
    ):
        if needle in t:
            return reason
    return "underperforms_baseline"


_SERIES_DESC = re.compile(r"^(\S+) daily close from ")
_OHLCV_DESC = re.compile(r"^\d+ OHLCV bars for (\d+) symbols from (\S+?),")


def dataset_kind(description) -> str | None:
    """Stable id of the kind of dataset: a series id, or ohlcv<symbol count>-<source>."""
    text = description or ""
    m = _OHLCV_DESC.match(text)
    if m:
        return slugify(f"ohlcv{m.group(1)}-{m.group(2)}")
    m = _SERIES_DESC.match(text)
    if m:
        return slugify(m.group(1))
    return None


def _strategy_id(kind, name, parameters_json) -> str:
    try:
        params = json.loads(parameters_json) if parameters_json else {}
    except ValueError:
        params = {}
    parts = [str(params[k]) for k in sorted(params) if isinstance(params[k], (str, int, float))]
    full = slugify("-".join([name, *parts]))
    if len(kind) + 1 + len(full) <= 64:
        return f"{kind}.{full}"
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:8]
    room = 64 - len(kind) - 1 - 9
    return f"{kind}.{slugify(name)[:room]}-{digest}"


def build_backtest_events(wh, now, run_id, book="research"):
    if not _has_table(wh.con, "experiments"):
        return [], ["backtest skipped: no experiments table"]
    rows = wh.con.execute(
        "SELECT experiment_id, strategy_name, parameters, dataset_fingerprint, dataset_description, "
        "evaluated_window, git_commit, metrics, max_drawdown, trade_count, promotion_decision, "
        "rejection_reason, train_start, train_end, validation_start, validation_end, created_at "
        "FROM experiments WHERE evaluated_window IN ('train', 'validation') "
        "ORDER BY created_at, experiment_id").fetchall()
    events, warnings, usable = [], [], []
    for row in rows:
        kind = dataset_kind(row[4])
        if kind is None:
            warnings.append(f"backtest {row[1]}/{row[5]} skipped: unrecognised dataset description")
        else:
            usable.append((kind, row))
    newest = {}
    for kind, row in usable:
        key = (kind, row[5])
        if key not in newest or row[16] >= newest[key][1]:
            newest[key] = (row[3], row[16])
    # Within the newest fingerprint, a re-run under the same strategy_id (new version or costs)
    # supersedes the older one, so the dashboard never shows two rows with one name.
    latest = {}
    for kind, row in usable:
        if newest[(kind, row[5])][0] == row[3]:
            latest[(kind, row[5], _strategy_id(kind, row[1], row[2]))] = row
    for kind, row in sorted(((k[0], r) for k, r in latest.items()),
                            key=lambda kr: (kr[0], kr[1][5], kr[1][1], kr[1][0])):
        (experiment_id, name, params, fp, _desc, window, commit, metrics, drawdown, trades,
         decision, reason, ts, te, vs, ve, _created) = row
        label = f"{name}/{window}"
        start, end = (ts, te) if window == "train" else (vs, ve)
        parsed = json.loads(metrics) if metrics else {}
        total, sharpe = parsed.get("total_return"), parsed.get("sharpe")
        if not (isinstance(commit, str) and _SHA.match(commit)):
            warnings.append(f"backtest {label} skipped: no valid commit sha recorded")
            continue
        if total is None or drawdown is None or trades is None or start is None or end is None or start >= end:
            warnings.append(f"backtest {label} skipped: missing return, drawdown, trades or period")
            continue
        payload = {
            "strategy_id": _strategy_id(kind, name, params), "dataset_version": fp[:12], "commit_sha": commit,
            "return_pct": round(float(Decimal(str(total)) * 100), 4),
            "max_drawdown_pct": round(float(drawdown) * 100, 4), "trades": int(trades),
            "period_start": f"{start.isoformat()}T00:00:00Z", "period_end": f"{end.isoformat()}T00:00:00Z",
            "status": "rejected" if decision == "REJECTED" else "candidate",
        }
        if sharpe is not None:
            payload["sharpe"] = round(float(Decimal(str(sharpe))), 4)
        if name != "buy_and_hold":
            payload["baseline_strategy_id"] = f"{kind}.buy_and_hold"
        if decision == "REJECTED":
            payload["rejection_reason"] = _rejection_reason(reason)
        events.append(_envelope("backtest", run_id, now, payload, f"{experiment_id}|{book}"))
    return events, warnings


def build_paper_session_events(wh, now, run_id):
    con = wh.con
    if not _has_table(con, "paper_sessions"):
        return [], []
    if not con.execute("SELECT count(*) FROM paper_sessions").fetchone()[0]:
        return [], []
    return [], ["paper_session skipped: realized/unrealized pnl are not persisted in paper_sessions"]


def build_all(wh, now):
    run_id = research_run_id(now)
    events, warnings = [], []
    health, warns = build_data_health(wh, now, run_id)
    warnings += warns
    if health is not None:
        events.append(health)
    built, warns = build_backtest_events(wh, now, run_id)
    events += built
    warnings += warns
    built, warns = build_paper_session_events(wh, now, run_id)
    events += built
    warnings += warns
    events.append(build_heartbeat(now, run_id, approved_strategies(wh)))
    return events, warnings
