"""Buy once on the first date there is data, then hold. The yardstick."""

from __future__ import annotations

from decimal import Decimal

from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.strategies.base import BaseStrategy, normalise_symbols, require_weight


class BuyAndHold(BaseStrategy):
    strategy_id = "buy_and_hold"
    warmup_days = 1

    def __init__(self, symbols, target_weight: Decimal) -> None:
        self.symbols = normalise_symbols(symbols)
        self.target_weight = require_weight("target_weight", target_weight)

    def parameters(self) -> dict[str, object]:
        return {"symbols": self.symbols, "target_weight": self.target_weight}

    def _signals(self, view: PointInTimeView) -> list[Signal]:
        """Assert the desired state every bar: be long, never sell.

        Entering only on a symbol's first-ever bar looked equivalent and was
        not: over any window starting later than the series does, the
        strategy emitted HOLD from day one, never bought, and returned
        exactly 0.0%. A benchmark stuck in cash flatters every strategy
        measured against it.

        Re-asserting ENTER_LONG is also what lets the entry survive a
        rejection: if a breaker or a cash limit refuses the first order, the
        position is still wanted tomorrow. The engine treats a re-asserted
        entry on a position it already holds as a no-op, so this still buys
        exactly once.
        """
        out: list[Signal] = []
        for symbol in sorted(self.symbols):
            if not view.bars(symbol):
                continue
            out.append(
                self._signal(
                    view,
                    symbol,
                    SignalDirection.ENTER_LONG,
                    "buy and hold: long from the first bar in the window, never sells",
                    self.target_weight,
                )
            )
        return out
