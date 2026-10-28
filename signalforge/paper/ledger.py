"""Append-only, tamper-evident trade event ledger.

"Immutable history" is easy to claim and easy to quietly break, so this does
two things instead of claiming it. Events are only ever appended and are
handed out as a tuple of frozen records, and each event hashes its own
contents together with the previous event's hash. Editing any earlier event
breaks the chain, and `verify()` says so.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

GENESIS_HASH = "0" * 64


class EventType(str, Enum):
    SESSION_OPENED = "SESSION_OPENED"
    SESSION_CLOSED = "SESSION_CLOSED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_REDUCED = "ORDER_REDUCED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    FILL = "FILL"
    PAUSED = "PAUSED"
    RESUMED = "RESUMED"
    BREAKER_TRIPPED = "BREAKER_TRIPPED"
    STRATEGY_OVERRIDE = "STRATEGY_OVERRIDE"
    """An UNAPPROVED strategy was run on an explicit operator override."""
    CORPORATE_ACTION = "CORPORATE_ACTION"
    """A split, adjustment or dividend was booked against the book at the open."""


def _canonical(value: Any) -> Any:
    """Make a payload JSON-stable so hashing is deterministic."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class LedgerHead:
    """A witness to the ledger's state: how long it was, and its last hash.

    A truncated hash chain is internally consistent, so no amount of
    self-inspection can detect that the tail was dropped. Recording a head
    somewhere else and checking against it later is the only way, which is
    what `verify_against` is for.
    """

    length: int
    event_hash: str


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    sequence: int
    event_type: EventType
    event_date: date
    payload: Mapping[str, Any]
    previous_hash: str
    event_hash: str

    def __post_init__(self) -> None:
        # Copy the payload so a caller mutating their dict afterwards cannot
        # change what the ledger recorded.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def compute_hash(self) -> str:
        body = json.dumps(
            {
                "sequence": self.sequence,
                "event_type": self.event_type.value,
                "event_date": self.event_date.isoformat(),
                "payload": _canonical(dict(self.payload)),
                "previous_hash": self.previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class TradeLedger:
    """The event history. Append-only by construction."""

    __slots__ = ("_events", "_appended")

    def __init__(self) -> None:
        self._events: list[LedgerEvent] = []
        # Monotonic count of everything ever appended. It only goes up, so a
        # shorter _events list means something was removed from this live
        # object, which a hash chain alone cannot reveal.
        self._appended = 0

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        """A tuple, so a caller cannot append, clear or reassign entries."""
        return tuple(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def append(
        self, event_type: EventType, event_date: date, payload: Mapping[str, Any]
    ) -> LedgerEvent:
        previous_hash = self._events[-1].event_hash if self._events else GENESIS_HASH
        draft = LedgerEvent(
            sequence=len(self._events) + 1,
            event_type=event_type,
            event_date=event_date,
            payload=payload,
            previous_hash=previous_hash,
            event_hash="",
        )
        event = LedgerEvent(
            sequence=draft.sequence,
            event_type=draft.event_type,
            event_date=draft.event_date,
            payload=draft.payload,
            previous_hash=previous_hash,
            event_hash=draft.compute_hash(),
        )
        self._events.append(event)
        self._appended += 1
        return event

    def head(self) -> LedgerHead:
        """A witness another process can store and check against later."""
        return LedgerHead(
            length=len(self._events),
            event_hash=self._events[-1].event_hash if self._events else GENESIS_HASH,
        )

    def verify_against(self, witness: LedgerHead) -> bool:
        """True if the chain is intact AND still matches a recorded head.

        This is what catches truncation across a restart, where the append
        counter is gone and a shortened chain looks perfectly valid.
        """
        if not self.verify():
            return False
        current = self.head()
        return current.length >= witness.length and (
            witness.length == 0
            or self._events[witness.length - 1].event_hash == witness.event_hash
        )

    def verify(self) -> bool:
        """True if the chain is intact and nothing was removed.

        Checks three things: every event's hash matches its contents, each
        links to its predecessor, and the list is as long as the number of
        appends. The last check is what detects tail truncation, which the
        chain itself cannot: dropping the final events leaves a structurally
        valid chain.
        """
        if len(self._events) != self._appended:
            return False
        previous_hash = GENESIS_HASH
        for index, event in enumerate(self._events, start=1):
            if event.sequence != index:
                return False
            if event.previous_hash != previous_hash:
                return False
            if event.compute_hash() != event.event_hash:
                return False
            previous_hash = event.event_hash
        return True

    def of_type(self, event_type: EventType) -> tuple[LedgerEvent, ...]:
        return tuple(e for e in self._events if e.event_type is event_type)

    def for_date(self, day: date) -> tuple[LedgerEvent, ...]:
        return tuple(e for e in self._events if e.event_date == day)
