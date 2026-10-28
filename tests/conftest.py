"""Shared fixtures.

Fixtures here are deterministic by construction: no wall-clock, no RNG without
an explicit seed, no network. A test that needs the network carries the
`network` marker and is skipped unless SIGNALFORGE_NETWORK_TESTS=1.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.core.history import PriceHistory
from signalforge.core.types import Bar

D = Decimal
ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "archive")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("SIGNALFORGE_NETWORK_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set SIGNALFORGE_NETWORK_TESTS=1 to run network tests")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def make_bar(
    symbol: str,
    trade_date: date,
    close: Decimal,
    *,
    open_: Decimal | None = None,
    high: Decimal | None = None,
    low: Decimal | None = None,
    volume: int = 1_000_000,
    adj_close: Decimal | None = None,
    source: str = "fixture",
) -> Bar:
    """Build a self-consistent bar from a close, widening high/low as needed."""
    o = open_ if open_ is not None else close
    h = high if high is not None else max(o, close)
    low_candidate = low if low is not None else min(o, close)
    return Bar(
        symbol=symbol,
        trade_date=trade_date,
        open=o,
        high=max(h, o, close),
        low=min(low_candidate, o, close),
        close=close,
        volume=volume,
        adj_close=adj_close if adj_close is not None else close,
        source=source,
    )


def business_days(start: date, count: int) -> list[date]:
    """`count` weekdays starting at `start`. Ignores holidays deliberately.

    Holiday handling is the data layer's job; a fixture that invented a
    holiday calendar would be asserting its own guess.
    """
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


@pytest.fixture
def flat_history() -> PriceHistory:
    """One symbol, 60 sessions, constant price. No strategy should trade it."""
    days = business_days(date(2020, 1, 1), 60)
    return PriceHistory.from_bars([make_bar("FLAT", d, D("100")) for d in days])


@pytest.fixture
def uptrend_history() -> PriceHistory:
    """One symbol rising a fixed 1 unit per session for 300 sessions."""
    days = business_days(date(2020, 1, 1), 300)
    return PriceHistory.from_bars(
        [make_bar("UP", d, D(100 + i)) for i, d in enumerate(days)]
    )


@pytest.fixture
def downtrend_history() -> PriceHistory:
    days = business_days(date(2020, 1, 1), 300)
    return PriceHistory.from_bars(
        [make_bar("DOWN", d, D(400 - i)) for i, d in enumerate(days)]
    )


@pytest.fixture
def archive_dir() -> str:
    return ARCHIVE_DIR
