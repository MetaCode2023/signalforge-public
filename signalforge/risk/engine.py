"""The risk engine.

Strategies propose orders; this engine decides whether they happen. It is
independent of strategy logic by construction: `evaluate` receives an order,
a portfolio and a point-in-time market view, and there is no hook, callback or
back-reference a strategy could use to influence the verdict.

Decision pipeline, in order:
  1. Hard blocks that refuse the order outright (breakers, data integrity,
     duplicates, rate limits, short selling, martingale patterns).
  2. Quantity caps, where the tightest cap wins and is named in the decision.

Exits are treated asymmetrically throughout: a rule that would block a
risk-reducing sell is a trap, so only the rules that must apply to sells do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from signalforge.config.models import ExecutionConfig, RiskConfig
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import RiskAction, RiskDecision, RiskEvent
from signalforge.core.types import Order, Portfolio, Side
from signalforge.risk import breakers, limits, martingale
from signalforge.risk.state import RiskState

ZERO = Decimal("0")

__all__ = ["RiskEngine", "RiskState", "ReconciliationReport", "Reservations"]


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    ok: bool
    breaks: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Reservations:
    """Commitments already made by orders that have not filled yet.

    Without this, every cap is computed against *filled* positions only, so a
    strategy can submit four orders on one bar that are each inside the limit
    and together four times it. The cap then fails silently, which is worse
    than having no cap, because the log still says every order was approved.

    Entries are keyed by ORDER ID and release exactly what they recorded.
    An earlier version took a symbol, a quantity and a recomputed cost at
    release time; because the reserve and release marks were read from
    different days, a `fill_delay_days >= 2` run stranded the difference
    permanently (9,060 of phantom committed cash on a 95,457 book, measured),
    understating buying power until every buy was rejected for want of cash.
    Keying by order id removes the arithmetic entirely, so there is nothing
    left to drift.
    """

    _entries: dict[str, tuple[str, int, Decimal]] = field(default_factory=dict)

    def add(self, order_id: str, symbol: str, quantity: int, cost: Decimal) -> None:
        self._entries[order_id] = (symbol, quantity, cost)

    def release(self, order_id: str) -> None:
        """Release the commitment for `order_id`. Unknown ids are a no-op."""
        self._entries.pop(order_id, None)

    @property
    def cash(self) -> Decimal:
        return sum((cost for _, _, cost in self._entries.values()), ZERO)

    def quantity_for(self, symbol: str) -> int:
        return sum(q for s, q, _ in self._entries.values() if s == symbol)

    @property
    def quantities(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for symbol, quantity, _ in self._entries.values():
            out[symbol] = out.get(symbol, 0) + quantity
        return out

    @property
    def symbols(self) -> set[str]:
        return {s for s, q in self.quantities.items() if q > 0}


class RiskEngine:
    """Independent gatekeeper. Holds no strategy references, ever."""

    def __init__(self, risk_config: RiskConfig, execution_config: ExecutionConfig) -> None:
        self.config = risk_config
        self.execution = execution_config
        self.events: list[RiskEvent] = []
        self._clusters: dict[str, tuple[str, ...]] = {}

    def set_correlation_clusters(self, clusters: dict[str, tuple[str, ...]]) -> None:
        """Declare groups of symbols that share an underlying risk.

        Supplied rather than inferred: a correlation estimated on the same
        data a strategy was fitted to is not independent evidence.
        """
        self._clusters = {name: tuple(symbols) for name, symbols in clusters.items()}

    def _cluster_for(self, symbol: str) -> tuple[str, ...] | None:
        for members in self._clusters.values():
            if symbol in members:
                return members
        return None

    def evaluate(
        self,
        order: Order,
        portfolio: Portfolio,
        view: PointInTimeView,
        state: RiskState,
        reservations: Reservations | None = None,
    ) -> RiskDecision:
        decision = self._decide(order, portfolio, view, state, reservations or Reservations())
        if decision.action is not RiskAction.ACCEPT:
            self.events.append(
                RiskEvent(
                    event_date=order.signal_date,
                    rule=decision.rule,
                    action=decision.action,
                    symbol=order.symbol,
                    reason=decision.reason,
                    strategy_id=order.strategy_id,
                    requested_quantity=order.quantity,
                    approved_quantity=decision.approved_quantity,
                )
            )
        return decision

    def _decide(
        self,
        order: Order,
        portfolio: Portfolio,
        view: PointInTimeView,
        state: RiskState,
        reservations: Reservations,
    ) -> RiskDecision:
        is_buy = order.side is Side.BUY

        # --- rules that apply to every order -----------------------------
        trip = breakers.check_price_available(order.symbol, view)
        if trip:
            return _reject(*trip)

        if state.is_duplicate(order):
            return _reject(
                "duplicate_order",
                f"an identical {order.side.value} of {order.quantity} {order.symbol} "
                f"from {order.strategy_id} was already submitted on {order.signal_date}",
            )

        # --- sell side: only the rules that must apply ---------------------
        if not is_buy:
            held = portfolio.quantity_of(order.symbol)
            if held <= 0:
                return _reject(
                    "short_selling_blocked",
                    f"nothing held in {order.symbol}; selling would open a short "
                    "position, which this book does not permit",
                )
            if order.quantity > held:
                return RiskDecision(
                    action=RiskAction.REDUCE,
                    rule="short_selling_blocked",
                    reason=(
                        f"requested {order.quantity} but only {held} {order.symbol} "
                        "held; reduced to the held quantity"
                    ),
                    approved_quantity=held,
                )
            return _accept(order.quantity)

        # --- buy side: the full gauntlet ----------------------------------
        for trip in (
            breakers.check_manual_pause(state),
            breakers.check_drawdown(state, self.config),
            breakers.check_daily_loss(state, self.config),
            breakers.check_staleness(order.symbol, view, self.config),
            breakers.check_liquidity(order.symbol, view),
        ):
            if trip:
                return _reject(*trip)

        if state.orders_today_for(order.symbol) >= self.config.max_orders_per_symbol_per_day:
            return _reject(
                "max_orders_per_symbol_per_day",
                f"{order.symbol} already has {state.orders_today_for(order.symbol)} "
                f"orders today, at the limit of "
                f"{self.config.max_orders_per_symbol_per_day}",
            )
        if len(state.orders_today) >= self.config.max_orders_per_day:
            return _reject(
                "max_orders_per_day",
                f"{len(state.orders_today)} orders already submitted today, at the "
                f"limit of {self.config.max_orders_per_day}",
            )

        bar = view.latest(order.symbol)
        price = bar.close
        marks = view.marks()
        equity = portfolio.equity({s: marks[s] for s in portfolio.positions if s in marks}) \
            if portfolio.positions else portfolio.cash
        gross = portfolio.gross_exposure(
            {s: marks[s] for s in portfolio.positions if s in marks}
        ) if portfolio.positions else ZERO

        held = portfolio.quantity_of(order.symbol)
        reserved = reservations.quantity_for(order.symbol)
        committed_symbols = set(portfolio.positions) | reservations.symbols
        if (
            held == 0
            and reserved == 0
            and len(committed_symbols) >= self.config.max_positions
        ):
            return _reject(
                "max_positions",
                f"{len(committed_symbols)} positions are already open or committed by "
                f"an unfilled order, at the limit of {self.config.max_positions}; "
                f"{order.symbol} would be a new one",
            )

        reason = martingale.check_averaging_down(order, portfolio, price, self.config)
        if reason:
            return _reject("martingale_averaging_down", reason)
        reason = martingale.check_pyramiding(order, portfolio, self.config)
        if reason:
            return _reject("pyramiding_blocked", reason)
        reason = martingale.check_size_increase_after_loss(order, state)
        if reason:
            return _reject("martingale_size_increase_after_loss", reason)
        reason = martingale.check_order_doubling(order, state)
        if reason:
            return _reject("martingale_order_doubling", reason)
        reason = martingale.check_sustained_decline(order, view)
        if reason:
            return _reject("sustained_decline", reason)

        available_cash = portfolio.cash - reservations.cash
        if available_cash < price + self.execution.min_commission:
            return _reject(
                "cash_available",
                f"uncommitted cash {available_cash} (of {portfolio.cash}, with "
                f"{reservations.cash} already committed by unfilled orders) cannot fund "
                f"a single {order.symbol} share at {price} plus "
                f"{self.execution.min_commission} commission; this book has no margin",
            )

        caps: list[tuple[str, int]] = [
            (
                "single_name_exposure",
                limits.single_name_cap(
                    order.symbol, price, equity, portfolio, self.config, reserved=reserved
                ),
            ),
            (
                "gross_leverage",
                limits.gross_leverage_cap(
                    price, equity, gross + reservations.cash, self.config
                ),
            ),
            ("cash_available", limits.cash_cap(price, available_cash, self.execution)),
        ]
        if bar.volume_known:
            caps.append(
                ("liquidity_participation", limits.participation_cap(bar.volume, self.execution))
            )
        budget_cap = limits.risk_budget_cap(order.symbol, price, equity, view, self.config)
        if budget_cap is not None:
            caps.append(("risk_budget_per_trade", budget_cap))
        cluster = self._cluster_for(order.symbol)
        if cluster is not None:
            caps.append(
                (
                    "correlated_cluster_exposure",
                    limits.cluster_cap(
                        order.symbol,
                        price,
                        equity,
                        portfolio,
                        marks,
                        cluster,
                        self.config,
                        reservations=reservations.quantities,
                    ),
                )
            )

        binding_rule, allowed = min(caps, key=lambda item: item[1])
        if allowed <= 0:
            return _reject(
                binding_rule,
                f"{binding_rule} leaves no room for {order.symbol}: 0 shares "
                f"permitted at {price} against equity {equity}",
            )
        if allowed < order.quantity:
            return RiskDecision(
                action=RiskAction.REDUCE,
                rule=binding_rule,
                reason=(
                    f"{binding_rule} caps {order.symbol} at {allowed} shares; "
                    f"{order.quantity} were requested"
                ),
                approved_quantity=allowed,
            )
        return _accept(order.quantity)

    def reconcile(
        self,
        portfolio: Portfolio,
        expected_positions: dict[str, int],
        expected_cash: Decimal,
    ) -> ReconciliationReport:
        """Compare the book against an independent expectation.

        Reports every break rather than the first, because a single break is
        rarely the whole story.
        """
        breaks: list[str] = []
        if portfolio.cash != expected_cash:
            breaks.append(
                f"cash break: book holds {portfolio.cash}, expected {expected_cash}, "
                f"difference {portfolio.cash - expected_cash}"
            )
        for symbol in sorted(set(expected_positions) | set(portfolio.positions)):
            actual = portfolio.quantity_of(symbol)
            expected = expected_positions.get(symbol, 0)
            if actual != expected:
                breaks.append(
                    f"{symbol} quantity break: book holds {actual}, expected {expected}"
                )
        return ReconciliationReport(ok=not breaks, breaks=breaks)


def _reject(rule: str, reason: str) -> RiskDecision:
    return RiskDecision(action=RiskAction.REJECT, rule=rule, reason=reason, approved_quantity=0)


def _accept(quantity: int) -> RiskDecision:
    return RiskDecision(
        action=RiskAction.ACCEPT,
        rule="all_rules_passed",
        reason="order is inside every configured limit",
        approved_quantity=quantity,
    )
