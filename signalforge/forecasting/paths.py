"""The forecaster contract: a context window in, N sampled future paths out."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

import numpy as np

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView

# Column order of ContextWindow.ohlcv.
OPEN, HIGH, LOW, CLOSE, VOLUME = range(5)


@dataclass(frozen=True, eq=False)
class ContextWindow:
    """The last `len(dates)` bars of one symbol, ending at the forecast origin.

    Prices are on the adjusted basis (each bar's OHLC scaled by its
    adj_close/close ratio, volume by the inverse), so a split inside the
    window is not a crash the forecaster has to explain. A history built with
    `price_basis="adjusted"` already has a ratio of exactly 1, so applying it
    again here is a no-op rather than a double adjustment.
    """

    symbol: str
    as_of: date
    dates: tuple[date, ...]
    ohlcv: np.ndarray
    volume_known: bool = True

    def __post_init__(self) -> None:
        # Own a private float64 copy, then freeze it: the caller's array is
        # neither aliased nor locked.
        object.__setattr__(self, "ohlcv", np.array(self.ohlcv, dtype=np.float64, copy=True))
        object.__setattr__(self, "dates", tuple(self.dates))
        if self.ohlcv.ndim != 2 or self.ohlcv.shape[1] != 5:
            raise ValidationError(f"ohlcv must be [L, 5], got {self.ohlcv.shape}")
        if self.ohlcv.shape[0] != len(self.dates):
            raise ValidationError(
                f"{len(self.dates)} dates for {self.ohlcv.shape[0]} rows of ohlcv"
            )
        if not self.dates:
            raise ValidationError("a context window needs at least one bar")
        if self.dates[-1] != self.as_of:
            raise ValidationError(
                f"window ends {self.dates[-1]} but claims as_of {self.as_of}; "
                "a forecast origin is the date its data ends"
            )
        if any(b <= a for a, b in zip(self.dates, self.dates[1:])):
            raise ValidationError("context window dates must be strictly increasing")
        if not np.all(np.isfinite(self.ohlcv)) or np.any(self.ohlcv[:, :4] <= 0):
            raise ValidationError(f"{self.symbol}: non-finite or non-positive price in window")
        self.ohlcv.setflags(write=False)

    @classmethod
    def from_view(cls, view: PointInTimeView, symbol: str, length: int) -> ContextWindow:
        """Cut the window from a point-in-time view. Refuses a short history.

        A forecaster that silently ran on 40 bars when it was built for 512
        would produce a cone that looks exactly like a real one.
        """
        bars = view.bars(symbol, lookback=length)
        if len(bars) < length:
            raise InsufficientContext(
                f"{symbol} has {len(bars)} bars at {view.as_of}; the forecaster needs {length}"
            )
        if bars[-1].trade_date != view.as_of:
            raise InsufficientContext(
                f"{symbol} has no bar on {view.as_of} (last is {bars[-1].trade_date}); "
                "a stale window is not a forecast from today"
            )
        rows = []
        for b in bars:
            ratio = float(b.adjustment_ratio) if b.adjustment_ratio is not None else 1.0
            rows.append((
                float(b.open) * ratio,
                float(b.high) * ratio,
                float(b.low) * ratio,
                float(b.close) * ratio,
                float(b.volume) / ratio,
            ))
        return cls(
            symbol=symbol,
            as_of=view.as_of,
            dates=tuple(b.trade_date for b in bars),
            ohlcv=np.asarray(rows, dtype=np.float64),
            volume_known=all(b.volume_known for b in bars),
        )

    @classmethod
    def from_closes(cls, symbol: str, dates, closes) -> ContextWindow:
        """A close-only window (open = high = low = close, volume unknown)."""
        c = np.asarray(closes, dtype=np.float64)
        ohlcv = np.column_stack([c, c, c, c, np.zeros_like(c)])
        dates = tuple(dates)
        return cls(symbol=symbol, as_of=dates[-1], dates=dates, ohlcv=ohlcv, volume_known=False)

    @property
    def closes(self) -> np.ndarray:
        return self.ohlcv[:, CLOSE]

    @property
    def last_close(self) -> float:
        return float(self.ohlcv[-1, CLOSE])

    @property
    def log_returns(self) -> np.ndarray:
        c = self.closes
        return np.diff(np.log(c))


class InsufficientContext(ValidationError):
    """The view does not hold enough history at this origin to forecast."""


@dataclass(frozen=True, eq=False)
class PathForecast:
    """N sampled closing-price paths for the `horizon` bars after `as_of`."""

    symbol: str
    as_of: date
    horizon: int
    forecaster_id: str
    last_close: float
    paths: np.ndarray
    """[n_paths, horizon] closes. Column h is the close h+1 bars after as_of."""
    ohlc_paths: np.ndarray | None = field(default=None)
    """Optional [n_paths, horizon, 4] OHLC, for forecasters that produce candles."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", np.array(self.paths, dtype=np.float64, copy=True))
        if self.paths.ndim != 2 or self.paths.shape[1] != self.horizon:
            raise ValidationError(
                f"paths must be [n, {self.horizon}], got {self.paths.shape}"
            )
        if self.paths.shape[0] < 2:
            raise ValidationError("a cone needs at least two paths")
        if not np.all(np.isfinite(self.paths)):
            raise ValidationError(f"{self.forecaster_id} produced a non-finite path")
        self.paths.setflags(write=False)

    @property
    def n_paths(self) -> int:
        return int(self.paths.shape[0])

    @property
    def terminal(self) -> np.ndarray:
        return self.paths[:, -1]

    def band(self, q: float) -> np.ndarray:
        """The q-quantile of the paths at every step, shape [horizon]."""
        return np.quantile(self.paths, q, axis=0)


@runtime_checkable
class PathForecaster(Protocol):
    forecaster_id: str
    context_length: int

    def parameters(self) -> dict[str, object]:
        """Every setting that changes the output, for the trial record."""
        ...

    def sample_paths(
        self, window: ContextWindow, horizon: int, n_paths: int, seed: int
    ) -> PathForecast:
        ...


def derive_seed(base: int, *parts: object) -> int:
    """A stable per-forecast seed, so a rerun reproduces every path exactly.

    Python's hash() is salted per process; sha256 is not.
    """
    text = "|".join([str(base), *(str(p) for p in parts)])
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") % (2**63)


def require_horizon_and_paths(horizon: int, n_paths: int) -> None:
    for name, value in (("horizon", horizon), ("n_paths", n_paths)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError(f"{name} must be a positive integer, got {value!r}")
    if n_paths < 2:
        raise ValidationError("n_paths must be at least 2 to form a cone")
