"""Forecasters that know nothing special. They are the bar Kronos has to clear.

`DriftlessRandomWalk` has no directional view at all: Gaussian log returns
with the window's volatility and zero mean. Its cone has a realistic width
and its "paths up" count hovers around 50 by construction, so it should
almost never call a TRENDING regime. If a model's confident calls are right
no more often than this one's coin-flips, the confidence is decoration.

`BlockBootstrapForecaster` resamples the window's own daily log returns in
blocks, so it inherits the trailing drift, fat tails and short-range
autocorrelation of the last two years. That makes it a momentum forecaster
in disguise: after a strong run it puts most paths up. It is the harder null,
because a model that merely learned "recent trend continues" will look
exactly like it.
"""

from __future__ import annotations

import numpy as np

from signalforge.core.errors import ValidationError
from signalforge.forecasting.paths import (
    ContextWindow,
    PathForecast,
    require_horizon_and_paths,
)


def _require_context(window: ContextWindow, needed: int, who: str) -> np.ndarray:
    returns = window.log_returns[-(needed - 1):]
    if returns.size < 20:
        raise ValidationError(
            f"{who} needs at least 21 closes to estimate volatility, got {returns.size + 1}"
        )
    return returns


class DriftlessRandomWalk:
    forecaster_id = "random_walk"

    def __init__(self, context_length: int = 512) -> None:
        if context_length < 21:
            raise ValidationError(f"context_length must be at least 21, got {context_length}")
        self.context_length = context_length

    def parameters(self) -> dict[str, object]:
        return {"context_length": self.context_length, "drift": 0}

    def sample_paths(
        self, window: ContextWindow, horizon: int, n_paths: int, seed: int
    ) -> PathForecast:
        require_horizon_and_paths(horizon, n_paths)
        returns = _require_context(window, self.context_length, self.forecaster_id)
        sigma = float(np.std(returns, ddof=1))
        rng = np.random.default_rng(seed)
        steps = rng.normal(0.0, sigma, size=(n_paths, horizon))
        paths = window.last_close * np.exp(np.cumsum(steps, axis=1))
        return PathForecast(
            symbol=window.symbol,
            as_of=window.as_of,
            horizon=horizon,
            forecaster_id=self.forecaster_id,
            last_close=window.last_close,
            paths=paths,
        )


class BlockBootstrapForecaster:
    forecaster_id = "block_bootstrap"

    def __init__(self, context_length: int = 512, block: int = 5) -> None:
        if context_length < 21:
            raise ValidationError(f"context_length must be at least 21, got {context_length}")
        if isinstance(block, bool) or not isinstance(block, int) or block < 1:
            raise ValidationError(f"block must be a positive integer, got {block!r}")
        self.context_length = context_length
        self.block = block

    def parameters(self) -> dict[str, object]:
        return {"context_length": self.context_length, "block": self.block}

    def sample_paths(
        self, window: ContextWindow, horizon: int, n_paths: int, seed: int
    ) -> PathForecast:
        require_horizon_and_paths(horizon, n_paths)
        returns = _require_context(window, self.context_length, self.forecaster_id)
        n = returns.size
        block = min(self.block, n)
        rng = np.random.default_rng(seed)
        n_blocks = -(-horizon // block)
        # Circular blocks: a start near the end wraps to the beginning, so
        # every return in the window is equally likely to be drawn.
        starts = rng.integers(0, n, size=(n_paths, n_blocks))
        offsets = np.arange(block)
        idx = (starts[:, :, None] + offsets[None, None, :]) % n
        steps = returns[idx.reshape(n_paths, -1)[:, :horizon]]
        paths = window.last_close * np.exp(np.cumsum(steps, axis=1))
        return PathForecast(
            symbol=window.symbol,
            as_of=window.as_of,
            horizon=horizon,
            forecaster_id=self.forecaster_id,
            last_close=window.last_close,
            paths=paths,
        )
