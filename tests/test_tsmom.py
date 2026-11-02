"""Time-series momentum: own-trend rule, decided monthly, point in time."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import SignalDirection
from signalforge.strategies.registry import build
from signalforge.strategies.tsmom import TimeSeriesMomentum, decision_reference
from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 160)  # Jan to early Aug 2020
LOOKBACK = 40


def series(symbol, prices, days=DAYS):
    return [make_bar(symbol, d, D(p), adj_close=D(p)) for d, p in zip(days, prices)]


def strategy(**over):
    params = {"symbols": ("UP", "DOWN"), "lookback": LOOKBACK, "target_weight": D("0.10"), **over}
    return TimeSeriesMomentum(**params)


def signals_on(history, day, strat=None):
    strat = strat or strategy()
    return {s.symbol: s for s in strat.generate_signals(day, history.view(day))}


def first_session_of(month, days=DAYS):
    return next(d for d in days if d.month == month)


def test_decision_reference_is_the_session_before_the_month_began():
    days = tuple(DAYS[:30])  # Jan 1 .. Feb 11
    feb_first = first_session_of(2)
    assert decision_reference(days) == days[days.index(feb_first) - 1]
    assert decision_reference(tuple(DAYS[:5])) is None


def test_holds_its_own_uptrend_and_exits_its_own_downtrend():
    up = series("UP", [100 + i for i in range(len(DAYS))])
    down = series("DOWN", [300 - i for i in range(len(DAYS))])
    history = PriceHistory.from_bars(up + down)
    day = first_session_of(5)
    got = signals_on(history, day)
    assert got["UP"].direction is SignalDirection.ENTER_LONG
    assert got["UP"].target_weight == D("0.10")
    assert got["DOWN"].direction is SignalDirection.EXIT_LONG
    assert got["DOWN"].target_weight == 0


def test_the_month_decision_holds_until_the_next_month():
    # Rises through April, then collapses from the 3rd session of May.
    may = first_session_of(5)
    crash_from = DAYS.index(may) + 2
    prices = [100 + i if i < crash_from else 10 for i in range(len(DAYS))]
    history = PriceHistory.from_bars(series("UP", prices) + series("DOWN", prices))
    mid_may = DAYS[crash_from + 5]
    assert mid_may.month == 5
    assert signals_on(history, mid_may)["UP"].direction is SignalDirection.ENTER_LONG
    assert signals_on(history, first_session_of(6))["UP"].direction is SignalDirection.EXIT_LONG


def test_no_signal_without_a_full_lookback_before_the_reference():
    late_days = DAYS[30:]
    young = series("UP", [100 + i for i in range(len(late_days))], days=late_days)
    old = series("DOWN", [100 + i for i in range(len(DAYS))])
    history = PriceHistory.from_bars(young + old)
    # By the first session of March UP has ~13 sessions; DOWN has ~43 (> lookback).
    got = signals_on(history, first_session_of(3))
    assert "UP" not in got
    assert got["DOWN"].direction is SignalDirection.ENTER_LONG


def test_later_bars_cannot_change_an_earlier_decision():
    base = [100 + i for i in range(len(DAYS))]
    day = first_session_of(4)
    cut = DAYS.index(day) + 1
    calm = PriceHistory.from_bars(series("UP", base) + series("DOWN", base))
    crashed = PriceHistory.from_bars(
        series("UP", base[:cut] + [1] * (len(DAYS) - cut))
        + series("DOWN", base[:cut] + [1] * (len(DAYS) - cut))
    )
    assert signals_on(calm, day) == signals_on(crashed, day)


def test_built_by_the_registry_and_validated():
    built = build("tsmom", symbols=("SPY",), lookback=252, target_weight=D("0.1"))
    assert built.parameters() == {"symbols": ("SPY",), "lookback": 252, "target_weight": D("0.1")}
    assert built.warmup_days == 253
    with pytest.raises(ValidationError):
        build("tsmom", symbols=("SPY",), lookback=0, target_weight=D("0.1"))
    with pytest.raises(ValidationError):
        build("tsmom", symbols=("SPY",), lookback=252, target_weight=D("1.5"))
