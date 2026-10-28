"""The backtest event loop.

Per trading day, in this order:

  1. Open the session, valuing the book at the *previous* close.
  2. Fill orders that became due today, using today's bar.
  3. Mark the book at today's close and update peak / drawdown state.
  4. Ask the strategy for signals, given a view truncated at today.
  5. Size each signal, put it through the risk engine, and queue what
     survives for a later bar.
  6. Record one equity point.

Step 4 happening after step 3 and before step 5 is the whole design: a signal
is generated from information available at today's close and can only be acted
on later. A strategy never gets a chance to trade on the bar it is reading.

Unfilled orders expire at the end of their due bar. They are day orders, so a
limit that was never reached does not quietly sit in the book and fill later at
a price the strategy never asked for.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import ROUND_FLOOR, Decimal

from signalforge.backtesting.execution import FillModel
from signalforge.backtesting.reconcile import reconcile_fill
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig, validate_paper_runtime
from signalforge.core.errors import ConfigError, LookAheadError, ValidationError
from signalforge.core.history import PriceHistory, summarise_symbols
from signalforge.core.interfaces import (
    BacktestResult,
    EquityPoint,
    RiskAction,
    RiskEvent,
    Signal,
    SignalDirection,
)
from signalforge.core.types import (
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Side,
)
from signalforge.risk.engine import Reservations, RiskEngine
from signalforge.risk.state import RiskState

ZERO = Decimal("0")
ENGINE_VERSION = "0.1.0"


class BacktestEngine:
    def __init__(self, research: ResearchConfig, risk: RiskConfig) -> None:
        research, risk = validate_paper_runtime(research, risk)
        self.research = research
        self.risk_config = risk
        # Committed-but-unfilled cash left over at the end of the last run.
        # Must be zero: anything else is buying power silently withheld.
        self._last_residual_reservation: Decimal = ZERO

    def run_and_report_residual(self, strategy, history, **kwargs) -> Decimal:
        """Run, then report leftover committed cash. Used by the tests."""
        self.run(strategy, history, **kwargs)
        return self._last_residual_reservation

    def run(
        self,
        strategy,
        history: PriceHistory,
        *,
        start: date | None = None,
        end: date | None = None,
        run_id: str | None = None,
        data_limitations: list[str] | None = None,
        execution: dict | None = None,
        correlation_clusters: dict[str, tuple[str, ...]] | None = None,
    ) -> BacktestResult:
        research = self.research
        if execution:
            research = research.model_copy(
                update={"execution": ExecutionConfig.model_validate(
                    {**research.execution.model_dump(), **execution}
                )}
            )

        days = [
            d
            for d in history.trading_days
            if (start is None or d >= start) and (end is None or d <= end)
        ]
        if not days:
            raise ConfigError(
                f"no trading days in the requested window "
                f"{start or 'start'}..{end or 'end'}; the history spans "
                f"{history.trading_days[0] if history.trading_days else 'nothing'} to "
                f"{history.trading_days[-1] if history.trading_days else 'nothing'}"
            )

        fill_model = FillModel(research.execution)
        risk_engine = RiskEngine(self.risk_config, research.execution)
        if correlation_clusters:
            risk_engine.set_correlation_clusters(correlation_clusters)

        portfolio = Portfolio(cash=research.starting_cash)
        state = RiskState()
        pending: dict[date, list[Order]] = {}
        reservations = Reservations()
        all_orders: list[Order] = []
        order_sequence = 0

        limitations = list(data_limitations or [])
        limitations.extend(self._detect_limitations(history, days))

        previous_marks: dict[str, Decimal] = {}
        equity_curve: list[EquityPoint] = []
        flagged_stale: set[str] = set()
        known_symbols = set(history.symbols)

        for day in days:
            view = history.view(day)
            marks = view.marks()

            opening_equity = self._value(portfolio, previous_marks or marks)
            state.open_day(day, opening_equity)

            # --- 2. fills due today ---------------------------------------
            for order in pending.pop(day, []):
                self._release(reservations, order)
                bar = view.latest(order.symbol)
                filled = False
                if bar is not None and bar.trade_date == day:
                    previous_close = previous_marks.get(order.symbol)
                    fill = fill_model.fill(order, bar, previous_close=previous_close)
                    fill, reconcile_event = reconcile_fill(
                        fill, order, portfolio, fill_model, day
                    )
                    if reconcile_event is not None:
                        risk_engine.events.append(reconcile_event)
                    if fill is not None:
                        realized_before = portfolio.realized_pnl
                        held_before = portfolio.quantity_of(order.symbol)
                        portfolio.apply_fill(fill)
                        filled = True
                        self._record_closed_trade(
                            portfolio, state, order, fill, held_before, realized_before
                        )
                        status = (
                            OrderStatus.FILLED
                            if fill.quantity == order.quantity
                            else OrderStatus.PARTIALLY_FILLED
                        )
                        self._update_status(all_orders, order, status)
                if not filled:
                    self._update_status(all_orders, order, OrderStatus.EXPIRED)

            # --- 3. mark to today's close ----------------------------------
            equity = self._value(portfolio, marks)
            state.mark_equity(day, equity)
            self._flag_stale_valuations(
                portfolio, view, day, risk_engine, flagged_stale, limitations
            )

            # --- 4. signals, generated from today's close ------------------
            try:
                signals = strategy.generate_signals(day, view)
            except LookAheadError:
                # Never re-wrapped. A look-ahead attempt is the one strategy
                # failure whose TYPE is the finding, and callers (and
                # `tests/test_point_in_time.py`) assert on it. Burying it
                # inside a ValidationError would hide the single most
                # important failure this engine can detect behind a generic
                # one. The message already names the symbol and both dates.
                raise
            except Exception as exc:
                # Deliberate: the run dies rather than skipping the session.
                # Catching this and carrying on would produce a complete
                # equity curve and a full set of metrics with one day of the
                # strategy's decisions silently missing, which is a partial
                # backtest presented as a finished one. What was wrong before
                # was only that the exception carried no context: a 38-fold
                # walk-forward dying inside a fold reported bare
                # "ZeroDivisionError" and nothing about where.
                raise ValidationError(
                    f"strategy {getattr(strategy, 'strategy_id', type(strategy).__name__)!r} "
                    f"raised {type(exc).__name__} while generating signals for "
                    f"{day.isoformat()} (run window {days[0].isoformat()}.."
                    f"{days[-1].isoformat()}): {exc}. The run is abandoned rather "
                    "than continued without that session's decisions."
                ) from exc

            # --- 5. size, risk-check, queue --------------------------------
            fill_day = self._fill_day(history, day, research.execution.fill_delay_days)
            for signal in signals or []:
                if signal.symbol not in known_symbols:
                    # A symbol the history has never held is a programming
                    # error -- a typo, a renamed ticker, a universe that does
                    # not match the ingested data -- not a market condition.
                    # It used to be dropped here in silence: no order, no
                    # rejection, no risk event, so a strategy that did
                    # nothing was indistinguishable from one with no
                    # opportunities. A symbol that IS in the history but has
                    # no bar yet is the ordinary late-listing case and stays
                    # a quiet no-op below.
                    raise ValidationError(
                        f"strategy {signal.strategy_id!r} emitted a "
                        f"{signal.direction.value} signal for {signal.symbol!r} on "
                        f"{day.isoformat()}, which this history has never held. "
                        f"It covers {summarise_symbols(sorted(known_symbols))}."
                    )
                order_sequence += 1
                order = self._order_from_signal(
                    signal, portfolio, view, equity, day, order_sequence
                )
                if order is None:
                    continue
                decision = risk_engine.evaluate(
                    order, portfolio, view, state, reservations
                )
                if not decision.allows_trade:
                    all_orders.append(order.with_status(OrderStatus.REJECTED))
                    continue
                approved = order
                if decision.approved_quantity != order.quantity:
                    approved = Order(
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
                accepted = approved.with_status(OrderStatus.ACCEPTED)
                all_orders.append(accepted)
                # The strategy's own requested size, not the reduced one: a
                # risk reduction is not the strategy escalating.
                state.record_submission(accepted, requested_quantity=order.quantity)
                if fill_day is None:
                    self._update_status(all_orders, accepted, OrderStatus.EXPIRED)
                else:
                    pending.setdefault(fill_day, []).append(accepted)
                    self._reserve(reservations, accepted, marks)

            # --- 6. record the day -----------------------------------------
            equity_curve.append(
                EquityPoint(
                    valuation_date=day,
                    equity=equity,
                    cash=portfolio.cash,
                    gross_exposure=self._gross(portfolio, marks),
                    position_count=len(portfolio.positions),
                    realized_pnl=portfolio.realized_pnl,
                    unrealized_pnl=self._unrealized(portfolio, marks),
                    drawdown=state.drawdown_fraction,
                    peak_equity=state.peak_equity,
                )
            )
            previous_marks = marks

        # Orders still queued beyond the window never had a bar to fill on.
        for queued in pending.values():
            for order in queued:
                self._update_status(all_orders, order, OrderStatus.EXPIRED)
                self._release(reservations, order)
        self._last_residual_reservation = reservations.cash

        symbols = tuple(sorted({o.symbol for o in all_orders})) or history.symbols
        return BacktestResult(
            strategy_id=getattr(strategy, "strategy_id", "unknown"),
            run_id=run_id or f"{getattr(strategy, 'strategy_id', 'run')}:{days[0]}:{days[-1]}",
            start_date=days[0],
            end_date=days[-1],
            starting_cash=research.starting_cash,
            equity_curve=equity_curve,
            fills=list(portfolio.fills),
            orders=all_orders,
            risk_events=list(risk_engine.events),
            parameters=dict(strategy.parameters()),
            data_limitations=limitations,
            symbols=symbols,
            bar_count=sum(len(history.bars_for(s)) for s in history.symbols),
            engine_version=ENGINE_VERSION,
            dataset_fingerprint=self.fingerprint(history, days[0], days[-1]),
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _order_from_signal(
        self,
        signal: Signal,
        portfolio: Portfolio,
        view,
        equity: Decimal,
        day: date,
        sequence: int,
    ) -> Order | None:
        """Turn intent into a concrete order, or into nothing.

        A strategy re-asserts its desired state every bar, so most signals on
        most days are already satisfied. Those become no-ops here rather than
        orders the risk engine would only reject, which keeps the rejection
        log a record of real refusals instead of routine noise.
        """
        if signal.as_of != day:
            raise ValidationError(
                f"strategy {signal.strategy_id} returned a signal dated "
                f"{signal.as_of} while being asked about {day}"
            )
        held = portfolio.quantity_of(signal.symbol)

        if signal.direction is SignalDirection.HOLD:
            return None

        if signal.direction is SignalDirection.EXIT_LONG:
            if held <= 0:
                return None
            return Order(
                order_id=self._order_id(signal, day, sequence),
                symbol=signal.symbol,
                side=Side.SELL,
                quantity=held,
                order_type=OrderType.MARKET,
                signal_date=day,
                strategy_id=signal.strategy_id,
                reason=signal.reason,
            )

        # ENTER_LONG
        if held > 0 and not self.risk_config.allow_pyramiding:
            return None
        bar = view.latest(signal.symbol)
        if bar is None or bar.close <= ZERO:
            return None
        target_value = equity * signal.target_weight
        desired = int((target_value / bar.close).to_integral_value(rounding=ROUND_FLOOR))
        quantity = desired - held
        if quantity <= 0:
            return None
        return Order(
            order_id=self._order_id(signal, day, sequence),
            symbol=signal.symbol,
            side=Side.BUY,
            quantity=quantity,
            order_type=OrderType.MARKET,
            signal_date=day,
            strategy_id=signal.strategy_id,
            reason=signal.reason,
        )

    @staticmethod
    def _order_id(signal: Signal, day: date, sequence: int) -> str:
        return f"{signal.strategy_id}:{signal.symbol}:{day.isoformat()}:{sequence}"

    @staticmethod
    def _fill_day(history: PriceHistory, day: date, delay: int) -> date | None:
        current: date | None = day
        for _ in range(delay):
            if current is None:
                return None
            current = history.next_trading_day(current)
        return current

    @staticmethod
    def _update_status(orders: list[Order], order: Order, status: OrderStatus) -> None:
        for index, existing in enumerate(orders):
            if existing.order_id == order.order_id:
                orders[index] = existing.with_status(status)
                return
        orders.append(order.with_status(status))

    @staticmethod
    def _record_closed_trade(
        portfolio: Portfolio,
        state: RiskState,
        order: Order,
        fill,
        held_before: int,
        realized_before: Decimal,
    ) -> None:
        """Feed a completed round trip back to the risk engine's memory.

        Martingale detection needs to know how the last trade in a symbol went,
        and only the engine knows when a position actually closed.
        """
        if order.side is not Side.SELL:
            return
        if portfolio.quantity_of(order.symbol) == 0 and held_before > 0:
            state.record_closed_trade(
                symbol=order.symbol,
                strategy_id=order.strategy_id,
                quantity=held_before,
                pnl=portfolio.realized_pnl - realized_before,
            )

    @staticmethod
    def _reserve(reservations: Reservations, order: Order, marks: dict[str, Decimal]) -> None:
        """Commit an accepted buy against the caps until it fills or expires."""
        if order.side is not Side.BUY:
            return
        mark = marks.get(order.symbol, ZERO)
        reservations.add(
            order.order_id, order.symbol, order.quantity, mark * order.quantity
        )

    @staticmethod
    def _release(reservations: Reservations, order: Order) -> None:
        """Release exactly what this order committed.

        No mark is recomputed here. Re-deriving one from a different day's
        close is what stranded phantom committed cash permanently.
        """
        if order.side is not Side.BUY:
            return
        reservations.release(order.order_id)

    def _flag_stale_valuations(
        self,
        portfolio: Portfolio,
        view,
        day: date,
        risk_engine: RiskEngine,
        already_flagged: set[str],
        limitations: list[str],
    ) -> None:
        """Say so when an open position is being valued on a dead price.

        `marks()` returns the last known close forever, so a holding whose
        symbol stops printing is carried at its final price indefinitely and
        equity is overstated from that point on. The engine does not invent a
        liquidation, because inventing a trade is worse than carrying a stale
        mark. It raises a FLAG event instead, once per symbol, so the
        unreliable figure is visible rather than silent.
        """
        limit = self.risk_config.stale_data_max_age_days
        for symbol in portfolio.positions:
            if symbol in already_flagged:
                continue
            age = view.staleness_days(symbol)
            if age is None or age <= limit:
                continue
            already_flagged.add(symbol)
            bar = view.latest(symbol)
            last_date = bar.trade_date.isoformat() if bar else "never"
            risk_engine.events.append(
                RiskEvent(
                    event_date=day,
                    rule="stale_position_valuation",
                    action=RiskAction.FLAG,
                    symbol=symbol,
                    reason=(
                        f"{symbol} is held but its last bar is {last_date}, {age} days "
                        f"before {day} (limit {limit}). The position is still valued at "
                        "that price because inventing a liquidation would be inventing a "
                        "trade, so equity from here on is NOT reliable for this symbol."
                    ),
                )
            )
            limitations.append(
                f"STALE VALUATION: {symbol} was held while its price stopped updating "
                f"on {last_date}. Equity after that date carries a dead mark and is "
                "overstated or understated by an unknown amount."
            )

    @staticmethod
    def _held_marks(portfolio: Portfolio, marks: dict[str, Decimal]) -> dict[str, Decimal]:
        return {s: marks[s] for s in portfolio.positions if s in marks}

    def _value(self, portfolio: Portfolio, marks: dict[str, Decimal]) -> Decimal:
        return portfolio.equity(self._held_marks(portfolio, marks))

    def _gross(self, portfolio: Portfolio, marks: dict[str, Decimal]) -> Decimal:
        return portfolio.gross_exposure(self._held_marks(portfolio, marks))

    def _unrealized(self, portfolio: Portfolio, marks: dict[str, Decimal]) -> Decimal:
        return portfolio.unrealized_pnl(self._held_marks(portfolio, marks))

    @staticmethod
    def _detect_limitations(history: PriceHistory, days: list[date]) -> list[str]:
        """Record what is structurally weak about this run's data.

        These notes travel with the result so a number cannot be quoted in a
        report without the caveat that belongs to it.
        """
        notes: list[str] = [
            "No corporate action is ever applied to an open position: a split "
            "does not change the share count held, and a dividend is not credited "
            "as cash. Returns are only adjusted to the extent the source's "
            "adjusted close reflects them. A holding carried through a split "
            "would therefore be mis-valued."
        ]
        bars = history.all_bars()
        if bars and all(b.open == b.high == b.low == b.close for b in bars):
            notes.append(
                "Close-only series: every bar has open == high == low == close, so "
                "there is no intraday range. Gap and limit-fill modelling degrades "
                "to close-to-close and understates real execution uncertainty."
            )
        unknown_volume = sorted({b.symbol for b in bars if not b.volume_known})
        if unknown_volume:
            notes.append(
                "No volume published for "
                + ", ".join(unknown_volume)
                + ". The liquidity breaker and the participation cap were SKIPPED for "
                "these instruments, not passed: no volume was invented to satisfy "
                "them. Fill sizes for them are therefore unconstrained by traded "
                "size, which overstates how much could really have been executed."
            )
        if bars and all(b.adj_close is None for b in bars):
            notes.append(
                "No adjusted close published for any bar: dividends are not "
                "reflected, so total return is understated."
            )
        if len(history.symbols) == 1:
            notes.append(
                f"Single-instrument run ({history.symbols[0]}): cross-sectional "
                "effects and diversification cannot be assessed, and the result is "
                "one sample path, not a universe."
            )
        if len(days) < 252:
            notes.append(
                f"Only {len(days)} trading days in the window; below the 252-session "
                "floor for annualised statistics."
            )
        return notes

    @staticmethod
    def fingerprint(history: PriceHistory, start: date, end: date) -> str:
        """Deterministic hash of exactly the bars a run consumed.

        Two runs over the same data must fingerprint identically, and any
        change to a single price must change it. This is the dataset version
        recorded in the experiment registry.
        """
        digest = hashlib.sha256()
        for symbol in history.symbols:
            for bar in history.bars_for(symbol):
                if not (start <= bar.trade_date <= end):
                    continue
                # Prices are formatted to a fixed scale rather than
                # interpolated as str(Decimal), which preserves trailing
                # zeros: the same prices loaded via two paths (a provider
                # parse vs a DECIMAL(18,6) warehouse read) fingerprinted
                # differently. The fingerprint is part of the hold-out key,
                # so a changed load path bought a fresh look at the test
                # window.
                adj = "none" if bar.adj_close is None else f"{bar.adj_close:.6f}"
                digest.update(
                    f"{bar.symbol}|{bar.trade_date.isoformat()}|{bar.open:.6f}|"
                    f"{bar.high:.6f}|{bar.low:.6f}|{bar.close:.6f}|{adj}|"
                    f"{bar.volume}|{bar.volume_known}|{bar.source}\n".encode()
                )
        return digest.hexdigest()
