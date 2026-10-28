"""Martingale and loss-chasing detection.

A martingale is the single fastest way a long-only research system turns a
drawdown into a wipeout: size up after a loss, average down into a decline,
repeat. These checks are deliberately strict and default to blocking. They are
also independent of strategy logic: all they see is the order, the book and
the recent price path.

Each function returns a rejection reason string, or None if the order is clear.
"""

from __future__ import annotations

from decimal import Decimal

from signalforge.config.models import RiskConfig
from signalforge.core.history import PointInTimeView
from signalforge.core.types import Order, Portfolio

ZERO = Decimal("0")

# A decline this deep over the lookback counts as sustained. Deliberately
# conservative: in a long-only book, adding into a 10% downtrend is the
# behaviour that compounds a loss.
DECLINE_LOOKBACK_SESSIONS = 60
DECLINE_THRESHOLD = Decimal("-0.10")


def check_averaging_down(
    order: Order, portfolio: Portfolio, price: Decimal, config: RiskConfig
) -> str | None:
    """Block buying more of a position that is already underwater."""
    if config.allow_averaging_down:
        return None
    position = portfolio.positions.get(order.symbol)
    if position is None or position.quantity == 0:
        return None
    if price < position.average_cost:
        return (
            f"{order.symbol} is held at an average cost of {position.average_cost} "
            f"and trades at {price}; buying more averages down into a loss. "
            "Set allow_averaging_down only for an explicitly approved experiment."
        )
    return None


def check_pyramiding(order: Order, portfolio: Portfolio, config: RiskConfig) -> str | None:
    """Block adding to any existing position unless pyramiding is enabled."""
    if config.allow_pyramiding:
        return None
    held = portfolio.quantity_of(order.symbol)
    if held > 0:
        return (
            f"{order.symbol} already holds {held} shares; adding to an open "
            "position is pyramiding, which is disabled by default."
        )
    return None


def check_size_increase_after_loss(order: Order, state) -> str | None:
    """Block sizing up in a symbol whose last closed trade lost money.

    This is the core martingale pattern. Same size after a loss is permitted;
    a larger size is not.
    """
    last = state.last_closed_trade(order.strategy_id, order.symbol)
    if last is None or not last.was_loss:
        return None
    if order.quantity > last.quantity:
        return (
            f"last closed trade in {order.symbol} lost {abs(last.pnl)} on "
            f"{last.quantity} shares; this order is for {order.quantity}, a size "
            "increase after a loss (martingale)."
        )
    return None


def check_order_doubling(order: Order, state) -> str | None:
    """Block an order that doubles the previous order size in the same symbol."""
    previous = state.last_order_quantity(order.strategy_id, order.symbol)
    if previous is None or previous <= 0:
        return None
    if order.quantity >= previous * 2:
        return (
            f"previous {order.symbol} order was {previous} shares; this one is "
            f"{order.quantity}, at least a doubling (martingale)."
        )
    return None


def check_sustained_decline(order: Order, view: PointInTimeView) -> str | None:
    """Block increasing exposure to a symbol in a sustained downtrend."""
    closes = view.adjusted_closes(order.symbol, lookback=DECLINE_LOOKBACK_SESSIONS + 1)
    if len(closes) < DECLINE_LOOKBACK_SESSIONS + 1:
        return None
    first, last = closes[0], closes[-1]
    if first <= ZERO:
        return None
    trailing_return = (last / first) - Decimal(1)
    if trailing_return <= DECLINE_THRESHOLD:
        return (
            f"{order.symbol} has returned {trailing_return:.1%} over the last "
            f"{DECLINE_LOOKBACK_SESSIONS} sessions; increasing exposure during a "
            "sustained decline is blocked."
        )
    return None
