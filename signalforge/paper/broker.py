"""The paper broker: a virtual venue, and the only venue this system has.

There is no external execution path here, and the absence is structural
rather than a setting. This module imports nothing that can open a socket,
and nothing from the data layer (which may use the network). The tests in
tests/test_paper_broker.py parse the shipped package and fail if any module
on the execution path gains a network import, so a future contributor cannot
quietly add one.

A session is driven explicitly by the operator or a runner:

    open_session(day, view)  ->  submit(...)  ->  settle(day, view)
                             ->  close_session(day, view) -> Statement

Orders submitted during a session become eligible after `fill_delay_days`
further sessions. They are day orders: once eligible, an order that does not
fill expires rather than lingering to surprise someone later.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from signalforge.backtesting.execution import PRICE_PRECISION, FillModel
from signalforge.backtesting.reconcile import reconcile_fill
from signalforge.config.models import ResearchConfig, RiskConfig, validate_paper_runtime
from signalforge.core.errors import LiveTradingAttempt, ValidationError
from signalforge.core.history import PointInTimeView
from signalforge.core.interfaces import RiskAction
from signalforge.core.types import (
    CorporateAction,
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Position,
    Side,
)
from signalforge.paper.corporate_actions import apply_corporate_action
from signalforge.paper.ledger import EventType, TradeLedger, _canonical
from signalforge.paper.statements import Statement
from signalforge.risk.engine import Reservations, RiskEngine
from signalforge.risk.state import RiskState

ZERO = Decimal("0")


class PaperBroker:
    """A simulated venue. Every figure it reports is simulated money."""

    def __init__(self, research: ResearchConfig, risk: RiskConfig) -> None:
        research, risk = validate_paper_runtime(research, risk)
        self._research = research
        self._risk_config = risk
        self._fill_model = FillModel(research.execution)
        self._risk = RiskEngine(risk, research.execution)
        self._portfolio = Portfolio(cash=research.starting_cash)
        self._state = RiskState()
        self._ledger = TradeLedger()
        self._statements: list[Statement] = []
        self._pending: list[tuple[int, Order]] = []
        # Every order submitted this run, with its latest status. The backtest
        # engine has always accumulated this; the broker recorded order events
        # in the ledger and kept only unfilled orders in `_pending`, so a
        # submitted, rejected or expired order could not be recovered as an
        # `Order`. The dashboard's risk section reads `result.orders`, which
        # meant a paper run rendered with an order count of zero beside a
        # non-zero fill count.
        self._orders: list[Order] = []
        self._reservations = Reservations()
        self._session_index = 0
        self._session_day: date | None = None
        self._session_view: PointInTimeView | None = None
        self._previous_marks: dict[str, Decimal] = {}
        self._order_sequence = 0

    # ------------------------------------------------------------------
    # safety
    # ------------------------------------------------------------------
    @property
    def is_paper(self) -> bool:
        """Always True. A property with no setter, so it cannot be flipped."""
        return True

    @staticmethod
    def _refuse_external_execution(detail: str) -> None:
        """Tripwire. Nothing in this package calls it; that is the point.

        If someone later wires an external venue in and routes through here,
        it raises instead of executing.
        """
        raise LiveTradingAttempt(
            f"SIGNALFORGE is paper only and has no external execution path: {detail}"
        )

    # ------------------------------------------------------------------
    # accessors
    # ------------------------------------------------------------------
    @property
    def ledger(self) -> TradeLedger:
        return self._ledger

    @property
    def statements(self) -> tuple[Statement, ...]:
        return tuple(self._statements)

    @property
    def risk_events(self):
        return tuple(self._risk.events)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def starting_cash(self) -> Decimal:
        """The simulated capital the book opened with."""
        return self._research.starting_cash

    @property
    def orders(self) -> tuple[Order, ...]:
        """Every order submitted, each once, carrying its latest status."""
        return tuple(self._orders)

    def portfolio(self) -> Portfolio:
        return self._portfolio

    # ------------------------------------------------------------------
    # session lifecycle
    # ------------------------------------------------------------------
    def open_session(
        self,
        day: date,
        view: PointInTimeView,
        corporate_actions: Sequence[CorporateAction] | None = None,
    ) -> None:
        self._session_index += 1
        self._session_day = day
        self._session_view = view
        self._apply_corporate_actions(day, corporate_actions or ())
        marks = view.marks()
        opening_equity = self._value(self._previous_marks or marks)
        self._state.open_day(day, opening_equity)
        self._state.mark_equity(day, self._value(marks))
        self._ledger.append(
            EventType.SESSION_OPENED,
            day,
            {
                "session_index": self._session_index,
                "opening_equity": opening_equity,
                "cash": self._portfolio.cash,
                "simulated": True,
            },
        )

    def _apply_corporate_actions(
        self, day: date, actions: Sequence[CorporateAction]
    ) -> None:
        """Book every action effective today, before opening equity is read.

        The booking uses the prior RAW close. Every action also rescales that
        stored close (a dividend by the cash paid), so opening equity, peak
        equity, the daily-loss breaker and the fill model's previous_close see
        a continuous series instead of a crash or a fake gain. A split also
        expires the day orders sized in pre-split shares; the strategy
        re-signals later. An action already evaluated is skipped whole, so a
        replay cannot rescale a second time.
        """
        for action in actions:
            if action.effective_date != day:
                continue
            if action.action_id in self._portfolio._applied_action_ids:
                continue
            stored = self._previous_marks.get(action.symbol)
            prior_close = stored if stored is not None else self._prior_bar_close(
                action.symbol, day
            )
            if prior_close is None:
                if self._portfolio.quantity_of(action.symbol) != 0:
                    raise ValidationError(
                        f"{action.action_id}: {action.symbol} is held but no close before "
                        f"{day} is known, so the action cannot be booked"
                    )
                self._portfolio._applied_action_ids.add(action.action_id)
                booking = None
                new_mark = None
            else:
                if action.action_type == "split":
                    new_mark = prior_close * action.denominator / action.numerator
                else:
                    new_mark = prior_close - action.cash_per_share
                    if new_mark <= ZERO:
                        raise ValidationError(
                            f"{action.action_id}: dividend {action.cash_per_share} is not "
                            f"below the prior close {prior_close}"
                        )
                new_mark = new_mark.quantize(PRICE_PRECISION, rounding=ROUND_HALF_UP)
                booking = apply_corporate_action(self._portfolio, action, prior_close)
            if stored is not None:
                self._previous_marks[action.symbol] = new_mark
            payload = {
                "action_id": action.action_id,
                "symbol": action.symbol,
                "kind": action.kind,
                "shares_before": booking.shares_before if booking else 0,
                "shares_after": booking.shares_after if booking else 0,
                "cash_delta": booking.cash_delta if booking else ZERO,
                "cash_in_lieu": booking.cash_in_lieu if booking else ZERO,
                "residual": booking.residual if booking else ZERO,
                "realized_pnl_delta": booking.realized_pnl_delta if booking else ZERO,
                "simulated": True,
            }
            if action.action_type == "dividend":
                payload["note"] = "pay date unknown; booked on ex-date"
            self._ledger.append(EventType.CORPORATE_ACTION, day, payload)
            if action.action_type == "split":
                self._expire_pending(day, action)

    def _prior_bar_close(self, symbol: str, day: date) -> Decimal | None:
        """The raw close of the last bar strictly before `day`, from the view.

        A restarted broker has no stored marks. The view is point-in-time, so
        this reads nothing from the future.
        """
        earlier = [b for b in self._session_view.bars(symbol) if b.trade_date < day]
        return earlier[-1].close if earlier else None

    def _expire_pending(self, day: date, action: CorporateAction) -> None:
        still_pending: list[tuple[int, Order]] = []
        for submitted_index, order in self._pending:
            if order.symbol != action.symbol:
                still_pending.append((submitted_index, order))
                continue
            self._release_reservation(order, self._session_view)
            self._ledger.append(
                EventType.ORDER_EXPIRED,
                day,
                {
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "quantity": order.quantity,
                    "detail": f"{action.kind} {action.action_id} effective today; "
                    "order was sized in pre-action shares, expired",
                },
            )
            self._log_order(order, OrderStatus.EXPIRED)
        self._pending = still_pending

    def settle(self, day: date, view: PointInTimeView) -> list:
        """Fill every eligible pending order against today's bar.

        The queue is split BEFORE any fill is attempted, so an order is off
        the queue whether or not its fill succeeds. Previously an exception
        from `apply_fill` skipped the queue reassignment entirely, leaving
        already-applied orders queued; they re-filled on the next session
        under a new fill_id, which the portfolio's replay protection does not
        recognise, turning one 100-share order into a 200-share position.

        Safe to call more than once for the same day: eligible orders are
        gone from the queue by the time anything can fail.
        """
        delay = self._research.execution.fill_delay_days
        due: list[tuple[int, Order]] = []
        still_pending: list[tuple[int, Order]] = []
        for submitted_index, order in self._pending:
            if self._session_index - submitted_index < delay:
                still_pending.append((submitted_index, order))
            else:
                due.append((submitted_index, order))
        self._pending = still_pending

        settled = []
        for _, order in due:
            self._release_reservation(order, view)
            bar = view.latest(order.symbol)
            fill = None
            if bar is not None and bar.trade_date == day:
                fill = self._fill_model.fill(
                    order, bar, previous_close=self._previous_marks.get(order.symbol)
                )
                fill, event = reconcile_fill(
                    fill, order, self._portfolio, self._fill_model, day
                )
                if event is not None:
                    self._risk.events.append(event)
            if fill is None:
                self._ledger.append(
                    EventType.ORDER_EXPIRED,
                    day,
                    {
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "quantity": order.quantity,
                        "detail": "no fill available on the eligible bar; day order expired",
                    },
                )
                self._log_order(order, OrderStatus.EXPIRED)
                continue
            realized_before = self._portfolio.realized_pnl
            held_before = self._portfolio.quantity_of(order.symbol)
            # apply_fill returns False for a fill_id already applied. The
            # ledger must not claim a fill the book refused, or the audit
            # trail over-reports trades relative to the portfolio.
            if not self._portfolio.apply_fill(fill):
                self._ledger.append(
                    EventType.ORDER_EXPIRED,
                    day,
                    {
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "quantity": order.quantity,
                        "detail": "fill already applied; replay refused by the book",
                    },
                )
                self._log_order(order, OrderStatus.EXPIRED)
                continue
            self._record_round_trip(order, held_before, realized_before)
            self._log_order(
                order,
                OrderStatus.FILLED
                if fill.quantity == order.quantity
                else OrderStatus.PARTIALLY_FILLED,
            )
            settled.append(fill)
            self._ledger.append(
                EventType.FILL,
                day,
                {
                    "order_id": order.order_id,
                    "fill_id": fill.fill_id,
                    "symbol": fill.symbol,
                    "side": fill.side,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "fees": fill.fees,
                    "slippage_cost": fill.slippage_cost,
                    "reason": order.reason,
                    "strategy_id": order.strategy_id,
                    "simulated": True,
                },
            )
        self._state.mark_equity(day, self._value(view.marks()))
        return settled

    def close_session(self, day: date, view: PointInTimeView) -> Statement:
        marks = view.marks()
        equity = self._value(marks)
        self._state.mark_equity(day, equity)
        statement = Statement(
            statement_date=day,
            equity=equity,
            cash=self._portfolio.cash,
            gross_exposure=self._gross(marks),
            realized_pnl=self._portfolio.realized_pnl,
            unrealized_pnl=self._unrealized(marks),
            total_fees=self._portfolio.total_fees,
            total_slippage_cost=self._portfolio.total_slippage_cost,
            drawdown=self._state.drawdown_fraction,
            peak_equity=self._state.peak_equity,
            positions={
                symbol: Position(
                    symbol=position.symbol,
                    quantity=position.quantity,
                    cost_basis=position.cost_basis,
                    realized_pnl=position.realized_pnl,
                )
                for symbol, position in self._portfolio.positions.items()
            },
            fills=tuple(f for f in self._portfolio.fills if f.fill_date == day),
            simulated=True,
        )
        self._statements.append(statement)
        self._ledger.append(
            EventType.SESSION_CLOSED,
            day,
            {
                "equity": equity,
                "cash": self._portfolio.cash,
                "drawdown": self._state.drawdown_fraction,
                "simulated": True,
            },
        )
        self._previous_marks = marks
        self._session_day = None
        self._session_view = None
        return statement

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    def create_order(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        signal_date: date,
        strategy_id: str,
        reason: str,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
    ) -> Order:
        self._order_sequence += 1
        return Order(
            order_id=f"paper:{strategy_id}:{symbol}:{signal_date.isoformat()}:{self._order_sequence}",
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            signal_date=signal_date,
            strategy_id=strategy_id,
            reason=reason,
            limit_price=limit_price,
        )

    def submit(self, order: Order) -> Order:
        """Put an order through the risk engine. Returns its resulting status."""
        if self._session_day is None or self._session_view is None:
            raise ValidationError(
                "no session is open; call open_session(day, view) before submitting. "
                "Submitting without a session would mean pricing an order against "
                "market data nobody chose."
            )
        self._ledger.append(
            EventType.ORDER_SUBMITTED,
            self._session_day,
            {
                "order_id": order.order_id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "order_type": order.order_type,
                "reason": order.reason,
                "strategy_id": order.strategy_id,
            },
        )
        decision = self._risk.evaluate(
            order, self._portfolio, self._session_view, self._state, self._reservations
        )

        if decision.action is RiskAction.REJECT:
            self._ledger.append(
                EventType.ORDER_REJECTED,
                self._session_day,
                {
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "rule": decision.rule,
                    "reason": decision.reason,
                },
            )
            self._log_order(order, OrderStatus.REJECTED)
            return order.with_status(OrderStatus.REJECTED)

        accepted = order
        if decision.approved_quantity != order.quantity:
            accepted = Order(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=decision.approved_quantity,
                order_type=order.order_type,
                signal_date=order.signal_date,
                strategy_id=order.strategy_id,
                reason=order.reason,
                limit_price=order.limit_price,
            )
            self._ledger.append(
                EventType.ORDER_REDUCED,
                self._session_day,
                {
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "requested": order.quantity,
                    "approved": decision.approved_quantity,
                    "rule": decision.rule,
                    "reason": decision.reason,
                },
            )

        accepted = accepted.with_status(OrderStatus.ACCEPTED)
        self._log_order(accepted, OrderStatus.ACCEPTED)
        self._state.record_submission(accepted, requested_quantity=order.quantity)
        self._pending.append((self._session_index, accepted))
        self._reserve(accepted)
        self._ledger.append(
            EventType.ORDER_ACCEPTED,
            self._session_day,
            {
                "order_id": accepted.order_id,
                "symbol": accepted.symbol,
                "side": accepted.side,
                "quantity": accepted.quantity,
                "eligible_after_sessions": self._research.execution.fill_delay_days,
                "reason": accepted.reason,
            },
        )
        return accepted

    # ------------------------------------------------------------------
    # operator controls
    # ------------------------------------------------------------------
    def pause(self, reason: str, day: date | None = None) -> None:
        self._state.paused = True
        self._state.pause_reason = reason
        self._ledger.append(
            EventType.PAUSED, day or self._session_day or date.today(), {"reason": reason}
        )

    def resume(self, day: date | None = None) -> None:
        self._state.paused = False
        self._state.pause_reason = ""
        self._ledger.append(
            EventType.RESUMED, day or self._session_day or date.today(), {}
        )

    @property
    def paused(self) -> bool:
        return self._state.paused

    @property
    def pause_reason(self) -> str:
        return self._state.pause_reason

    def state_digest(self) -> str:
        """A hash of everything that decides the next session's behaviour.

        Two brokers with the same digest trade identically from here on. The
        runner stores it per session and compares it to a replay, so a
        restored book is proven rather than assumed. Order reason text is
        excluded: it is commentary, not state.
        """
        portfolio = self._portfolio
        state = self._state
        pending = [
            [index, o.order_id, o.symbol, o.side, o.quantity, o.order_type, o.limit_price]
            for index, o in self._pending
        ]
        relevant = set(portfolio.positions) | {o.symbol for _, o in self._pending}
        body = {
            "cash": portfolio.cash,
            "closed_realized_pnl": portfolio.closed_realized_pnl,
            "total_fees": portfolio.total_fees,
            "total_slippage_cost": portfolio.total_slippage_cost,
            "positions": sorted(
                [p.symbol, p.quantity, p.cost_basis, p.realized_pnl]
                for p in portfolio.positions.values()
            ),
            "applied_fill_ids": sorted(portfolio._applied_fill_ids),
            "applied_action_ids": sorted(portfolio._applied_action_ids),
            "pending": pending,
            "marks": {s: m for s, m in self._previous_marks.items() if s in relevant},
            "peak_equity": state.peak_equity,
            "current_equity": state.current_equity,
            "paused": state.paused,
            "pause_reason": state.pause_reason,
            "last_closed": sorted(
                [t.strategy_id, t.symbol, t.quantity, t.pnl]
                for t in state._last_closed.values()
            ),
            "last_order_quantity": sorted(
                [strategy_id, symbol, quantity]
                for (strategy_id, symbol), quantity in state._last_order_quantity.items()
            ),
            "session_index": self._session_index,
            "order_sequence": self._order_sequence,
        }
        encoded = json.dumps(_canonical(body), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def status(self) -> dict:
        marks = self._session_view.marks() if self._session_view else self._previous_marks
        equity = self._value(marks) if marks else self._portfolio.cash
        return {
            "is_paper": True,
            "simulated": True,
            "paused": self._state.paused,
            "pause_reason": self._state.pause_reason,
            "session_day": self._session_day,
            "session_index": self._session_index,
            "equity": equity,
            "cash": self._portfolio.cash,
            "gross_exposure": self._gross(marks) if marks else ZERO,
            "position_count": len(self._portfolio.positions),
            "pending_count": self.pending_count,
            "daily_loss": self._state.daily_loss_fraction,
            "drawdown": self._state.drawdown_fraction,
            "peak_equity": self._state.peak_equity,
            "ledger_intact": self._ledger.verify(),
            "breakers": {
                "daily_loss_tripped": self._state.daily_loss_fraction
                >= self._risk_config.max_daily_loss,
                "drawdown_tripped": self._state.drawdown_fraction
                >= self._risk_config.max_portfolio_drawdown,
                "paused": self._state.paused,
            },
        }

    def reconcile(self, expected_positions: dict[str, int], expected_cash: Decimal):
        return self._risk.reconcile(self._portfolio, expected_positions, expected_cash)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _log_order(self, order: Order, status: OrderStatus) -> None:
        """Record or update one order's status. Keyed by order id, so an
        order that is accepted and later filled appears once, not twice."""
        stamped = order.with_status(status)
        for index, existing in enumerate(self._orders):
            if existing.order_id == order.order_id:
                self._orders[index] = stamped
                return
        self._orders.append(stamped)

    def _reserve(self, order: Order) -> None:
        """Commit an accepted buy against the caps until it fills or expires.

        Without this, every cap was computed against FILLED positions only,
        so two strategies could each be granted the full single-name cap in
        one session and the log would record both as approved.
        """
        if order.side is not Side.BUY or self._session_view is None:
            return
        bar = self._session_view.latest(order.symbol)
        mark = bar.close if bar is not None else ZERO
        self._reservations.add(
            order.order_id, order.symbol, order.quantity, mark * order.quantity
        )

    def _release_reservation(self, order: Order, view: PointInTimeView) -> None:
        """Release at the SAME mark the reservation was taken at.

        Releasing at a different mark strands the difference in
        Reservations.cash with nothing to clear it, which understates buying
        power permanently. The mark is therefore read from the reservation
        itself rather than recomputed from today's price.
        """
        if order.side is not Side.BUY:
            return
        self._reservations.release(order.order_id)

    def _record_round_trip(
        self, order: Order, held_before: int, realized_before: Decimal
    ) -> None:
        if order.side is not Side.SELL:
            return
        if self._portfolio.quantity_of(order.symbol) == 0 and held_before > 0:
            self._state.record_closed_trade(
                symbol=order.symbol,
                strategy_id=order.strategy_id,
                quantity=held_before,
                pnl=self._portfolio.realized_pnl - realized_before,
            )

    def _held_marks(self, marks: dict[str, Decimal]) -> dict[str, Decimal]:
        return {s: marks[s] for s in self._portfolio.positions if s in marks}

    def _value(self, marks: dict[str, Decimal]) -> Decimal:
        return self._portfolio.equity(self._held_marks(marks))

    def _gross(self, marks: dict[str, Decimal]) -> Decimal:
        return self._portfolio.gross_exposure(self._held_marks(marks))

    def _unrealized(self, marks: dict[str, Decimal]) -> Decimal:
        return self._portfolio.unrealized_pnl(self._held_marks(marks))
