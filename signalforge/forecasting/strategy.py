"""The demo's trading rule, as a SignalForge strategy that has to earn its keep.

Rule: every `rebalance_every` bars, forecast `horizon` bars ahead. Be long
the symbol only while the cone reads TRENDING and UP (at least
`trend_agreement` of paths end above today's close); otherwise hold cash.

It is an ordinary `BaseStrategy`: it sees a `PointInTimeView`, proposes
signals, and the engine, fill model and risk engine decide what happens. It
lives in `forecasting/` rather than `strategies/` and is not registered in
the baseline suite or the paper runner. Nothing promotes it but the
promotion gate, and nothing should until a trial says the cone means
something.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.forecasting.cone import DEFAULT_TREND_AGREEMENT, Direction, Regime, read_cone
from signalforge.forecasting.paths import (
    ContextWindow,
    InsufficientContext,
    PathForecaster,
    derive_seed,
    require_horizon_and_paths,
)
from signalforge.strategies.base import (
    BaseStrategy,
    normalise_symbols,
    require_positive_int,
    require_weight,
)


class ForecastConeStrategy(BaseStrategy):
    def __init__(
        self,
        forecaster: PathForecaster,
        symbols,
        target_weight: Decimal,
        *,
        horizon: int = 24,
        n_paths: int = 100,
        rebalance_every: int | None = None,
        trend_agreement: float = DEFAULT_TREND_AGREEMENT,
        seed: int = 20261031,
    ) -> None:
        require_horizon_and_paths(horizon, n_paths)
        self.forecaster = forecaster
        self.symbols = normalise_symbols(symbols)
        self.target_weight = require_weight("target_weight", target_weight)
        self.horizon = horizon
        self.n_paths = n_paths
        self.rebalance_every = require_positive_int(
            "rebalance_every", horizon if rebalance_every is None else rebalance_every
        )
        if not (0.5 < trend_agreement <= 1.0):
            raise ValidationError(f"trend_agreement must be in (0.5, 1], got {trend_agreement}")
        self.trend_agreement = trend_agreement
        self.seed = seed
        self.strategy_id = f"cone_{forecaster.forecaster_id}"
        self.warmup_days = forecaster.context_length
        # symbol -> (anchor date, wants long, reason). A memo of the last
        # forecast, rebuilt from scratch whenever the clock runs backwards
        # (a new run), so reuse cannot leak one run's decisions into another.
        self._last: dict[str, tuple[date, bool, str]] = {}
        self._clock: date | None = None

    def parameters(self) -> dict[str, object]:
        return {
            "forecaster": self.forecaster.forecaster_id,
            "forecaster_parameters": dict(self.forecaster.parameters()),
            "symbols": self.symbols,
            "target_weight": self.target_weight,
            "horizon": self.horizon,
            "n_paths": self.n_paths,
            "rebalance_every": self.rebalance_every,
            "trend_agreement": self.trend_agreement,
            "seed": self.seed,
        }

    def _due(self, view: PointInTimeView, symbol: str) -> bool:
        memo = self._last.get(symbol)
        if memo is None:
            return True
        recent = view.bars(symbol, lookback=self.rebalance_every + 1)
        since = sum(1 for b in recent if b.trade_date > memo[0])
        return since >= self.rebalance_every

    def _decide(self, view: PointInTimeView, symbol: str) -> tuple[bool, str] | None:
        try:
            window = ContextWindow.from_view(view, symbol, self.forecaster.context_length)
        except InsufficientContext:
            return None
        forecast = self.forecaster.sample_paths(
            window, self.horizon, self.n_paths,
            derive_seed(self.seed, symbol, view.as_of.isoformat()),
        )
        r = read_cone(forecast, self.trend_agreement)
        long = r.regime is Regime.TRENDING and r.direction is Direction.UP
        reason = (
            f"{self.forecaster.forecaster_id} cone {self.horizon}d: {r.paths_up}/{r.n_paths} "
            f"paths up, {r.regime.value}, median {r.median_return:+.2%}, "
            f"10-90 spread {r.spread_pct:.1%}"
        )
        return long, reason

    def _signals(self, view: PointInTimeView) -> list[Signal]:
        if self._clock is not None and view.as_of <= self._clock:
            self._last.clear()
        self._clock = view.as_of
        out: list[Signal] = []
        for symbol in sorted(self.symbols):
            if not view.has_history(symbol, self.forecaster.context_length):
                continue
            if self._due(view, symbol):
                decision = self._decide(view, symbol)
                if decision is None:
                    continue
                self._last[symbol] = (view.as_of, *decision)
            _, long, reason = self._last[symbol]
            # Re-assert the standing decision every bar, as BuyAndHold does,
            # so an entry the risk engine refused is retried tomorrow.
            out.append(self._signal(
                view,
                symbol,
                SignalDirection.ENTER_LONG if long else SignalDirection.EXIT_LONG,
                reason,
                self.target_weight,
            ))
        return out
