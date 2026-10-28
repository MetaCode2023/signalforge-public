"""Fill-time reconciliation, shared by the backtest engine and the paper broker.

An order is sized against the book as it stood when the signal fired and
fills a bar later. In between, a gap can push a buy past the available cash,
and another order can already have closed the position a sell meant to exit.
Unhandled, either raises AccountingError from inside the caller's loop, which
on real data is routine rather than exotic.

This lives in one module because it is safety-critical and was previously
implemented only in the backtest engine. The paper broker went without it,
and the result was an order that could not be funded staying queued forever
and re-filling every session under a new fill_id, defeating replay
protection. Two copies of this logic would drift the same way again.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from signalforge.backtesting.execution import FillModel
from signalforge.core.interfaces import RiskAction, RiskEvent
from signalforge.core.types import Fill, Order, Portfolio, Side


def reconcile_fill(
    fill: Fill | None,
    order: Order,
    portfolio: Portfolio,
    fill_model: FillModel,
    day: date,
) -> tuple[Fill | None, RiskEvent | None]:
    """Cut a fill down to what the book can actually support today.

    Returns `(fill_or_None, event_or_None)`. The fill is reduced to what is
    possible and the reduction is reported as a RiskEvent. Nothing is
    silently swallowed, and nothing raises.
    """
    if fill is None:
        return None, None

    if fill.side is Side.BUY:
        cost = fill.gross_value + fill.fees
        if cost <= portfolio.cash:
            return fill, None
        affordable = fill_model.affordable_quantity(fill, portfolio.cash)
        resized = fill_model.resize(fill, affordable)
        return resized, RiskEvent(
            event_date=day,
            rule="cash_available_at_fill",
            action=RiskAction.REDUCE if resized else RiskAction.REJECT,
            symbol=order.symbol,
            reason=(
                f"{order.symbol} was sized for {order.quantity} shares on "
                f"{order.signal_date} but costs {cost} at the {day} fill price "
                f"{fill.price}, against cash {portfolio.cash}; reduced to "
                f"{affordable} shares"
            ),
            strategy_id=order.strategy_id,
            requested_quantity=fill.quantity,
            approved_quantity=affordable,
        )

    held = portfolio.quantity_of(order.symbol)
    if fill.quantity <= held:
        return fill, None
    resized = fill_model.resize(fill, held)
    return resized, RiskEvent(
        event_date=day,
        rule="position_closed_before_fill",
        action=RiskAction.REDUCE if resized else RiskAction.REJECT,
        symbol=order.symbol,
        reason=(
            f"{order.symbol} sell of {fill.quantity} was sized on "
            f"{order.signal_date} but only {held} shares remain at the {day} "
            "fill; another order closed the rest first"
        ),
        strategy_id=order.strategy_id,
        requested_quantity=fill.quantity,
        approved_quantity=held,
    )
