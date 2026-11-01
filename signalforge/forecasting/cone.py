"""Reading a cone of sampled paths: median, band, spread, agreement, regime.

These are the quantities the Kronos demos put on screen ("median 82,640",
"71/100 paths up", "TRENDING"). They are computed here exactly once so that
every forecaster is read by the same rules, and so the trial can ask the
only question that matters about them: when the reading says confident, is
the forecast actually right more often?
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from signalforge.core.errors import ValidationError
from signalforge.forecasting.paths import PathForecast

DEFAULT_TREND_AGREEMENT = 0.65
"""Share of paths that must end on the same side of today's close for the
cone to be read as TRENDING. The demo screenshots label 69 and 71 of 100 as
trending and 49 and 51 as choppy; 0.65 sits between them."""

BAND_LOW = 0.10
BAND_HIGH = 0.90


class Regime(str, Enum):
    TRENDING = "TRENDING"
    CHOPPY = "CHOPPY"


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


@dataclass(frozen=True, slots=True)
class ConeReading:
    median: float
    p10: float
    p90: float
    last_close: float
    paths_up: int
    n_paths: int
    trend_agreement: float

    @property
    def up_fraction(self) -> float:
        return self.paths_up / self.n_paths

    @property
    def agreement(self) -> float:
        """Share of paths on the majority side: 0.5 is a coin, 1.0 unanimous."""
        return max(self.up_fraction, 1.0 - self.up_fraction)

    @property
    def direction(self) -> Direction:
        return Direction.UP if self.up_fraction >= 0.5 else Direction.DOWN

    @property
    def regime(self) -> Regime:
        return Regime.TRENDING if self.agreement >= self.trend_agreement else Regime.CHOPPY

    @property
    def spread_pct(self) -> float:
        """Width of the 10-90 band at the horizon, as a fraction of today's close."""
        return (self.p90 - self.p10) / self.last_close

    @property
    def median_return(self) -> float:
        return self.median / self.last_close - 1.0


def read_cone(
    forecast: PathForecast, trend_agreement: float = DEFAULT_TREND_AGREEMENT
) -> ConeReading:
    if not (0.5 < trend_agreement <= 1.0):
        raise ValidationError(f"trend_agreement must be in (0.5, 1], got {trend_agreement}")
    terminal = forecast.terminal
    p10, median, p90 = np.quantile(terminal, [BAND_LOW, 0.5, BAND_HIGH])
    return ConeReading(
        median=float(median),
        p10=float(p10),
        p90=float(p90),
        last_close=forecast.last_close,
        paths_up=int(np.sum(terminal > forecast.last_close)),
        n_paths=forecast.n_paths,
        trend_agreement=trend_agreement,
    )
