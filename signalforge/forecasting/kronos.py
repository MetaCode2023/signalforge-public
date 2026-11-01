"""Kronos as a SignalForge path forecaster.

Kronos (NeoQuasar/Kronos-small, about 24.7M parameters, 512-bar context) is a
decoder-only model trained on K-line sequences: it tokenizes OHLCV bars and
samples the next bar the way a language model samples the next word. This
adapter turns it into a `PathForecaster` so it can be put through exactly the
same trial as the random-walk and bootstrap nulls.

Three things about it are deliberate.

1. **One path per sample, never the average.** The stock `predict()` call
   with `sample_count=N` returns the MEAN of the N samples, which is one
   smooth line rather than a cone. Spread, "71 of 100 paths up" and the regime
   call all need the individual paths, so this adapter replicates the context
   N times along the batch axis and samples each copy once.

2. **Local weights only.** The model and tokenizer are loaded from
   directories on disk, never from a hub id, so constructing or running a
   forecaster cannot open a network connection. Download them once,
   deliberately:

       huggingface-cli download NeoQuasar/Kronos-small --local-dir models/kronos-small
       huggingface-cli download NeoQuasar/Kronos-Tokenizer-base --local-dir models/kronos-tokenizer-base
       git clone https://github.com/shiyu-coder/Kronos models/kronos-src

3. **No pandas on our side.** The project cannot import pandas on the build
   machine (see README). The adapter builds every input array itself and
   calls `KronosPredictor.generate`, which takes NumPy arrays. The upstream
   `model` package may still import pandas at module level; if it does on a
   machine that blocks it, loading fails with a message saying so rather
   than a DLL traceback.

The inputs mirror upstream `KronosPredictor.predict`: columns open, high,
low, close, volume, amount (amount = volume x mean(OHLC)); per-column z-score
over the context window, clipped at +/-clip; time features minute, hour,
weekday, day, month. Future time stamps are the next business days, because
the forecast cannot know the exchange calendar.

This module was written against the published upstream interface and is
exercised in the tests with a stand-in predictor of the same shape. Run
`python -m signalforge.forecast_cli kronos-check` once on a machine with the
weights before trusting a Kronos trial.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from signalforge.core.errors import ConfigError, ValidationError
from signalforge.forecasting.paths import (
    CLOSE,
    ContextWindow,
    PathForecast,
    require_horizon_and_paths,
)

KRONOS_FEATURES = ("open", "high", "low", "close", "volume", "amount")
TIME_FEATURES = ("minute", "hour", "weekday", "day", "month")
NORM_EPS = 1e-5


def future_business_days(after: date, count: int) -> tuple[date, ...]:
    out: list[date] = []
    day = after
    while len(out) < count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return tuple(out)


def time_features(days) -> np.ndarray:
    """[len(days), 5] float32: minute, hour, weekday, day, month.

    Daily bars have no intraday time, so minute and hour are zero, which is
    what upstream produces for a midnight-stamped daily index.
    """
    return np.asarray(
        [(0, 0, d.weekday(), d.day, d.month) for d in days], dtype=np.float32
    )


def kronos_inputs(window: ContextWindow, clip: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Normalised [L, 6] features plus the mean and std to undo it."""
    o, h, low, c, v = (window.ohlcv[:, i] for i in range(5))
    if not window.volume_known:
        # Upstream fills a missing volume column with zeros; do the same and
        # let the caller know through the window flag rather than inventing one.
        v = np.zeros_like(c)
    amount = v * (o + h + low + c) / 4.0
    x = np.column_stack([o, h, low, c, v, amount]).astype(np.float64)
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    z = np.clip((x - mean) / (std + NORM_EPS), -clip, clip)
    return z.astype(np.float32), mean, std


class KronosForecaster:
    forecaster_id = "kronos"

    def __init__(
        self,
        model_dir: str | Path,
        tokenizer_dir: str | Path,
        *,
        kronos_src: str | Path | None = None,
        device: str = "cpu",
        context_length: int = 512,
        temperature: float = 1.0,
        top_p: float = 0.9,
        clip: float = 5.0,
        batch_size: int = 25,
        predictor=None,
    ) -> None:
        if context_length < 2 or context_length > 512:
            raise ValidationError(
                f"Kronos-small/base take at most 512 bars of context, got {context_length}"
            )
        if not (0.0 < top_p <= 1.0):
            raise ValidationError(f"top_p must be in (0, 1], got {top_p}")
        if temperature <= 0:
            raise ValidationError(f"temperature must be positive, got {temperature}")
        if batch_size < 1:
            raise ValidationError(f"batch_size must be positive, got {batch_size}")
        self.model_dir = Path(model_dir)
        self.tokenizer_dir = Path(tokenizer_dir)
        self.kronos_src = Path(kronos_src) if kronos_src is not None else None
        self.device = device
        self.context_length = context_length
        self.temperature = temperature
        self.top_p = top_p
        self.clip = clip
        self.batch_size = batch_size
        self._predictor = predictor

    def parameters(self) -> dict[str, object]:
        return {
            "model": self.model_dir.name,
            "tokenizer": self.tokenizer_dir.name,
            "context_length": self.context_length,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "clip": self.clip,
            "device": self.device,
        }

    def _load(self):
        if self._predictor is not None:
            return self._predictor
        for label, path in (("model", self.model_dir), ("tokenizer", self.tokenizer_dir)):
            if not path.is_dir():
                raise ConfigError(
                    f"Kronos {label} directory {path} does not exist. Weights are loaded "
                    "from disk only, never fetched; see signalforge/forecasting/kronos.py "
                    "for the one-time download commands."
                )
        if self.kronos_src is not None:
            if not (self.kronos_src / "model").is_dir():
                raise ConfigError(f"{self.kronos_src} does not contain the Kronos `model` package")
            if str(self.kronos_src) not in sys.path:
                sys.path.insert(0, str(self.kronos_src))
        try:
            import torch  # noqa: F401
            from model import Kronos, KronosPredictor, KronosTokenizer
        except ImportError as exc:
            raise ConfigError(
                "Kronos could not be imported. It needs torch, einops and the upstream "
                "`model` package (pass --kronos-src). If the error names pandas, this "
                f"machine blocks it and Kronos must run elsewhere. Underlying error: {exc}"
            ) from exc
        tokenizer = KronosTokenizer.from_pretrained(str(self.tokenizer_dir))
        model = Kronos.from_pretrained(str(self.model_dir))
        self._predictor = KronosPredictor(
            model, tokenizer, device=self.device, max_context=self.context_length
        )
        return self._predictor

    def _seed(self, seed: int) -> None:
        np.random.seed(seed % (2**32))
        try:
            import torch
        except ImportError:
            return
        torch.manual_seed(seed)

    def sample_paths(
        self, window: ContextWindow, horizon: int, n_paths: int, seed: int
    ) -> PathForecast:
        require_horizon_and_paths(horizon, n_paths)
        if len(window.dates) > self.context_length:
            raise ValidationError(
                f"window has {len(window.dates)} bars; this forecaster takes {self.context_length}"
            )
        predictor = self._load()
        self._seed(seed)
        z, mean, std = kronos_inputs(window, self.clip)
        x_stamp = time_features(window.dates)
        y_stamp = time_features(future_business_days(window.as_of, horizon))

        chunks = []
        remaining = n_paths
        while remaining:
            b = min(self.batch_size, remaining)
            preds = predictor.generate(
                np.repeat(z[None], b, axis=0),
                np.repeat(x_stamp[None], b, axis=0),
                np.repeat(y_stamp[None], b, axis=0),
                horizon,
                self.temperature,
                0,
                self.top_p,
                1,
                False,
            )
            preds = np.asarray(preds, dtype=np.float64)
            if preds.shape != (b, horizon, len(KRONOS_FEATURES)):
                raise ConfigError(
                    f"Kronos returned shape {preds.shape}, expected {(b, horizon, 6)}; "
                    "the upstream generate() signature may have changed"
                )
            chunks.append(preds)
            remaining -= b

        raw = np.concatenate(chunks, axis=0) * (std + NORM_EPS) + mean
        ohlc = raw[:, :, :4]
        # A sampled bar can come back with high below its own open or close;
        # the cone uses closes, and the candles are repaired rather than shown
        # as impossible bars.
        ohlc[:, :, 1] = ohlc.max(axis=2)
        ohlc[:, :, 2] = ohlc.min(axis=2)
        closes = raw[:, :, CLOSE]
        if np.any(closes <= 0):
            raise ValidationError(
                f"Kronos produced a non-positive close for {window.symbol} at {window.as_of}"
            )
        return PathForecast(
            symbol=window.symbol,
            as_of=window.as_of,
            horizon=horizon,
            forecaster_id=self.forecaster_id,
            last_close=window.last_close,
            paths=closes,
            ohlc_paths=ohlc,
        )
