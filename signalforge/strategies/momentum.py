"""Cross-sectional momentum: hold the top_n by trailing return, skipping recent bars."""

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


class CrossSectionalMomentum(BaseStrategy):
    strategy_id = "xs_momentum"

    def __init__(
        self,
        lookback_days: int,
        skip_days: int,
        top_n: int,
        target_weight_total: Decimal,
        min_history_days: int,
        symbols=None,
    ) -> None:
        self.lookback_days = require_positive_int("lookback_days", lookback_days)
        if isinstance(skip_days, bool) or not isinstance(skip_days, int) or skip_days < 0:
            raise ValidationError(f"skip_days must be a non-negative integer, got {skip_days!r}")
        self.skip_days = skip_days
        self.top_n = require_positive_int("top_n", top_n)
        self.target_weight_total = require_weight("target_weight_total", target_weight_total)
        self.min_history_days = require_positive_int("min_history_days", min_history_days)
        self.symbols = None if symbols is None else normalise_symbols(symbols)
        self.required_bars = max(self.min_history_days, self.lookback_days + self.skip_days + 1)
        self.warmup_days = self.required_bars

    def parameters(self) -> dict[str, object]:
        params: dict[str, object] = {
            "lookback_days": self.lookback_days,
            "skip_days": self.skip_days,
            "top_n": self.top_n,
            "target_weight_total": self.target_weight_total,
            "min_history_days": self.min_history_days,
        }
        if self.symbols is not None:
            params["symbols"] = self.symbols
        return params

    def _signals(self, view: PointInTimeView) -> list[Signal]:
        ranked: list[tuple[Decimal, str]] = []
        for symbol in view.cross_section():
            if self.symbols is not None and symbol not in self.symbols:
                continue
            if not view.has_history(symbol, self.required_bars):
                continue
            closes = view.adjusted_closes(symbol, self.lookback_days + self.skip_days + 1)
            end = closes[len(closes) - 1 - self.skip_days]
            start = closes[0]
            if start <= 0:
                continue
            ranked.append((end / start - Decimal(1), symbol))
        ranked.sort(key=lambda t: (-t[0], t[1]))
        each = (self.target_weight_total / self.top_n).quantize(WEIGHT_QUANT, ROUND_DOWN)
        out: list[Signal] = []
        for rank, (ret, symbol) in enumerate(ranked, start=1):
            detail = f"rank {rank}/{len(ranked)}, {self.lookback_days}d return {ret:.4f} (skip {self.skip_days})"
            if rank <= self.top_n:
                out.append(self._signal(view, symbol, SignalDirection.ENTER_LONG,
                                        f"top {self.top_n} momentum: {detail}", each, ret))
            else:
                out.append(self._signal(view, symbol, SignalDirection.EXIT_LONG,
                                        f"outside top {self.top_n}: {detail}", strength=ret))
        return sorted(out, key=lambda s: s.symbol)
