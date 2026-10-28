"""Trend following with position size scaled to a target annualised volatility."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.strategies.base import (
    WEIGHT_QUANT,
    BaseStrategy,
    normalise_symbols,
    require_positive_int,
    require_weight,
)
from signalforge.strategies.trend import sma_pair


def realised_daily_vol(returns: tuple[Decimal, ...]) -> Decimal:
    """Sample standard deviation of simple returns, in Decimal."""
    n = len(returns)
    mean = sum(returns) / n
    return (sum((r - mean) ** 2 for r in returns) / (n - 1)).sqrt()


class VolatilityTargetedTrend(BaseStrategy):
    strategy_id = "vol_targeted_trend"

    def __init__(
        self,
        fast_window: int,
        slow_window: int,
        target_volatility: Decimal,
        vol_window: int,
        max_weight: Decimal,
        annualisation_factor: int,
        symbols,
    ) -> None:
        self.fast_window = require_positive_int("fast_window", fast_window)
        self.slow_window = require_positive_int("slow_window", slow_window)
        if self.fast_window >= self.slow_window:
            raise ValidationError(
                f"fast_window ({fast_window}) must be below slow_window ({slow_window})"
            )
        self.target_volatility = Decimal(target_volatility)
        if self.target_volatility <= 0:
            raise ValidationError(f"target_volatility must be positive, got {target_volatility}")
        self.vol_window = require_positive_int("vol_window", vol_window)
        if self.vol_window < 2:
            raise ValidationError("vol_window must be at least 2 to estimate a deviation")
        self.max_weight = require_weight("max_weight", max_weight)
        self.annualisation_factor = require_positive_int("annualisation_factor", annualisation_factor)
        self.symbols = normalise_symbols(symbols)
        self.warmup_days = max(self.slow_window, self.vol_window + 1)

    def parameters(self) -> dict[str, object]:
        return {
            "fast_window": self.fast_window,
            "slow_window": self.slow_window,
            "target_volatility": self.target_volatility,
            "vol_window": self.vol_window,
            "max_weight": self.max_weight,
            "annualisation_factor": self.annualisation_factor,
            "symbols": self.symbols,
        }

    def _weight(self, daily_vol: Decimal) -> Decimal:
        if daily_vol == 0:
            return self.max_weight
        annual = daily_vol * Decimal(self.annualisation_factor).sqrt()
        return min(self.max_weight, (self.target_volatility / annual).quantize(WEIGHT_QUANT, ROUND_DOWN))

    def _signals(self, view: PointInTimeView) -> list[Signal]:
        out: list[Signal] = []
        for symbol in sorted(self.symbols):
            if not view.has_history(symbol, self.warmup_days):
                continue
            fast, slow = sma_pair(view, symbol, self.fast_window, self.slow_window)
            detail = f"SMA{self.fast_window}={fast:.4f} vs SMA{self.slow_window}={slow:.4f}"
            if fast <= slow:
                out.append(self._signal(view, symbol, SignalDirection.EXIT_LONG,
                                        f"fast at or below slow: {detail}"))
                continue
            vol = realised_daily_vol(view.returns(symbol, self.vol_window))
            weight = self._weight(vol)
            out.append(self._signal(
                view, symbol, SignalDirection.ENTER_LONG,
                f"fast above slow: {detail}; daily vol {vol:.6f} -> weight {weight} "
                f"(target vol {self.target_volatility}, cap {self.max_weight})",
                weight,
            ))
        return out
