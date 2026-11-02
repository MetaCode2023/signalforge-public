"""Walk-forward forecast trial: forecast at each origin, score after the fact.

The forecaster sees only a `ContextWindow` cut from `history.view(origin)`.
The trial itself reads the realized closes, because grading is the
evaluator's job, and it refuses any outcome dated after `end` so a window
boundary is never crossed to finish a forecast.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory
from signalforge.forecasting.cone import DEFAULT_TREND_AGREEMENT
from signalforge.forecasting.paths import (
    ContextWindow,
    InsufficientContext,
    PathForecast,
    PathForecaster,
    derive_seed,
    require_horizon_and_paths,
)
from signalforge.forecasting.scoring import ForecastOutcome, TrialSummary, score, summarize


@dataclass(slots=True)
class TrialResult:
    forecaster_id: str
    parameters: dict
    symbols: tuple[str, ...]
    start: date
    end: date
    horizon: int
    n_paths: int
    stride: int
    seed: int
    trend_agreement: float
    outcomes: list[ForecastOutcome] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def summary(self) -> TrialSummary:
        return summarize(self.outcomes, stride=self.stride, horizon=self.horizon)


def _adjusted_close(bar) -> float:
    return float(bar.adj_close if bar.adj_close is not None else bar.close)


def run_trial(
    history: PriceHistory,
    symbols,
    forecaster: PathForecaster,
    *,
    start: date,
    end: date,
    horizon: int = 24,
    n_paths: int = 100,
    stride: int | None = None,
    seed: int = 20261031,
    trend_agreement: float = DEFAULT_TREND_AGREEMENT,
    progress=None,
) -> TrialResult:
    require_horizon_and_paths(horizon, n_paths)
    stride = horizon if stride is None else stride
    if isinstance(stride, bool) or not isinstance(stride, int) or stride < 1:
        raise ValidationError(f"stride must be a positive integer, got {stride!r}")
    if end <= start:
        raise ValidationError(f"trial window {start}..{end} is empty")
    result = TrialResult(
        forecaster_id=forecaster.forecaster_id,
        parameters=dict(forecaster.parameters()),
        symbols=tuple(symbols),
        start=start,
        end=end,
        horizon=horizon,
        n_paths=n_paths,
        stride=stride,
        seed=seed,
        trend_agreement=trend_agreement,
    )
    for symbol in result.symbols:
        bars = history.bars_for(symbol)
        if not bars:
            result.skipped["no bars"] = result.skipped.get("no bars", 0) + 1
            continue
        # Origins: every `stride`-th bar inside the window whose full horizon
        # of outcomes also lies inside it.
        inside = [i for i, b in enumerate(bars) if start <= b.trade_date <= end]
        usable = [i for i in inside if i + horizon < len(bars) and bars[i + horizon].trade_date <= end]
        for i in usable[::stride]:
            origin = bars[i].trade_date
            try:
                window = ContextWindow.from_view(history.view(origin), symbol, forecaster.context_length)
            except InsufficientContext:
                result.skipped["short context"] = result.skipped.get("short context", 0) + 1
                continue
            forecast = forecaster.sample_paths(
                window, horizon, n_paths, derive_seed(seed, symbol, origin.isoformat())
            )
            _check_forecast(forecast, window, horizon, n_paths)
            realized = [_adjusted_close(b) for b in bars[i + 1:i + 1 + horizon]]
            result.outcomes.append(score(forecast, realized, trend_agreement))
            if progress is not None:
                progress(symbol, origin)
    return result


def _check_forecast(forecast: PathForecast, window: ContextWindow, horizon: int, n_paths: int) -> None:
    if forecast.as_of != window.as_of or forecast.symbol != window.symbol:
        raise ValidationError(
            f"forecaster answered for {forecast.symbol}@{forecast.as_of}, "
            f"asked for {window.symbol}@{window.as_of}"
        )
    if forecast.paths.shape != (n_paths, horizon):
        raise ValidationError(f"expected {n_paths}x{horizon} paths, got {forecast.paths.shape}")
    if not math.isclose(forecast.last_close, window.last_close, rel_tol=1e-12):
        raise ValidationError("forecast last_close does not match the window it was given")


def forecast_latest(
    history: PriceHistory,
    symbol: str,
    forecaster: PathForecaster,
    *,
    horizon: int = 24,
    n_paths: int = 100,
    seed: int = 20261031,
) -> PathForecast:
    """A forward cone from the symbol's most recent bar. Nothing to score yet."""
    bars = history.bars_for(symbol)
    if not bars:
        raise ValidationError(f"no bars for {symbol}")
    origin = bars[-1].trade_date
    window = ContextWindow.from_view(history.view(origin), symbol, forecaster.context_length)
    return forecaster.sample_paths(window, horizon, n_paths, derive_seed(seed, symbol, origin.isoformat()))
