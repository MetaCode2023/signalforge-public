"""Point-in-time eligibility: a strategy only sees names it could have screened that day.

A symbol is eligible on a day if it is in the committed universe, has enough
visible history, has printed a bar within the risk engine's staleness limit
(a frozen name would otherwise hold a slot the risk engine refuses to fill),
and its median traded dollar volume over the trailing window
clears a floor. Every input comes from the view, so the screen cannot look
ahead. Adjusted close times adjusted volume is the raw dollar volume, so the
screen does not move when a later split or dividend rewrites old prices.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from statistics import median

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.strategies.base import ZERO, normalise_symbols, require_positive_int


class EligibleView:
    """A view that shows only eligible symbols to has_history and cross_section.

    Everything else delegates to the wrapped view, so a future bar still raises.
    """

    def __init__(self, view: PointInTimeView, eligible: frozenset[str]) -> None:
        self._view = view
        self._eligible = eligible

    def has_history(self, symbol: str, required: int) -> bool:
        return symbol in self._eligible and self._view.has_history(symbol, required)

    def cross_section(self) -> tuple[str, ...]:
        return tuple(s for s in self._view.cross_section() if s in self._eligible)

    def __getattr__(self, name: str):
        return getattr(self._view, name)


class EligibilityFilter:
    def __init__(
        self,
        inner,
        symbols,
        min_history_sessions: int,
        min_median_dollar_volume: Decimal,
        dollar_volume_window: int,
        max_stale_days: int,
    ) -> None:
        self.inner = inner
        self.candidates = frozenset(normalise_symbols(symbols))
        self.min_history_sessions = require_positive_int("min_history_sessions", min_history_sessions)
        self.dollar_volume_window = require_positive_int("dollar_volume_window", dollar_volume_window)
        self.max_stale_days = require_positive_int("max_stale_days", max_stale_days)
        floor = Decimal(min_median_dollar_volume)
        if floor < ZERO:
            raise ValidationError(f"min_median_dollar_volume must not be negative, got {floor}")
        self.min_median_dollar_volume = floor
        self.strategy_id = inner.strategy_id
        self.warmup_days = inner.warmup_days

    def parameters(self) -> dict[str, object]:
        return {
            **self.inner.parameters(),
            "elig_min_history_sessions": self.min_history_sessions,
            "elig_min_median_dollar_volume": self.min_median_dollar_volume,
            "elig_dollar_volume_window": self.dollar_volume_window,
            "elig_max_stale_days": self.max_stale_days,
        }

    def _is_eligible(self, view: PointInTimeView, symbol: str) -> bool:
        if not view.has_history(symbol, self.min_history_sessions):
            return False
        if view.staleness_days(symbol) > self.max_stale_days:
            return False
        recent = view.bars(symbol, self.dollar_volume_window)
        if not all(b.volume_known for b in recent):
            return False
        return median(b.close * b.volume for b in recent) >= self.min_median_dollar_volume

    def eligible(self, view: PointInTimeView) -> frozenset[str]:
        return frozenset(s for s in self.candidates if self._is_eligible(view, s))

    def generate_signals(self, as_of: date, view: PointInTimeView) -> list[Signal]:
        eligible = self.eligible(view)
        signals = list(self.inner.generate_signals(as_of, EligibleView(view, eligible)))
        for symbol in sorted(self.candidates - eligible):
            if view.latest(symbol) is None:
                continue
            signals.append(
                Signal(
                    symbol=symbol,
                    direction=SignalDirection.EXIT_LONG,
                    as_of=as_of,
                    strategy_id=self.strategy_id,
                    reason=(
                        "ineligible: history, staleness, volume availability "
                        "or median dollar volume below the screen"
                    ),
                )
            )
        return sorted(signals, key=lambda s: s.symbol)
