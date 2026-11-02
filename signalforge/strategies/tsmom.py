"""Time-series momentum: hold each asset while its own trailing return is positive.

Moskowitz, Ooi and Pedersen (2012), long-only: an asset is held at a fixed
weight while its total return over the last `lookback` sessions is above zero,
and held in cash otherwise. Each asset is judged against its own past, not
against the others, which is what separates it from cross-sectional momentum.

The decision is monthly. It is taken on the first session of each month from
the close of the session before it (the previous month's last close), because
a session is only known to be a month's last once the next one has printed.
Between decisions the strategy re-asserts the month's state. Everything is
recomputed from the view each bar, so there is no hidden state to drift.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import Signal, SignalDirection
from signalforge.strategies.base import (
    ZERO,
    BaseStrategy,
    normalise_symbols,
    require_positive_int,
    require_weight,
)

# A month has at most 23 sessions; this many recent bars always reach back
# past the month's first session to the close the decision is taken from.
MONTH_SESSIONS_BOUND = 24


def decision_reference(days: tuple[date, ...]) -> date | None:
    """The close this month's decision reads: the session before the month's first.

    None until the view holds a month boundary, so a window never acts on a
    month it did not see begin.
    """
    for i in range(len(days) - 1, 0, -1):
        if (days[i].year, days[i].month) != (days[i - 1].year, days[i - 1].month):
            return days[i - 1]
    return None


class TimeSeriesMomentum(BaseStrategy):
    strategy_id = "tsmom"

    def __init__(self, symbols, lookback: int, target_weight: Decimal) -> None:
        self.symbols = normalise_symbols(symbols)
        self.lookback = require_positive_int("lookback", lookback)
        self.target_weight = require_weight("target_weight", target_weight)
        self.warmup_days = self.lookback + 1

    def parameters(self) -> dict[str, object]:
        return {"symbols": self.symbols, "lookback": self.lookback, "target_weight": self.target_weight}

    def trailing_return(self, view: PointInTimeView, symbol: str, reference: date) -> Decimal | None:
        """Total return over `lookback` sessions ending at `reference`, or None.

        None when the symbol has no bar on the reference session or lacks
        `lookback` earlier sessions: a name is not judged on a partial year.
        """
        bars = view.bars(symbol, self.lookback + MONTH_SESSIONS_BOUND)
        upto = [b for b in bars if b.trade_date <= reference]
        if not upto or upto[-1].trade_date != reference or len(upto) < self.lookback + 1:
            return None
        start, end = upto[-self.lookback - 1], upto[-1]
        first = start.adj_close if start.adj_close is not None else start.close
        last = end.adj_close if end.adj_close is not None else end.close
        if first <= ZERO:
            return None
        return last / first - Decimal(1)

    def _signals(self, view: PointInTimeView) -> list[Signal]:
        reference = decision_reference(view.trading_days)
        if reference is None:
            return []
        out: list[Signal] = []
        for symbol in sorted(self.symbols):
            ret = self.trailing_return(view, symbol, reference)
            if ret is None:
                continue
            detail = f"{self.lookback}-session return {ret:.2%} to {reference}"
            if ret > ZERO:
                out.append(self._signal(view, symbol, SignalDirection.ENTER_LONG,
                                        f"own trend positive: {detail}", self.target_weight))
            else:
                out.append(self._signal(view, symbol, SignalDirection.EXIT_LONG,
                                        f"own trend not positive: {detail}"))
        return out
