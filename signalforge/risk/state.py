"""Mutable risk state carried across a trading session.

This is the engine's memory: what equity the day opened at, where the peak
has been, what has already been submitted today, and how the last closed
trade in each symbol went. Martingale detection and the circuit breakers are
both functions of this state, which is why it lives in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from signalforge.core.types import Order, Side

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    symbol: str
    strategy_id: str
    quantity: int
    pnl: Decimal

    @property
    def was_loss(self) -> bool:
        return self.pnl < ZERO


@dataclass(slots=True)
class RiskState:
    """Session state. Reset per day via `open_day`, except peak and history."""

    day: date | None = None
    day_start_equity: Decimal = ZERO
    current_equity: Decimal = ZERO
    peak_equity: Decimal = ZERO
    paused: bool = False
    pause_reason: str = ""
    orders_today: list[Order] = field(default_factory=list)
    _submission_signatures: set[tuple] = field(default_factory=set, repr=False)
    _last_closed: dict[tuple[str, str], ClosedTrade] = field(default_factory=dict, repr=False)
    _last_order_quantity: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)

    def open_day(self, day: date, equity: Decimal) -> None:
        """Begin a session. Resets the daily counters, keeps the running peak."""
        self.day = day
        self.day_start_equity = equity
        self.current_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        self.orders_today = []
        self._submission_signatures = set()

    def mark_equity(self, day: date, equity: Decimal) -> None:
        """Update the current valuation and the running peak."""
        if self.day is None:
            self.open_day(day, equity)
            return
        self.current_equity = equity
        self.peak_equity = max(self.peak_equity, equity)

    @property
    def daily_loss_fraction(self) -> Decimal:
        """Loss since the session opened, as a positive fraction. 0 if up."""
        if self.day_start_equity <= ZERO:
            return ZERO
        change = (self.day_start_equity - self.current_equity) / self.day_start_equity
        return change if change > ZERO else ZERO

    @property
    def drawdown_fraction(self) -> Decimal:
        """Peak-to-current drawdown as a positive fraction. 0 at a new high."""
        if self.peak_equity <= ZERO:
            return ZERO
        change = (self.peak_equity - self.current_equity) / self.peak_equity
        return change if change > ZERO else ZERO

    @staticmethod
    def signature(order: Order) -> tuple:
        """Identity of an order's *intent*, deliberately excluding order_id.

        Two orders with different ids but the same symbol, side, size, signal
        date and strategy are the same intent submitted twice. Keying on
        order_id would make the duplicate guard trivially bypassable.
        """
        return (
            order.symbol,
            order.side.value,
            order.quantity,
            order.signal_date,
            order.strategy_id,
            order.order_type.value,
            order.limit_price,
        )

    def is_duplicate(self, order: Order) -> bool:
        return self.signature(order) in self._submission_signatures

    def record_submission(
        self, order: Order, requested_quantity: int | None = None
    ) -> None:
        """Record a submitted order.

        `requested_quantity` is what the strategy ASKED for, before any risk
        reduction. The doubling rule compares against intent, not against
        what risk allowed: recording the approved quantity meant one hard
        reduction pinned the strategy's ceiling for the rest of the run, and
        the rejection log then blamed "martingale" for risk's own decision.
        """
        self.orders_today.append(order)
        self._submission_signatures.add(self.signature(order))
        if order.side is Side.BUY:
            key = (order.strategy_id, order.symbol)
            self._last_order_quantity[key] = (
                requested_quantity if requested_quantity is not None else order.quantity
            )

    def orders_today_for(self, symbol: str) -> int:
        return sum(1 for o in self.orders_today if o.symbol == symbol)

    def record_closed_trade(
        self, *, symbol: str, strategy_id: str, quantity: int, pnl: Decimal
    ) -> None:
        """Record a completed round trip, and end that campaign.

        Closing the position clears the carried order size: a later, larger
        entry is a fresh decision, not an escalation of a position that no
        longer exists. The after-a-loss rule still applies, and it is the one
        that actually guards against chasing a loss.
        """
        self._last_closed[(strategy_id, symbol)] = ClosedTrade(
            symbol=symbol, strategy_id=strategy_id, quantity=quantity, pnl=pnl
        )
        self._last_order_quantity.pop((strategy_id, symbol), None)

    def last_closed_trade(self, strategy_id: str, symbol: str) -> ClosedTrade | None:
        return self._last_closed.get((strategy_id, symbol))

    def last_order_quantity(self, strategy_id: str, symbol: str) -> int | None:
        return self._last_order_quantity.get((strategy_id, symbol))
