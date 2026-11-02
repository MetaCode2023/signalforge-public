"""Strategy id -> class, with a factory for the experiment runner."""

from __future__ import annotations

from signalforge.core.errors import ValidationError
from signalforge.strategies.base import BaseStrategy
from signalforge.strategies.benchmark import BuyAndHold
from signalforge.strategies.eligibility import EligibilityFilter
from signalforge.strategies.mean_reversion import ZScoreMeanReversion
from signalforge.strategies.momentum import CrossSectionalMomentum
from signalforge.strategies.regime import RegimeOverlay
from signalforge.strategies.trend import MovingAverageTrend
from signalforge.strategies.tsmom import TimeSeriesMomentum
from signalforge.strategies.volatility import VolatilityTargetedTrend

STRATEGIES: dict[str, type[BaseStrategy]] = {
    cls.strategy_id: cls
    for cls in (
        MovingAverageTrend,
        CrossSectionalMomentum,
        ZScoreMeanReversion,
        VolatilityTargetedTrend,
        TimeSeriesMomentum,
        BuyAndHold,
    )
}


def build(strategy_id: str, **params):
    cls = STRATEGIES.get(strategy_id)
    if cls is None:
        raise ValidationError(f"unknown strategy {strategy_id!r}; known: {sorted(STRATEGIES)}")
    eligibility = params.pop("eligibility", None)
    gate = params.pop("riskoff_gate", None)
    try:
        built = cls(**params)
    except TypeError as exc:
        raise ValidationError(f"bad parameters for {strategy_id}: {exc}") from exc
    if eligibility is not None:
        if params.get("symbols") is None:
            raise ValidationError(f"eligibility for {strategy_id} needs an explicit symbols list")
        built = EligibilityFilter(built, params["symbols"], **eligibility)
    if gate is not None:
        built = RegimeOverlay(built, gate)
    return built
