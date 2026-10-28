"""The fill model: how a decision becomes a transaction.

Every assumption here is deliberately conservative, because each one is a way
a backtest can flatter itself:

* A fill is only ever produced on a bar strictly later than the signal bar.
  Asking for a fill on the signal bar raises LookAheadError rather than
  returning a plausible-looking number.
* Market orders fill at the next bar's open, not its close. Filling at the
  close of a bar you are reacting to is the classic look-ahead.
* Slippage always moves against the order. A favourable gap does not earn a
  discount: we do not model luck as a bonus.
* A limit fill is capped at the limit. Price protection is the point of one.
* Fill size is capped at a fraction of that bar's volume. Assuming unlimited
  size in a thin name is one of the most flattering errors available.
* A zero-volume bar produces no fill at all, because no trade happened.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from signalforge.config.models import ExecutionConfig
from signalforge.core.errors import LookAheadError
from signalforge.core.types import Bar, Fill, Order, OrderType, Side

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
PRICE_PRECISION = Decimal("0.000001")

# How far the open must move from the previous close before we treat it as a
# gap rather than ordinary noise. 2% on a daily bar is a real discontinuity.
GAP_THRESHOLD = Decimal("0.02")


class FillModel:
    """Turns an accepted order plus a future bar into a Fill, or into nothing."""

    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    def fill(
        self, order: Order, bar: Bar, previous_close: Decimal | None = None
    ) -> Fill | None:
        """Attempt to fill `order` on `bar`.

        Returns None when the market would not have filled it: no volume, a
        limit that was never reached, or a size the bar could not absorb.
        """
        if bar.trade_date <= order.signal_date:
            raise LookAheadError(
                f"refusing to fill order {order.order_id} on {bar.trade_date}: the "
                f"signal was generated on {order.signal_date}, so this bar is not "
                "strictly in the future. Filling here would assume knowledge the "
                "strategy did not have."
            )
        if bar.symbol != order.symbol:
            raise LookAheadError(
                f"bar is for {bar.symbol} but order is for {order.symbol}"
            )

        quantity = self._fillable_quantity(order, bar)
        if quantity <= 0:
            return None

        priced = self._fill_price(order, bar, previous_close)
        if priced is None:
            return None
        price, reference = priced

        slippage_per_share = (
            price - reference if order.side is Side.BUY else reference - price
        )
        if slippage_per_share < ZERO:
            slippage_per_share = ZERO

        return Fill(
            order_id=order.order_id,
            fill_id=f"{order.order_id}:{bar.trade_date.isoformat()}",
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            fees=self._commission(quantity),
            slippage_cost=slippage_per_share * quantity,
            fill_date=bar.trade_date,
            reference_price=reference,
        )

    def resize(self, fill: Fill, quantity: int) -> Fill | None:
        """Return the same fill at a smaller quantity, with costs recomputed.

        Used when the book turns out not to be able to support the whole
        order at fill time: a buy whose cost has risen above available cash,
        or a sell of shares another order already closed. Commission is
        recomputed from the new size rather than carried over, and slippage
        is rescaled per share, so a shrunken fill is not charged for size it
        never took.
        """
        if quantity <= 0:
            return None
        if quantity >= fill.quantity:
            return fill
        per_share_slippage = fill.slippage_cost / fill.quantity if fill.quantity else ZERO
        return Fill(
            order_id=fill.order_id,
            fill_id=fill.fill_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=quantity,
            price=fill.price,
            fees=self._commission(quantity),
            slippage_cost=per_share_slippage * quantity,
            fill_date=fill.fill_date,
            reference_price=fill.reference_price,
        )

    def affordable_quantity(self, fill: Fill, cash: Decimal) -> int:
        """Largest quantity of `fill` that `cash` can pay for, fees included.

        The same question `risk.limits.cash_cap` asks before the order is
        placed, so it goes through the same closed-form solver. Both used to
        search downward one share at a time from an upper bound that ignores
        the per-share commission: measured at 1.556 seconds for a single call
        on a 10,000,000-share penny position, and round 6's cap fix makes
        positions that size legitimately reachable.
        """
        return min(
            fill.quantity, self.config.max_affordable_quantity(fill.price, cash)
        )

    def _fillable_quantity(self, order: Order, bar: Bar) -> int:
        if not bar.volume_known:
            # The source publishes no volume, so there is nothing to cap
            # against. The run records that the participation cap was not
            # applied; inventing a volume here would make the cap a fiction
            # that still looked like a control.
            return order.quantity
        if bar.volume <= 0:
            return 0
        cap = int(
            (Decimal(bar.volume) * self.config.max_participation_of_volume).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if order.quantity <= cap:
            return order.quantity
        if not self.config.allow_partial_fills:
            return 0
        return cap

    def _fill_price(
        self, order: Order, bar: Bar, previous_close: Decimal | None
    ) -> tuple[Decimal, Decimal] | None:
        """Return (fill_price, reference_price), or None if no fill happens."""
        reference = bar.open

        if order.order_type is OrderType.LIMIT:
            limit = order.limit_price
            assert limit is not None  # Order.__post_init__ guarantees this
            if order.side is Side.BUY:
                if bar.open <= limit:
                    reference = bar.open
                elif bar.low < limit:
                    reference = limit
                else:
                    return None
            else:
                if bar.open >= limit:
                    reference = bar.open
                elif bar.high > limit:
                    reference = limit
                else:
                    return None

        rate = self._slippage_rate(order, bar, previous_close)
        if order.side is Side.BUY:
            price = reference * (ONE + rate)
        else:
            price = reference * (ONE - rate)

        if order.order_type is OrderType.LIMIT:
            # Slippage may not push a fill through its own limit.
            limit = order.limit_price
            price = min(price, limit) if order.side is Side.BUY else max(price, limit)

        price = price.quantize(PRICE_PRECISION, rounding=ROUND_HALF_UP)
        if price <= ZERO:
            return None
        return price, reference

    def _slippage_rate(
        self, order: Order, bar: Bar, previous_close: Decimal | None
    ) -> Decimal:
        rate = self.config.slippage_bps / BPS
        if previous_close is None or previous_close <= ZERO:
            return rate
        move = (bar.open - previous_close) / previous_close
        if abs(move) <= GAP_THRESHOLD:
            return rate
        adverse = move > ZERO if order.side is Side.BUY else move < ZERO
        if adverse:
            return rate * self.config.gap_slippage_multiplier
        return rate

    def _commission(self, quantity: int) -> Decimal:
        return self.config.commission(quantity)
