"""Collection scheduling and health, independent of the paper execution path.

The default is one cycle. Nothing starts at import time. An OS-held file lock
protects a canonical warehouse path and is released even if a process crashes.
This module uses only the standard library so its control loop can be tested
without a network, credentials, or a database installation.
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


class CollectorBusy(RuntimeError):
    pass


class CollectionFailure(RuntimeError):
    """Safe public error: never include provider bodies, URLs, or credentials."""

    def __init__(self, *, retryable: bool):
        super().__init__("provider temporarily unavailable" if retryable else "collection refused")
        self.retryable = retryable


@dataclass(frozen=True)
class CollectorConfig:
    provider: str = "fred"
    series_ids: tuple[str, ...] = ("NASDAQCOM", "VIXCLS")
    symbols: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    interval_seconds: float = 86400
    daily_at_utc: str | None = "23:30"
    stale_after_days: int = 7
    stale_after_days_by_item: dict[str, int] = field(default_factory=dict)
    max_attempts: int = 3
    retry_seconds: float = 30
    max_retry_seconds: float = 300
    requests_per_day: int = 24
    lookback_days: int = 14
    # Fetch only the `batch_size` stalest symbols per cycle, so a universe
    # larger than the provider's hourly limit is covered by rotation rather
    # than by one oversized pull. `priority_symbols` go first among equally
    # stale symbols (the research book refreshes before the long tail).
    batch_size: int | None = None
    priority_symbols: tuple[str, ...] = ()

    def __post_init__(self):
        if self.provider not in {"fred", "local_archive", "tiingo", "alphavantage"}:
            raise ValueError("unsupported collector provider")
        for name in ("series_ids", "symbols", "datasets", "priority_symbols"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ) or len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique nonempty strings")
            object.__setattr__(self, name, tuple(values))
        if set(self.series_ids) & set(self.symbols):
            raise ValueError("series_ids and symbols must be distinct")
        if not set(self.priority_symbols) <= set(self.symbols):
            raise ValueError("priority_symbols must all be listed in symbols")
        if self.batch_size is not None:
            if type(self.batch_size) is not int or self.batch_size < 1:
                raise ValueError("batch_size must be a positive integer or null")
            if not self.symbols:
                raise ValueError("batch_size rotates symbols; it requires symbols")
        for name in ("interval_seconds", "retry_seconds", "max_retry_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("stale_after_days", "max_attempts", "requests_per_day", "lookback_days"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_attempts > 10 or self.max_retry_seconds < self.retry_seconds:
            raise ValueError("invalid retry bounds")
        if not isinstance(self.stale_after_days_by_item, dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 1
            for k, v in self.stale_after_days_by_item.items()
        ):
            raise ValueError("item freshness limits must be positive integer days")
        if self.daily_at_utc is not None:
            try:
                parsed = datetime.strptime(self.daily_at_utc, "%H:%M")
                if parsed.strftime("%H:%M") != self.daily_at_utc:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("daily_at_utc must be HH:MM or null") from None
            if self.interval_seconds != 86400:
                raise ValueError("daily_at_utc requires a daily interval; use null for interval scheduling")
        if self.provider == "fred" and (not self.series_ids or self.symbols or self.datasets):
            raise ValueError("FRED requires series_ids only")
        if self.provider in {"tiingo", "alphavantage"} and (not self.symbols or self.series_ids or self.datasets):
            raise ValueError("equity providers require symbols only")
        if self.provider == "local_archive" and not (self.series_ids or self.symbols):
            raise ValueError("archive collection requires explicit symbols or series_ids")
        minimum = 60 if self.provider == "local_archive" else 3600
        if self.interval_seconds < minimum:
            raise ValueError(f"collection interval must be at least {minimum} seconds")
        if self.provider == "alphavantage" and self.interval_seconds < 86400:
            raise ValueError("Alpha Vantage collection must be daily or slower")
        if self.provider == "alphavantage" and self.requests_per_day > 25:
            raise ValueError("Alpha Vantage local request budget must not exceed 25/day")
        if self.request_cost > self.requests_per_day:
            raise ValueError("one fetch exceeds the configured daily request budget")

    @property
    def items(self) -> tuple[str, ...]:
        return self.series_ids + self.symbols

    @property
    def request_cost(self) -> int:
        if self.provider == "local_archive":
            return 0
        if not self.symbols:
            return 1
        return min(self.batch_size or len(self.symbols), len(self.symbols))


def select_batch(config: CollectorConfig, latest: dict[str, date | None]) -> tuple[str, ...]:
    """The symbols one cycle fetches: all of them, or the stalest `batch_size`.

    Never-fetched symbols come first, then the oldest latest bar. Among equally
    stale symbols the priority ones go first, then configuration order, so the
    choice is deterministic and a full rotation reaches every symbol.
    """
    if config.batch_size is None or config.batch_size >= len(config.symbols):
        return config.symbols
    priority = set(config.priority_symbols)
    order = {symbol: i for i, symbol in enumerate(config.symbols)}

    def key(symbol):
        day = latest.get(symbol)
        return (day is not None, day or date.min, symbol not in priority, order[symbol])

    return tuple(sorted(config.symbols, key=key)[: config.batch_size])


@dataclass(frozen=True)
class Snapshot:
    latest: dict[str, date | None]
    rows_accepted: int = 0
    rows_quarantined: int = 0
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0


class WarehouseLock:
    def __init__(self, warehouse: Path):
        self.path = Path(str(warehouse.resolve()) + ".collector.lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"\0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise CollectorBusy("another collector holds this warehouse lock") from None
        return self

    def __exit__(self, *_):
        if self.file is not None:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def next_run(config: CollectorConfig, now: datetime) -> datetime:
    if config.daily_at_utc is None:
        return now + timedelta(seconds=config.interval_seconds)
    hour, minute = map(int, config.daily_at_utc.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class Collector:
    def __init__(self, config: CollectorConfig, warehouse: Path, task: Callable[[], Snapshot],
                 *, clock=utcnow, wait=None, jitter=None, emit=None):
        self.config = config
        self.warehouse = Path(warehouse).resolve()
        self.task = task
        self.clock = clock
        self.stop = threading.Event()
        self.wait = wait or self.stop.wait
        self.jitter = jitter or (lambda: random.uniform(1.0, 1.2))
        self.emit = emit or (lambda event: None)
        self.run_id = uuid.uuid4().hex
        self.status_path = Path(str(self.warehouse) + ".collector.json")
        self.events_path = Path(str(self.warehouse) + ".collector.jsonl")
        self.budget_path = Path(str(self.warehouse) + ".collector-budget.json")

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("collector clock must be timezone aware")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _atomic_json(path, payload):
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _reserve_requests(self):
        cost = self.config.request_cost
        if cost == 0:
            return True
        # Reserve before fetching, including failed and partly completed pulls.
        # Malformed persisted budgets fail closed rather than resetting a quota.
        budgets = json.loads(self.budget_path.read_text(encoding="utf-8")) if self.budget_path.exists() else {}
        today = self._now().date().isoformat()
        state = budgets.get(self.config.provider, {"day": today, "used": 0})
        if not isinstance(state, dict) or type(state.get("used")) is not int or state["used"] < 0:
            raise ValueError("invalid persisted request budget")
        try:
            budget_day = date.fromisoformat(state["day"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid persisted request budget date") from None
        if budget_day > self._now().date():
            raise ValueError("request budget date is in the future")
        used = state["used"] if state["day"] == today else 0
        if used + cost > self.config.requests_per_day:
            return False
        budgets[self.config.provider] = {"day": today, "used": used + cost}
        self._atomic_json(self.budget_path, budgets)
        return True

    def _record(self, cycle, attempt, status, **extra):
        event = {"run_id": self.run_id, "cycle": cycle, "attempt": attempt,
                 "provider": self.config.provider, "checked_at": self._now().isoformat(),
                 "status": status, "paper_only": True, **extra}
        encoded = json.dumps(event, sort_keys=True)
        with self.events_path.open("a", encoding="utf-8") as file:
            file.write(encoded + "\n")
            file.flush()
        self._atomic_json(self.status_path, event)
        self.emit(event)
        return event

    def cycle(self, number):
        for attempt in range(1, self.config.max_attempts + 1):
            if self.stop.is_set():
                return self._record(number, attempt, "stopped")
            try:
                reserved = self._reserve_requests()
            except Exception:
                self._record(number, attempt, "failed", reason="request_budget_unavailable")
                raise
            if not reserved:
                return self._record(number, attempt, "budget_exhausted")
            try:
                snapshot = self.task()
            except CollectionFailure as exc:
                retry = exc.retryable and attempt < self.config.max_attempts
                event = self._record(number, attempt, "retrying" if retry else "failed")
                if not retry:
                    return event
                delay = min(self.config.max_retry_seconds,
                            self.config.retry_seconds * (2 ** (attempt - 1)) * self.jitter())
                if self.wait(delay):
                    return self._record(number, attempt, "stopped")
                continue
            except Exception:
                # No arbitrary exception text is persisted: it can contain credentials.
                self._record(number, attempt, "failed")
                raise
            today = self._now().date()
            stale = {}
            for item in self.config.items:
                latest = snapshot.latest.get(item)
                age = (today - latest).days if latest is not None else None
                threshold = self.config.stale_after_days_by_item.get(item, self.config.stale_after_days)
                if latest is None or age < 0 or age > threshold:
                    stale[item] = {"latest": latest.isoformat() if latest else None,
                                   "age_days": age, "limit_days": threshold}
            return self._record(number, attempt, "stale" if stale else "ok",
                                latest={k: v.isoformat() if v else None for k, v in snapshot.latest.items()},
                                stale=stale, rows_accepted=snapshot.rows_accepted,
                                rows_quarantined=snapshot.rows_quarantined,
                                inserted=snapshot.inserted, revised=snapshot.revised,
                                unchanged=snapshot.unchanged)

    def run(self, *, max_cycles: int | None = 1):
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise ValueError("max_cycles must be positive or None")
        last = None
        with WarehouseLock(self.warehouse):
            cycle = 0
            while not self.stop.is_set() and (max_cycles is None or cycle < max_cycles):
                cycle += 1
                last = self.cycle(cycle)
                if max_cycles is not None and cycle >= max_cycles:
                    break
                due = next_run(self.config, self._now())
                self._record(cycle, 0, "waiting", next_run_at=due.isoformat(),
                             last_status=last["status"])
                if self.wait(max(0, (due - self._now()).total_seconds())):
                    break
        return last
