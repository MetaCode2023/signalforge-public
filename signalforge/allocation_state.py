"""Where the allocation book stands today: each asset's trend and its distance from its high.

Computed with the strategy's own `decision_reference` and `trailing_return`,
so the page and the book cannot disagree about what is held. It measures no
performance and reads nothing about any evaluation window: it is the state the
rule is in now, the same thing a paper session would act on.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from signalforge.config.models import AllocationConfig
from signalforge.data.storage import Warehouse
from signalforge.pipeline import ohlcv_history
from signalforge.strategies.tsmom import TimeSeriesMomentum, decision_reference

HIGH_WINDOW = 252
# Calendar days loaded: the lookback, a month of slack and the one-year high.
LOAD_CALENDAR_DAYS = 600
STALE_AFTER_DAYS = 5

ON, OFF, YOUNG = "on", "off", "not enough history"
STATE_TEXT = {ON: "held", OFF: "cash", YOUNG: "waiting (history)"}


@dataclass(frozen=True)
class AssetState:
    symbol: str
    asset_class: str
    state: str
    decided_return: Decimal | None
    current_return: Decimal | None
    below_high: Decimal | None
    last_date: date | None
    stale: bool

    @property
    def flip_watch(self) -> bool:
        """Today's trailing return disagrees with the month's decision."""
        if self.state == YOUNG or self.current_return is None:
            return False
        return (self.current_return > 0) != (self.state == ON)


@dataclass
class MarketState:
    name: str
    as_of: date
    reference: date
    target_weight: Decimal
    lookback: int
    assets: list[AssetState] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def invested(self) -> Decimal:
        return self.target_weight * sum(1 for a in self.assets if a.state == ON)

    @property
    def next_decision_month(self) -> date:
        """The first day of the month whose first session takes the next decision."""
        days = calendar.monthrange(self.as_of.year, self.as_of.month)[1]
        return self.as_of.replace(day=1) + timedelta(days=days)


def _latest_bar_date(warehouse: Warehouse, allocation: AllocationConfig) -> date:
    symbols = list(allocation.symbols)
    row = warehouse.con.execute(
        f"SELECT max(trade_date) FROM bars WHERE source = ? AND symbol IN ({', '.join('?' for _ in symbols)})",
        [allocation.source, *symbols],
    ).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"no {allocation.source} bars in the warehouse for allocation {allocation.name}")
    return row[0]


def market_state(warehouse: Warehouse, allocation: AllocationConfig, as_of: date | None = None) -> MarketState:
    end = as_of or _latest_bar_date(warehouse, allocation)
    loaded = ohlcv_history(
        warehouse, list(allocation.symbols), sources=[allocation.source], price_basis="adjusted",
        start=end - timedelta(days=LOAD_CALENDAR_DAYS), end=end,
    )
    history = loaded.history
    latest = history.trading_days[-1]
    view = history.view(latest)
    reference = decision_reference(view.trading_days)
    if reference is None:
        raise ValueError("the loaded history holds no month boundary; load more bars")
    rule = TimeSeriesMomentum(allocation.symbols, allocation.lookback, allocation.target_weight)
    state = MarketState(
        name=allocation.name, as_of=latest, reference=reference,
        target_weight=allocation.target_weight, lookback=allocation.lookback,
        notes=list(loaded.limitations),
    )
    for symbol in allocation.symbols:
        bars = view.bars(symbol)
        last = bars[-1] if bars else None
        decided = rule.trailing_return(view, symbol, reference)
        current = rule.trailing_return(view, symbol, last.trade_date) if last else None
        closes = view.adjusted_closes(symbol, HIGH_WINDOW)
        below = closes[-1] / max(closes) - 1 if closes else None
        state.assets.append(AssetState(
            symbol=symbol,
            asset_class=allocation.classes.get(symbol, ""),
            state=YOUNG if decided is None else (ON if decided > 0 else OFF),
            decided_return=decided,
            current_return=current,
            below_high=below,
            last_date=last.trade_date if last else None,
            stale=last is None or (latest - last.trade_date).days > STALE_AFTER_DAYS,
        ))
    return state
