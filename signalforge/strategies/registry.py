"""Strategy id -> class, with a factory for the experiment runner."""

from __future__ import annotations

from signalforge.core.errors import ValidationError
from signalforge.strategies.base import BaseStrategy
from signalforge.strategies.benchmark import BuyAndHold
from signalforge.strategies.mean_reversion import ZScoreMeanReversion
from signalforge.strategies.momentum import CrossSectionalMomentum
from signalforge.strategies.trend import MovingAverageTrend
from signalforge.strategies.volatility import VolatilityTargetedTrend

STRATEGIES: dict[str, type[BaseStrategy]] = {
    cls.strategy_id: cls
    for cls in (
        MovingAverageTrend,
        CrossSectionalMomentum,
        ZScoreMeanReversion,
        VolatilityTargetedTrend,
        BuyAndHold,
    )
}


def build(strategy_id: str, **params) -> BaseStrategy:
    cls = STRATEGIES.get(strategy_id)
    if cls is None:
        raise ValidationError(f"unknown strategy {strategy_id!r}; known: {sorted(STRATEGIES)}")
    try:
        return cls(**params)
    except TypeError as exc:
        raise ValidationError(f"bad parameters for {strategy_id}: {exc}") from exc
