"""Fast/slow simple moving-average crossover on adjusted closes."""

from __future__ import annotations

from decimal import Decimal

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.strategies.base import (
    BaseStrategy,
    normalise_symbols,
    require_positive_int,
    require_weight,
)


def sma_pair(view: PointInTimeView, symbol: str, fast: int, slow: int) -> tuple[Decimal, Decimal] | None:
    """(fast SMA, slow SMA) or None if the symbol lacks `slow` bars."""
    if not view.has_history(symbol, slow):
        return None
    closes = view.adjusted_closes(symbol, slow)
    return sum(closes[-fast:]) / fast, sum(closes) / slow


class MovingAverageTrend(BaseStrategy):
    strategy_id = "ma_trend"

    def __init__(
        self,
        fast_window: int,
        slow_window: int,
        target_weight: Decimal,
        symbols,
        max_names: int | None = None,
    ) -> None:
        self.fast_window = require_positive_int("fast_window", fast_window)
        self.slow_window = require_positive_int("slow_window", slow_window)
        if self.fast_window >= self.slow_window:
            raise ValidationError(
                f"fast_window ({fast_window}) must be below slow_window ({slow_window})"
            )
        self.target_weight = require_weight("target_weight", target_weight)
        self.symbols = normalise_symbols(symbols)
        self.max_names = None if max_names is None else require_positive_int("max_names", max_names)
        self.warmup_days = self.slow_window

    def parameters(self) -> dict[str, object]:
        params: dict[str, object] = {
            "fast_window": self.fast_window,
            "slow_window": self.slow_window,
            "target_weight": self.target_weight,
            "symbols": self.symbols,
        }
        if self.max_names is not None:
            params["max_names"] = self.max_names
        return params

    def _signals(self, view: PointInTimeView) -> list[Signal]:
        out: list[Signal] = []
        trending: list[tuple[Decimal, str, str]] = []
        for symbol in sorted(self.symbols):
            pair = sma_pair(view, symbol, self.fast_window, self.slow_window)
            if pair is None:
                continue
            fast, slow = pair
            detail = f"SMA{self.fast_window}={fast:.4f} vs SMA{self.slow_window}={slow:.4f}"
            if fast > slow and self.max_names is not None:
                trending.append((fast / slow, symbol, detail))
            elif fast > slow:
                out.append(self._signal(view, symbol, SignalDirection.ENTER_LONG,
                                        f"fast above slow: {detail}", self.target_weight))
            else:
                out.append(self._signal(view, symbol, SignalDirection.EXIT_LONG,
                                        f"fast at or below slow: {detail}"))
        trending.sort(key=lambda t: (-t[0], t[1]))
        for rank, (ratio, symbol, detail) in enumerate(trending, start=1):
            if rank <= self.max_names:
                out.append(self._signal(view, symbol, SignalDirection.ENTER_LONG,
                                        f"fast above slow, rank {rank}/{len(trending)}: {detail}",
                                        self.target_weight))
            else:
                out.append(self._signal(view, symbol, SignalDirection.EXIT_LONG,
                                        f"outside top {self.max_names}: {detail}"))
        return sorted(out, key=lambda s: s.symbol)
