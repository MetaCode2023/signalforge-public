"""Risk-off overlay: flatten the book while a published macro series says so.

A gate reads an observation that was published strictly before the session
being decided: the reference session is `lag_sessions` trading days back, and
the value used is the last non-null observation dated at or before it. A
series with nothing recent enough is "unknown", which is treated as risk-on
and counted, so a dead feed cannot quietly switch the overlay off unnoticed.
Observations are the latest published values, not ALFRED vintages.
"""

from __future__ import annotations

import bisect
from dataclasses import replace
from datetime import date
from decimal import Decimal

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.strategies.base import ZERO, require_positive_int

STALE_AFTER_DAYS = 10
RULES = (">", "<")
STATES = ("risk_off", "risk_on", "unknown")


class RiskOffGate:
    def __init__(
        self,
        name: str,
        series_id: str,
        rule: str,
        threshold: Decimal,
        lag_sessions: int,
        observations,
    ) -> None:
        if rule not in RULES:
            raise ValidationError(f"rule must be one of {list(RULES)}, got {rule!r}")
        self.name = name
        self.series_id = series_id
        self.rule = rule
        self.threshold = Decimal(threshold)
        self.lag_sessions = require_positive_int("lag_sessions", lag_sessions)
        ordered = sorted(observations, key=lambda o: o[0])
        for (a, _), (b, _) in zip(ordered, ordered[1:]):
            if a == b:
                raise ValidationError(
                    f"duplicate observation date {a} in {series_id}; refusing to pick one"
                )
        usable = [(d, v) for d, v in ordered if v is not None]
        self._dates = [d for d, _ in usable]
        self._values = [v for _, v in usable]

    def _state_at(self, days: tuple[date, ...], index: int) -> str:
        if index - self.lag_sessions < 0:
            return "unknown"
        reference = days[index - self.lag_sessions]
        cut = bisect.bisect_right(self._dates, reference)
        if cut == 0 or (reference - self._dates[cut - 1]).days > STALE_AFTER_DAYS:
            return "unknown"
        value = self._values[cut - 1]
        off = value > self.threshold if self.rule == ">" else value < self.threshold
        return "risk_off" if off else "risk_on"

    def state(self, view: PointInTimeView) -> str:
        days = view.trading_days
        return self._state_at(days, len(days) - 1)

    def count_states(self, trading_days, start: date, end: date) -> dict[str, int]:
        days = tuple(trading_days)
        counts = dict.fromkeys(STATES, 0)
        for index, day in enumerate(days):
            if start <= day <= end:
                counts[self._state_at(days, index)] += 1
        return counts


class RegimeOverlay:
    def __init__(self, inner, gate: RiskOffGate) -> None:
        self.inner = inner
        self.gate = gate
        self.strategy_id = f"{inner.strategy_id}_riskoff_{gate.name}"
        self.warmup_days = inner.warmup_days

    def parameters(self) -> dict[str, object]:
        return {
            **self.inner.parameters(),
            "riskoff_series": self.gate.series_id,
            "riskoff_rule": self.gate.rule,
            "riskoff_threshold": self.gate.threshold,
            "riskoff_lag_sessions": self.gate.lag_sessions,
        }

    def generate_signals(self, as_of: date, view: PointInTimeView) -> list[Signal]:
        signals = self.inner.generate_signals(as_of, view)
        if self.gate.state(view) != "risk_off":
            return signals
        return [
            replace(
                s,
                direction=SignalDirection.EXIT_LONG,
                target_weight=ZERO,
                reason=f"risk-off {self.gate.name}: {s.reason}",
            )
            if s.direction is SignalDirection.ENTER_LONG
            else s
            for s in signals
        ]
