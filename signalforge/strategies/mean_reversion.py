"""Z-score mean reversion. Long-only: buy statistically cheap, exit at the mean."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.strategies.base import (
    BaseStrategy,
    normalise_symbols,
    require_positive_int,
    require_weight,
)

FLAT_TOLERANCE = 1e-12


class ZScoreMeanReversion(BaseStrategy):
    """Position state is replayed from the visible history on every call, so the
    strategy holds no memory between calls and is a pure function of its view.

    Floats are used for the z-score only (a decision statistic, never money);
    weights stay Decimal.
    """

    strategy_id = "zscore_mean_reversion"

    def __init__(
        self,
        window: int,
        entry_z: Decimal,
        exit_z: Decimal,
        target_weight: Decimal,
        max_holding_days: int,
        symbols,
    ) -> None:
        self.window = require_positive_int("window", window)
        if self.window < 3:
            raise ValidationError("window must be at least 3 to estimate a deviation")
        self.entry_z = Decimal(entry_z)
        self.exit_z = Decimal(exit_z)
        if self.entry_z <= 0:
            raise ValidationError(f"entry_z must be positive, got {entry_z}")
        if self.exit_z >= self.entry_z:
            raise ValidationError(
                f"exit_z ({exit_z}) must be below entry_z ({entry_z}) or a position exits where it entered"
            )
        self.target_weight = require_weight("target_weight", target_weight)
        self.max_holding_days = require_positive_int("max_holding_days", max_holding_days)
        self.symbols = normalise_symbols(symbols)
        self.warmup_days = self.window

    def parameters(self) -> dict[str, object]:
        return {
            "window": self.window,
            "entry_z": self.entry_z,
            "exit_z": self.exit_z,
            "target_weight": self.target_weight,
            "max_holding_days": self.max_holding_days,
            "symbols": self.symbols,
        }

    def _zscores(self, prices: np.ndarray) -> np.ndarray:
        windows = sliding_window_view(prices, self.window)
        mean = windows.mean(axis=1)
        std = windows.std(axis=1, ddof=1)
        z = np.full(len(windows), np.nan)
        ok = std > FLAT_TOLERANCE * np.maximum(1.0, np.abs(mean))
        z[ok] = (prices[self.window - 1:][ok] - mean[ok]) / std[ok]
        return z

    def _signals(self, view: PointInTimeView) -> list[Signal]:
        entry, exit_ = float(self.entry_z), float(self.exit_z)
        out: list[Signal] = []
        for symbol in sorted(self.symbols):
            if not view.has_history(symbol, self.window):
                continue
            prices = np.array([float(p) for p in view.adjusted_closes(symbol)])
            z = self._zscores(prices)
            held: int | None = None
            for value in z:
                if held is None:
                    if value < -entry:
                        held = 0
                else:
                    held += 1
                    if value > -exit_ or held >= self.max_holding_days:
                        held = None
            z_now = z[-1]
            z_text = "undefined (zero variance)" if np.isnan(z_now) else f"{z_now:.3f}"
            if held is not None:
                out.append(self._signal(
                    view, symbol, SignalDirection.ENTER_LONG,
                    f"long: z={z_text} (entry below -{self.entry_z}), held {held}d of max {self.max_holding_days}",
                    self.target_weight,
                    Decimal(str(round(-z_now, 6))) if z_now < 0 else Decimal(0),
                ))
            else:
                out.append(self._signal(
                    view, symbol, SignalDirection.EXIT_LONG,
                    f"flat: z={z_text}, no entry below -{self.entry_z} or exited above -{self.exit_z}",
                ))
        return out
