"""Shared contracts between layers.

These types are the seams of the system. A strategy produces Signals and knows
nothing about cash, fills, or limits. The risk engine turns proposed Orders
into RiskDecisions and knows nothing about why a strategy wanted them. The
broker turns accepted Orders into Fills. Keeping the seams narrow is what lets
the risk engine be genuinely independent of strategy logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from signalforge.core.errors import ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.types import Fill, Order, Portfolio

ZERO = Decimal("0")


class SignalDirection(str, Enum):
    """What a strategy wants. Long-only: there is no SHORT member."""

    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's intent for one symbol on one date.

    `target_weight` is the fraction of equity the strategy would like in this
    symbol, before any risk control sees it. A strategy expresses desire here;
    it never sizes in dollars and never decides whether it is allowed.
    """

    symbol: str
    direction: SignalDirection
    as_of: date
    strategy_id: str
    reason: str
    target_weight: Decimal = ZERO
    strength: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValidationError("signal requires a symbol")
        if not self.reason:
            raise ValidationError("signal requires a reason; it becomes the order's reason")
        if not self.strategy_id:
            raise ValidationError("signal requires a strategy_id")
        if self.target_weight < ZERO:
            raise ValidationError(
                f"target_weight must not be negative ({self.target_weight}); "
                "this book is long-only"
            )
        if self.target_weight > Decimal("1"):
            raise ValidationError(f"target_weight must not exceed 1, got {self.target_weight}")


@runtime_checkable
class Strategy(Protocol):
    """A deterministic signal generator.

    `generate_signals` receives a PointInTimeView, which cannot reach data
    after `as_of`. That is the only market data a strategy ever gets.
    """

    strategy_id: str
    warmup_days: int

    def parameters(self) -> dict[str, object]:
        """Every tunable, for the experiment registry. Must be exhaustive."""
        ...

    def generate_signals(self, as_of: date, view: PointInTimeView) -> list[Signal]:
        ...


class RiskAction(str, Enum):
    ACCEPT = "ACCEPT"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    FLAG = "FLAG"
    """An observation that changes no order but must not be silent, such as a
    holding being valued on a price that has stopped updating. Kept distinct
    from REJECT so a warning never inflates the rejected-order count."""


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The risk engine's verdict on one proposed order.

    REDUCE carries an `approved_quantity` below the request. REJECT carries
    zero. Every decision carries a rule name so a rejection can be explained
    in the dashboard without guesswork.
    """

    action: RiskAction
    rule: str
    reason: str
    approved_quantity: int = 0

    def __post_init__(self) -> None:
        if not self.rule:
            raise ValidationError("risk decision requires the rule that produced it")
        if self.action is RiskAction.REJECT and self.approved_quantity != 0:
            raise ValidationError("a rejected order cannot carry an approved quantity")
        if self.action is not RiskAction.REJECT and self.approved_quantity <= 0:
            raise ValidationError(
                f"{self.action.value} must approve a positive quantity, "
                f"got {self.approved_quantity}"
            )

    @property
    def allows_trade(self) -> bool:
        return self.action is not RiskAction.REJECT


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """An audit record of a risk control firing. Append-only."""

    event_date: date
    rule: str
    action: RiskAction
    symbol: str | None
    reason: str
    strategy_id: str | None = None
    requested_quantity: int | None = None
    approved_quantity: int | None = None


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One end-of-day valuation of the book."""

    valuation_date: date
    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal
    position_count: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    drawdown: Decimal
    peak_equity: Decimal


@dataclass(slots=True)
class BacktestResult:
    """Everything a run produced. The only input to metrics and reports.

    `data_limitations` is not decoration. Every known weakness of the run's
    input data travels with the result, so a number can never be quoted in a
    report without the caveat that belongs to it.
    """

    strategy_id: str
    run_id: str
    start_date: date
    end_date: date
    starting_cash: Decimal
    equity_curve: list[EquityPoint] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    risk_events: list[RiskEvent] = field(default_factory=list)
    parameters: dict[str, object] = field(default_factory=dict)
    data_limitations: list[str] = field(default_factory=list)
    symbols: tuple[str, ...] = ()
    bar_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now())
    engine_version: str = "0.1.0"
    dataset_fingerprint: str = ""

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1].equity if self.equity_curve else self.starting_cash

    @property
    def trade_count(self) -> int:
        return len(self.fills)

    @property
    def rejected_order_count(self) -> int:
        return sum(1 for e in self.risk_events if e.action is RiskAction.REJECT)


@runtime_checkable
class Broker(Protocol):
    """A venue that can accept orders. The only implementation is PaperBroker.

    There is deliberately no `cancel_all`, no `account_credentials`, and no
    network method in this protocol. The shape of the interface is part of the
    safety argument.
    """

    def submit(self, order: Order) -> Order:
        ...

    def portfolio(self) -> Portfolio:
        ...

    @property
    def is_paper(self) -> bool:
        ...
