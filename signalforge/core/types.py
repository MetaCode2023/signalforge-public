"""Core domain types.

Money and prices are Decimal throughout. Share quantities are whole integers.
This is deliberate: the accounting invariant `equity == cash + marked
positions` has to hold exactly, and binary floats cannot promise that.

Slippage note: `Fill.price` is the price actually transacted and already
embeds any modelled slippage. `Fill.slippage_cost` is a reporting field that
records how much of the fill price was slippage relative to the reference
price. Cash is moved by `price` alone, so slippage is never charged twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from signalforge.core.errors import AccountingError, ValidationError

ZERO = Decimal("0")


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class QualityStatus(str, Enum):
    """Outcome of data validation for a single record."""

    OK = "OK"
    QUARANTINED = "QUARANTINED"
    SUSPECT = "SUSPECT"


def _require_int_quantity(quantity: object, label: str = "quantity") -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValidationError(f"{label} must be a whole number of shares, got {quantity!r}")
    if quantity <= 0:
        raise ValidationError(f"{label} must be positive, got {quantity}")
    return quantity


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily OHLCV observation for one symbol.

    `adj_close` is the split- and dividend-adjusted close where the provider
    supplies it, and None where it does not. A None adj_close is a real fact
    about the source, not a value to be guessed at.
    """

    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adj_close: Decimal | None = None
    source: str = "unknown"
    volume_known: bool = True
    """False when the source publishes no volume at all, as for an index
    series. This is not the same as a volume of zero: zero means nothing
    traded, unknown means nobody told us. Rules that depend on volume are
    skipped for unknown bars and the run records that they were skipped,
    rather than a number being invented to satisfy them."""

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValidationError("bar requires a symbol")
        if not isinstance(self.trade_date, date):
            raise ValidationError(f"trade_date must be a date, got {self.trade_date!r}")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise ValidationError(f"{name} must be Decimal, got {type(value).__name__}")
            if value <= ZERO:
                raise ValidationError(
                    f"{self.symbol} {self.trade_date}: {name} must be positive, got {value}"
                )
        if self.adj_close is not None and self.adj_close <= ZERO:
            raise ValidationError(
                f"{self.symbol} {self.trade_date}: adj_close must be positive, got {self.adj_close}"
            )
        if isinstance(self.volume, bool) or not isinstance(self.volume, int):
            raise ValidationError(f"volume must be an integer, got {self.volume!r}")
        if self.volume < 0:
            raise ValidationError(
                f"{self.symbol} {self.trade_date}: volume must not be negative, got {self.volume}"
            )
        if not self.volume_known and self.volume != 0:
            raise ValidationError(
                f"{self.symbol} {self.trade_date}: volume_known is False but volume is "
                f"{self.volume}; a bar cannot both lack a volume and report one"
            )
        if self.high < self.low:
            raise ValidationError(
                f"{self.symbol} {self.trade_date}: high {self.high} < low {self.low}"
            )
        for name in ("open", "close"):
            value = getattr(self, name)
            if not (self.low <= value <= self.high):
                raise ValidationError(
                    f"{self.symbol} {self.trade_date}: {name} {value} outside "
                    f"low/high {self.low}/{self.high}"
                )

    @property
    def adjustment_ratio(self) -> Decimal | None:
        """adj_close / close, i.e. the cumulative corporate-action factor."""
        if self.adj_close is None:
            return None
        return self.adj_close / self.close


@dataclass(frozen=True, slots=True)
class Order:
    """A proposed order. Creating one does not mean it will be accepted.

    `signal_date` is the bar whose close produced the signal. Execution
    happens strictly after it; the backtest engine enforces that.
    """

    order_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    signal_date: date
    strategy_id: str
    reason: str
    limit_price: Decimal | None = None
    status: OrderStatus = OrderStatus.NEW

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValidationError("order requires an order_id")
        if not self.symbol:
            raise ValidationError("order requires a symbol")
        if not self.strategy_id:
            raise ValidationError("order requires a strategy_id for attribution")
        if not self.reason:
            raise ValidationError("order requires a reason for the trade ledger")
        _require_int_quantity(self.quantity)
        if not isinstance(self.signal_date, date):
            raise ValidationError(f"signal_date must be a date, got {self.signal_date!r}")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ValidationError("limit order requires a limit_price")
            if self.limit_price <= ZERO:
                raise ValidationError(f"limit_price must be positive, got {self.limit_price}")
        elif self.limit_price is not None:
            raise ValidationError("market order must not carry a limit_price")

    def with_status(self, status: OrderStatus) -> Order:
        """Return a copy in a new status. Orders are immutable by design."""
        return Order(
            order_id=self.order_id,
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            order_type=self.order_type,
            signal_date=self.signal_date,
            strategy_id=self.strategy_id,
            reason=self.reason,
            limit_price=self.limit_price,
            status=status,
        )


@dataclass(frozen=True, slots=True)
class Fill:
    """An executed transaction. Immutable; the ledger is append-only.

    `fill_id` defaults to `order_id`, which is correct for a single complete
    fill. Partial fills of one order must each carry a distinct fill_id.
    """

    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    fees: Decimal
    slippage_cost: Decimal
    fill_date: date
    fill_id: str = ""
    reference_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValidationError("fill requires an order_id")
        _require_int_quantity(self.quantity)
        if not isinstance(self.price, Decimal) or self.price <= ZERO:
            raise ValidationError(f"fill price must be a positive Decimal, got {self.price!r}")
        if self.fees < ZERO:
            raise ValidationError(f"fees must not be negative, got {self.fees}")
        if not self.fill_id:
            object.__setattr__(self, "fill_id", self.order_id)

    @property
    def gross_value(self) -> Decimal:
        return self.price * self.quantity

    @property
    def cash_delta(self) -> Decimal:
        """Signed effect on cash. Negative for a buy, positive for a sell."""
        if self.side is Side.BUY:
            return -(self.gross_value + self.fees)
        return self.gross_value - self.fees


@dataclass(slots=True)
class Position:
    """A long-only position tracked at average cost.

    Buy-side fees are capitalised into the cost basis. Sell-side fees are
    charged against realized P&L. Short positions are impossible here: a sell
    beyond the held quantity raises rather than going negative.
    """

    symbol: str
    quantity: int = 0
    cost_basis: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    @property
    def average_cost(self) -> Decimal:
        if self.quantity == 0:
            return ZERO
        return self.cost_basis / self.quantity

    def market_value(self, mark: Decimal) -> Decimal:
        return mark * self.quantity

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        return self.market_value(mark) - self.cost_basis

    def apply_buy(self, quantity: int, price: Decimal, fees: Decimal) -> None:
        _require_int_quantity(quantity)
        self.quantity += quantity
        self.cost_basis += price * quantity + fees

    def apply_sell(self, quantity: int, price: Decimal, fees: Decimal) -> None:
        _require_int_quantity(quantity)
        if quantity > self.quantity:
            raise AccountingError(
                f"{self.symbol}: cannot sell {quantity}, only {self.quantity} held "
                "(this book is long-only; shorting is not permitted)"
            )
        cost_removed = self.average_cost * quantity
        self.realized_pnl += price * quantity - cost_removed - fees
        self.quantity -= quantity
        if self.quantity == 0:
            self.cost_basis = ZERO
        else:
            self.cost_basis -= cost_removed


@dataclass(slots=True)
class Portfolio:
    """Cash, positions, and the append-only fill ledger.

    Every mutation validates before it mutates, so a refused operation leaves
    the book byte-for-byte unchanged.
    """

    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    closed_realized_pnl: Decimal = ZERO
    """Realized P&L from positions that have fully closed and left the book."""
    total_fees: Decimal = ZERO
    total_slippage_cost: Decimal = ZERO
    _applied_fill_ids: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cash, Decimal):
            raise ValidationError(f"cash must be Decimal, got {type(self.cash).__name__}")
        if self.cash < ZERO:
            raise ValidationError(f"starting cash must not be negative, got {self.cash}")
        # Derive replay protection from any fills handed in at construction.
        # Without this, a portfolio built with existing fills would happily
        # re-apply one of them and double-count it.
        for fill in self.fills:
            self._applied_fill_ids.add(fill.fill_id)

    def quantity_of(self, symbol: str) -> int:
        position = self.positions.get(symbol)
        return position.quantity if position else 0

    def apply_fill(self, fill: Fill) -> bool:
        """Apply a fill to the book.

        Returns True if applied, False if this fill_id was already applied
        (replay protection). Raises AccountingError without mutating anything
        if the fill would break an invariant.
        """
        if fill.fill_id in self._applied_fill_ids:
            return False

        if fill.side is Side.BUY:
            cost = fill.gross_value + fill.fees
            if cost > self.cash:
                raise AccountingError(
                    f"{fill.symbol}: buy of {fill.quantity} @ {fill.price} costs {cost}, "
                    f"available cash {self.cash} (no margin borrowing)"
                )
            position = self.positions.get(fill.symbol) or Position(symbol=fill.symbol)
            position.apply_buy(fill.quantity, fill.price, fill.fees)
            self.positions[fill.symbol] = position
            self.cash -= cost
        else:
            held = self.quantity_of(fill.symbol)
            if fill.quantity > held:
                raise AccountingError(
                    f"{fill.symbol}: cannot sell {fill.quantity}, only {held} held "
                    "(this book is long-only; shorting is not permitted)"
                )
            position = self.positions[fill.symbol]
            position.apply_sell(fill.quantity, fill.price, fill.fees)
            self.cash += fill.gross_value - fill.fees
            if position.quantity == 0:
                self.closed_realized_pnl += position.realized_pnl
                del self.positions[fill.symbol]

        self.total_fees += fill.fees
        self.total_slippage_cost += fill.slippage_cost
        self.fills.append(fill)
        self._applied_fill_ids.add(fill.fill_id)
        return True

    @property
    def realized_pnl(self) -> Decimal:
        """Realized P&L across closed AND partially closed positions.

        Accumulating only on full close hid the realized leg of a partially
        closed position, so the equity curve's realized/unrealized split did
        not reconcile to equity. Reachable with the default config, because a
        sell is partially filled at the participation cap.
        """
        open_realized = sum(
            (p.realized_pnl for p in self.positions.values()), ZERO
        )
        return self.closed_realized_pnl + open_realized

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        """Cash plus the marked value of every open position.

        A position with no mark raises. Valuing an unmarkable position at zero
        would silently understate a drawdown, which is the exact failure a risk
        system must never have.
        """
        total = self.cash
        for symbol, position in self.positions.items():
            mark = marks.get(symbol)
            if mark is None:
                raise AccountingError(
                    f"no mark available for open position {symbol} "
                    f"({position.quantity} shares); refusing to value the book"
                )
            if mark <= ZERO:
                raise AccountingError(f"mark for {symbol} must be positive, got {mark}")
            total += position.market_value(mark)
        return total

    def unrealized_pnl(self, marks: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for symbol, position in self.positions.items():
            mark = marks.get(symbol)
            if mark is None:
                raise AccountingError(f"no mark available for open position {symbol}")
            total += position.unrealized_pnl(mark)
        return total

    def gross_exposure(self, marks: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for symbol, position in self.positions.items():
            mark = marks.get(symbol)
            if mark is None:
                raise AccountingError(f"no mark available for open position {symbol}")
            total += position.market_value(mark)
        return total
