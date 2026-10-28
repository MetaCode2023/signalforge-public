"""Shared plumbing for strategies: point-in-time guard, warmup gate, Signal factory."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection

ZERO = Decimal("0")
WEIGHT_QUANT = Decimal("0.000001")


def normalise_symbols(symbols) -> tuple[str, ...]:
    if isinstance(symbols, str):
        raise ValidationError(f"symbols must be a collection of tickers, not a string: {symbols!r}")
    out = tuple(str(s).strip().upper() for s in symbols)
    if not out or not all(out):
        raise ValidationError(f"symbols must be a non-empty collection of tickers, got {symbols!r}")
    return out


def require_weight(name: str, value: Decimal) -> Decimal:
    value = Decimal(value)
    if not (ZERO < value <= Decimal("1")):
        raise ValidationError(f"{name} must be in (0, 1], got {value}")
    return value


def require_positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{name} must be a positive integer, got {value!r}")
    return value


class BaseStrategy:
    """Subclasses set `strategy_id` and `warmup_days`, and implement
    `parameters()` and `_signals(view)`. They only ever see the view.
    """

    strategy_id: str = ""
    warmup_days: int = 1

    def parameters(self) -> dict[str, object]:
        raise NotImplementedError

    def _signals(self, view: PointInTimeView) -> list[Signal]:
        raise NotImplementedError

    def generate_signals(self, as_of: date, view: PointInTimeView) -> list[Signal]:
        if as_of != view.as_of:
            raise ValidationError(
                f"as_of {as_of} does not match the view's as_of {view.as_of}; "
                "a strategy may only act on the date its data ends"
            )
        if len(view.trading_days) < self.warmup_days:
            return []
        return self._signals(view)

    def _signal(
        self,
        view: PointInTimeView,
        symbol: str,
        direction: SignalDirection,
        reason: str,
        weight: Decimal = ZERO,
        strength: Decimal = ZERO,
    ) -> Signal:
        return Signal(
            symbol=symbol,
            direction=direction,
            as_of=view.as_of,
            strategy_id=self.strategy_id,
            reason=reason,
            target_weight=ZERO if direction is SignalDirection.EXIT_LONG else weight,
            strength=strength,
        )
