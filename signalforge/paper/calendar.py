"""Freshness gate for a book trading real OHLCV.

The calendar is the benchmark's own bar dates, so no exchange calendar is
assumed. A symbol that is missing a day the benchmark traded, or that stops
short of the run date, would be marked stale or silently skipped: better to
refuse and say which.

Only RECENT_SESSIONS of history are gated hard, because the gate exists for
freshness. Older holes inside the trading window are returned as warnings so
they are reported, not hidden, without blocking a run on decades-old gaps.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping

MAX_DATES_SHOWN = 10
RECENT_SESSIONS = 30


def _dates(missing: list[date]) -> str:
    shown = ", ".join(d.isoformat() for d in missing[:MAX_DATES_SHOWN])
    more = f" (first {MAX_DATES_SHOWN} of {len(missing)})" if len(missing) > MAX_DATES_SHOWN else ""
    return f"{len(missing)} calendar date(s){more}: {shown}"


def check_freshness(
    calendar: Iterable[date],
    symbol_dates: Mapping[str, Iterable[date]],
    through: date,
    window_start: date | None = None,
) -> tuple[list[str], list[str]]:
    """(refusals, warnings) for a run through `through`.

    A symbol is expected on every calendar date from its own first bar (or
    `window_start`, if later) through `through`. It is refused if its last bar
    is before the last calendar session on or before `through` (so a holiday
    `through` is not a false alarm), or if it misses any of the last
    RECENT_SESSIONS sessions. Older missing sessions are warnings.
    """
    days = sorted(set(calendar))
    if not days:
        return ["the benchmark calendar has no bars, so freshness cannot be checked"], []
    if through > days[-1]:
        return [
            f"through {through} is after the calendar's last date {days[-1]}; "
            "the benchmark has no bars that late"
        ], []
    sessions = [d for d in days if d <= through]
    if not sessions:
        return [f"through {through} is before the calendar's first date {days[0]}"], []
    target = sessions[-1]
    recent_from = sessions[-RECENT_SESSIONS:][0]
    refusals: list[str] = []
    warnings: list[str] = []
    for symbol in sorted(symbol_dates):
        have = set(symbol_dates[symbol])
        if not have:
            refusals.append(f"{symbol}: has no bars")
            continue
        if max(have) < target:
            refusals.append(
                f"{symbol}: last bar {max(have)} is before the last calendar session "
                f"{target} on or before through {through}"
            )
        floor = min(have) if window_start is None else max(min(have), window_start)
        missing = [d for d in sessions if d >= floor and d not in have]
        recent = [d for d in missing if d >= recent_from]
        older = [d for d in missing if d < recent_from]
        if recent:
            refusals.append(
                f"{symbol}: missing {_dates(recent)} within the last {RECENT_SESSIONS} sessions"
            )
        if older:
            warnings.append(f"{symbol}: older gaps, missing {_dates(older)}")
    return refusals, warnings
