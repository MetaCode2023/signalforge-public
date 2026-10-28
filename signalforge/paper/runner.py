"""Drives the paper broker, session by session, and persists its progress.

`PaperBroker` is a library; something has to open each session, settle what
is due, ask the strategy for signals, and close the books. That driver is
this module, and it has two jobs beyond the obvious one:

**It refuses to pick a strategy nobody approved.** A loop that quietly
selects one is the same failure as a strategy promoting itself, moved one
layer out. The runner starts only if the warehouse records an APPROVED active
strategy, or the operator passes an explicit override with a reason, which is
then recorded in both the ledger and the warehouse.

**It resumes without replaying.** A restart that re-ran yesterday's sessions
would double-count every fill, so completed sessions are persisted and a
second run over the same range does nothing. A new runner cannot advance to
new sessions until the book/ledger is restored; the current implementation
fails closed rather than treating persisted progress as a fresh cash account.

Still PAPER ONLY. This module imports nothing that can open a socket; the
structural tests in tests/test_paper_broker.py cover it along with the rest
of the execution path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.errors import PromotionError, SignalForgeError, ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import BacktestResult, EquityPoint, SignalDirection
from signalforge.core.types import CorporateAction, OrderType, Side
from signalforge.paper.broker import PaperBroker
from signalforge.paper.ledger import EventType
from signalforge.strategies.registry import build

ZERO = Decimal("0")
SPLIT_PRICE_TOLERANCE = Decimal("0.25")
UNRECORDED_JUMP_RATIO = Decimal("1.4")
DEFAULT_BOOK_ID = "series:NASDAQCOM"

SCHEMA = """
CREATE TABLE IF NOT EXISTS active_strategy (
    seq BIGINT,
    strategy_id VARCHAR NOT NULL,
    parameters VARCHAR NOT NULL,
    approved_by VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    approved_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS paper_runs (
    run_id VARCHAR,
    strategy_id VARCHAR NOT NULL,
    approval_source VARCHAR NOT NULL,
    approval_detail VARCHAR,
    first_session DATE,
    last_session DATE,
    sessions BIGINT,
    started_at TIMESTAMP,
    book_id VARCHAR DEFAULT 'series:NASDAQCOM'
);
CREATE TABLE IF NOT EXISTS paper_sessions (
    book_id VARCHAR NOT NULL,
    session_date DATE NOT NULL,
    strategy_id VARCHAR NOT NULL,
    equity DECIMAL(18,6),
    cash DECIMAL(18,6),
    gross_exposure DECIMAL(18,6),
    position_count BIGINT,
    fills BIGINT,
    completed_at TIMESTAMP,
    PRIMARY KEY (book_id, session_date)
);
CREATE TABLE IF NOT EXISTS paper_corporate_actions (
    book_id VARCHAR NOT NULL,
    action_id VARCHAR NOT NULL,
    session_date DATE,
    kind VARCHAR,
    symbol VARCHAR,
    shares_before BIGINT,
    shares_after BIGINT,
    cash_delta DECIMAL(18,2),
    cash_in_lieu DECIMAL(18,2),
    residual DECIMAL(18,6),
    realized_pnl_delta DECIMAL(18,6),
    recorded_at TIMESTAMP,
    PRIMARY KEY (book_id, action_id)
);
"""


def _columns(con, table: str) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
        ).fetchall()
    }


def ensure_schema(con) -> None:
    """Create the paper tables, migrating a pre-book warehouse in place.

    Rows written before books existed belong to the default NASDAQCOM book.
    paper_sessions needs a new primary key, which DuckDB cannot alter, so the
    legacy table is renamed, rebuilt and copied across.
    """
    sessions = _columns(con, "paper_sessions")
    runs = _columns(con, "paper_runs")
    if sessions and "book_id" not in sessions:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("ALTER TABLE paper_sessions RENAME TO paper_sessions_legacy")
            con.execute(SCHEMA)
            con.execute(
                "INSERT INTO paper_sessions (book_id, session_date, strategy_id, equity, cash, "
                "gross_exposure, position_count, fills, completed_at) SELECT ?, session_date, "
                "strategy_id, equity, cash, gross_exposure, position_count, fills, completed_at "
                "FROM paper_sessions_legacy",
                [DEFAULT_BOOK_ID],
            )
            con.execute("DROP TABLE paper_sessions_legacy")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    if runs and "book_id" not in runs:
        con.execute("ALTER TABLE paper_runs ADD COLUMN book_id VARCHAR DEFAULT 'series:NASDAQCOM'")
    con.execute(SCHEMA)


class RunnerRefusal(SignalForgeError):
    """The runner declined to start. Always for a stated reason."""


@dataclass(frozen=True, slots=True)
class RunResult:
    strategy_id: str
    approval_source: str
    sessions: int
    first_session: date | None
    last_session: date | None
    notes: tuple[str, ...] = ()
    result: BacktestResult | None = None
    """What the run produced, in the shape the reports and dashboard read.

    `render_dashboard(paper_runs=...)` needs a `BacktestResult`, and nothing
    used to convert a paper run into one, so the dashboard's PAPER SIMULATION
    provenance class had never rendered from an actual run. It is None when no
    session was due, because an equity curve of zero points through
    `compute_metrics` renders a page saying 0.00% rather than one saying
    nothing happened.
    """


PAPER_NOTE = (
    "PAPER SIMULATION. Every figure is simulated money from a virtual book. "
    "No order reached a venue and no real capital exists on this machine."
)


def paper_result(
    broker: PaperBroker,
    *,
    strategy_id: str,
    run_id: str,
    parameters: dict,
    symbols: tuple[str, ...],
    notes: tuple[str, ...] = (),
    dataset_fingerprint: str = "",
) -> BacktestResult | None:
    """Turn a driven paper broker into the result shape everything else reads.

    The equity curve is the broker's own end-of-day statements rather than a
    recomputation, so the page cannot disagree with the statements it was
    built from. Returns None when no session was recorded.
    """
    statements = broker.statements
    if not statements:
        return None
    curve = [
        EquityPoint(
            valuation_date=statement.statement_date,
            equity=statement.equity,
            cash=statement.cash,
            gross_exposure=statement.gross_exposure,
            position_count=len(statement.positions),
            realized_pnl=statement.realized_pnl,
            unrealized_pnl=statement.unrealized_pnl,
            drawdown=statement.drawdown,
            peak_equity=statement.peak_equity,
        )
        for statement in statements
    ]
    return BacktestResult(
        strategy_id=strategy_id,
        run_id=run_id,
        start_date=curve[0].valuation_date,
        end_date=curve[-1].valuation_date,
        starting_cash=broker.starting_cash,
        equity_curve=curve,
        fills=list(broker.portfolio().fills),
        orders=list(broker.orders),
        risk_events=list(broker.risk_events),
        parameters=dict(parameters),
        data_limitations=[PAPER_NOTE, *notes],
        symbols=symbols,
        dataset_fingerprint=dataset_fingerprint,
    )


def record_active_strategy(
    warehouse, *, strategy_id: str, parameters: dict, approved_by: str, reason: str
) -> None:
    """Record the strategy approved for paper trading.

    Requires a named human and a reason, for the same reason the promotion
    gate does: an approval with nobody's name on it is not an approval.
    """
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise PromotionError(
            "an active paper strategy must name the human who approved it"
        )
    if not reason or not reason.strip():
        raise ValidationError("recording an active strategy requires a reason")
    ensure_schema(warehouse.con)
    seq = warehouse.con.execute(
        "SELECT coalesce(max(seq), 0) + 1 FROM active_strategy"
    ).fetchall()[0][0]
    warehouse.con.execute(
        "INSERT INTO active_strategy (seq, strategy_id, parameters, approved_by, "
        "reason, approved_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            seq,
            strategy_id,
            json.dumps(parameters, sort_keys=True, default=str),
            approved_by.strip(),
            reason.strip(),
            datetime.now(),
        ],
    )


class PaperRunner:
    def __init__(
        self,
        *,
        warehouse,
        research: ResearchConfig,
        risk: RiskConfig,
        override_strategy: tuple[str, dict] | None = None,
        override_reason: str = "",
        book_id: str = DEFAULT_BOOK_ID,
        corporate_actions=None,
        raw_basis: bool = False,
        corporate_action_source: str | None = None,
    ) -> None:
        self.warehouse = warehouse
        ensure_schema(self.warehouse.con)
        self.book_id = book_id
        self.corporate_actions = (
            list(corporate_actions) if corporate_actions is not None else None
        )
        self.raw_basis = raw_basis
        self.corporate_action_source = corporate_action_source
        self.research = research
        self.risk = risk
        self.override_strategy = override_strategy
        self.override_reason = override_reason
        self.broker = PaperBroker(research, risk)
        self._paused_reason = ""
        self._last_persisted_session: date | None = None

    # ------------------------------------------------------------------
    def pause(self, reason: str) -> None:
        self._paused_reason = reason
        self.broker.pause(reason)

    def resume(self) -> None:
        self._paused_reason = ""
        self.broker.resume()

    # ------------------------------------------------------------------
    def _resolve_strategy(self) -> tuple[str, dict, str, str]:
        """Return (strategy_id, params, approval_source, detail), or refuse."""
        rows = self.warehouse.con.execute(
            "SELECT strategy_id, parameters, approved_by, reason FROM active_strategy "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchall()
        if rows:
            strategy_id, params_json, approved_by, reason = rows[0]
            return (
                strategy_id,
                json.loads(params_json),
                "promotion",
                f"approved by {approved_by}: {reason}",
            )

        if self.override_strategy is None:
            raise RunnerRefusal(
                "no strategy is approved for paper trading, so the runner will not "
                "choose one. Record an approved promotion with "
                "record_active_strategy(...), or pass an explicit override_strategy "
                "with an override_reason. A loop that picks its own strategy is the "
                "same failure as a strategy promoting itself."
            )
        if not self.override_reason or not self.override_reason.strip():
            raise RunnerRefusal(
                "override_strategy was supplied without an override_reason. An "
                "unapproved strategy may run only on the record, so the reason is "
                "required and is written to the ledger and the warehouse."
            )
        strategy_id, params = self.override_strategy
        return strategy_id, dict(params), "override", self.override_reason.strip()

    # ------------------------------------------------------------------
    def last_completed_session(self, history: PriceHistory) -> date | None:
        rows = self.warehouse.con.execute(
            "SELECT max(session_date) FROM paper_sessions WHERE book_id = ?",
            [self.book_id],
        ).fetchall()
        return rows[0][0] if rows and rows[0][0] is not None else None

    def _due_sessions(self, history: PriceHistory, through: date) -> list[date]:
        done = self.last_completed_session(history)
        return [
            d
            for d in history.trading_days
            if d <= through and (done is None or d > done)
        ]

    # ------------------------------------------------------------------
    def run(self, history: PriceHistory, *, through: date) -> RunResult:
        strategy_id, params, source, detail = self._resolve_strategy()
        strategy = build(strategy_id, **self._coerce(params))

        days = self._due_sessions(history, through)
        completed = self.last_completed_session(history)
        if days and completed is not None and completed != self._last_persisted_session:
            raise RunnerRefusal(
                "persisted sessions exist but the broker book was not restored; "
                "refusing to continue from a fresh cash balance. Keep the current "
                "runner or implement verified book/ledger restoration before restart."
            )
        actions = self._actions_for(history)
        if self.raw_basis or self.corporate_actions is not None:
            self._refuse_late_actions(history, actions)
            self._refuse_non_trading_day_actions(history, days, through, actions)
            self._refuse_inconsistent_splits(history, days, actions)
            self._refuse_unrecorded_jumps(history, days, actions)
        notes: list[str] = []
        if source == "override":
            notes.append(
                f"UNAPPROVED STRATEGY running on an explicit override: {detail}"
            )
            self.broker.ledger.append(
                EventType.STRATEGY_OVERRIDE,
                days[0] if days else through,
                {
                    "override": True,
                    "strategy_id": strategy_id,
                    "reason": detail,
                    "detail": "running an UNAPPROVED strategy on operator override",
                },
            )
        if self._paused_reason:
            notes.append(f"paused: {self._paused_reason}")

        book = "" if self.book_id == DEFAULT_BOOK_ID else f"{self.book_id}:"
        run_id = f"paper:{book}{strategy_id}:{through.isoformat()}"
        for day in days:
            view = history.view(day)
            ledger_start = len(self.broker.ledger)
            self.broker.open_session(
                day,
                view,
                [a for a in actions if a.effective_date == day] or None,
            )
            self.broker.settle(day, view)
            self._submit_signals(strategy, day, view)
            statement = self.broker.close_session(day, view)
            self._record_session(day, strategy_id, statement, ledger_start)

        self.warehouse.con.execute(
            "INSERT INTO paper_runs (run_id, strategy_id, approval_source, "
            "approval_detail, first_session, last_session, sessions, started_at, book_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                strategy_id,
                source,
                detail,
                days[0] if days else None,
                days[-1] if days else None,
                len(days),
                datetime.now(),
                self.book_id,
            ],
        )
        if source == "promotion":
            notes.append(f"Strategy {strategy_id} is approved for paper trading: {detail}")
        return RunResult(
            strategy_id=strategy_id,
            approval_source=source,
            sessions=len(days),
            first_session=days[0] if days else None,
            last_session=days[-1] if days else None,
            notes=tuple(notes),
            result=paper_result(
                self.broker,
                strategy_id=strategy_id,
                run_id=run_id,
                parameters=params,
                symbols=history.symbols,
                notes=tuple(notes),
                dataset_fingerprint=BacktestEngine.fingerprint(
                    history, days[0], days[-1]
                ) if days else "",
            ) if days else None,
        )

    # ------------------------------------------------------------------
    def _submit_signals(self, strategy, day: date, view) -> None:
        signals = strategy.generate_signals(day, view) or []
        portfolio = self.broker.portfolio()
        marks = view.marks()
        held_marks = {s: marks[s] for s in portfolio.positions if s in marks}
        equity = portfolio.equity(held_marks)
        for signal in signals:
            if signal.direction is SignalDirection.HOLD:
                continue
            held = portfolio.quantity_of(signal.symbol)
            if signal.direction is SignalDirection.EXIT_LONG:
                if held <= 0:
                    continue
                self.broker.submit(
                    self.broker.create_order(
                        symbol=signal.symbol,
                        side=Side.SELL,
                        quantity=held,
                        signal_date=day,
                        strategy_id=signal.strategy_id,
                        reason=signal.reason,
                        order_type=OrderType.MARKET,
                    )
                )
                continue
            # ENTER_LONG. The engine's no-pyramiding no-op applies here too:
            # a re-asserted entry on a held position is not an order.
            if held > 0 and not self.risk.allow_pyramiding:
                continue
            bar = view.latest(signal.symbol)
            if bar is None or bar.close <= ZERO:
                continue
            desired = int((equity * signal.target_weight) / bar.close)
            quantity = desired - held
            if quantity <= 0:
                continue
            self.broker.submit(
                self.broker.create_order(
                    symbol=signal.symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    signal_date=day,
                    strategy_id=signal.strategy_id,
                    reason=signal.reason,
                    order_type=OrderType.MARKET,
                )
            )

    def _record_session(
        self, day: date, strategy_id: str, statement, ledger_start: int
    ) -> None:
        con = self.warehouse.con
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                "INSERT OR REPLACE INTO paper_sessions (book_id, session_date, strategy_id, "
                "equity, cash, gross_exposure, position_count, fills, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    self.book_id,
                    day,
                    strategy_id,
                    str(statement.equity),
                    str(statement.cash),
                    str(statement.gross_exposure),
                    len(statement.positions),
                    len(statement.fills),
                    datetime.now(),
                ],
            )
            for event in self.broker.ledger.events[ledger_start:]:
                if event.event_type is not EventType.CORPORATE_ACTION:
                    continue
                p = event.payload
                con.execute(
                    "INSERT INTO paper_corporate_actions (book_id, action_id, session_date, "
                    "kind, symbol, shares_before, shares_after, cash_delta, cash_in_lieu, "
                    "residual, realized_pnl_delta, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        self.book_id,
                        p["action_id"],
                        day,
                        p["kind"],
                        p["symbol"],
                        p["shares_before"],
                        p["shares_after"],
                        format(p["cash_delta"], "f"),
                        format(p["cash_in_lieu"], "f"),
                        format(p["residual"], "f"),
                        format(p["realized_pnl_delta"], "f"),
                        datetime.now(),
                    ],
                )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        self._last_persisted_session = day

    # ------------------------------------------------------------------
    def _actions_for(self, history: PriceHistory) -> list[CorporateAction]:
        if self.corporate_actions is not None:
            return list(self.corporate_actions)
        if self.raw_basis:
            source = self.corporate_action_source
            return self.warehouse.load_corporate_actions(
                symbols=history.symbols,
                source=None if source is None else (source, "manual"),
            )
        return []

    def _refuse_late_actions(
        self, history: PriceHistory, actions: list[CorporateAction]
    ) -> None:
        """An action for a day this book already traded, with no audit row.

        Booking it now would mean the day was traded on the wrong share count
        and cash, and the session cannot be re-run without replaying fills.
        """
        con = self.warehouse.con
        traded = {
            r[0]
            for r in con.execute(
                "SELECT session_date FROM paper_sessions WHERE book_id = ?", [self.book_id]
            ).fetchall()
        }
        audited = {
            r[0]
            for r in con.execute(
                "SELECT action_id FROM paper_corporate_actions WHERE book_id = ?",
                [self.book_id],
            ).fetchall()
        }
        late = []
        for action in actions:
            if action.effective_date not in traded or action.action_id in audited:
                continue
            bars = history.bars_for(action.symbol)
            if bars and bars[0].trade_date <= action.effective_date:
                late.append(action.action_id)
        if late:
            raise RunnerRefusal(
                f"corporate action data arrived after the day was traded for book "
                f"{self.book_id}: {', '.join(sorted(late))}. Those sessions were run "
                "without the action, so the book's shares and cash for them are wrong. "
                "Start a fresh book rather than trading on."
            )

    def _refuse_non_trading_day_actions(
        self,
        history: PriceHistory,
        days: list[date],
        through: date,
        actions: list[CorporateAction],
    ) -> None:
        """An action dated on a day with no session is never booked.

        The broker only books an action on the session whose date equals its
        effective date, so one dated on a holiday or weekend inside the range
        this book trades would vanish without a trace.
        """
        first = self.warehouse.con.execute(
            "SELECT min(session_date) FROM paper_sessions WHERE book_id = ?", [self.book_id]
        ).fetchall()[0][0]
        if first is None:
            if not days:
                return
            first = days[0]
        trading = set(history.trading_days)
        lost = sorted(
            a.action_id
            for a in actions
            if a.symbol in history.symbols
            and first <= a.effective_date <= through
            and a.effective_date not in trading
        )
        if lost:
            raise RunnerRefusal(
                f"{', '.join(lost)}: dated on a day that is not a trading day in the "
                "history, so no session would ever book it and the book would silently "
                "miss it. Correct the action's effective date."
            )

    @staticmethod
    def _split_like(actions: list[CorporateAction]) -> list[CorporateAction]:
        return [a for a in actions if a.kind in ("split", "adjustment")]

    def _refuse_inconsistent_splits(
        self, history: PriceHistory, days: list[date], actions: list[CorporateAction]
    ) -> None:
        window = set(days)
        for action in self._split_like(actions):
            if action.effective_date not in window:
                continue
            bars = history.bars_for(action.symbol)
            prior = [b for b in bars if b.trade_date < action.effective_date]
            today = [b for b in bars if b.trade_date == action.effective_date]
            if not prior or not today:
                continue
            reference = today[0].open if today[0].open > ZERO else today[0].close
            observed = prior[-1].close / reference
            expected = Decimal(action.numerator) / Decimal(action.denominator)
            if abs(observed / expected - 1) > SPLIT_PRICE_TOLERANCE:
                raise RunnerRefusal(
                    f"{action.action_id}: raw prices do not look like this {action.kind}. "
                    f"{action.symbol} on {action.effective_date}: expected a prior/today "
                    f"price ratio near {expected:.4f} (within {SPLIT_PRICE_TOLERANCE:.0%}), "
                    f"observed {observed:.4f} ({prior[-1].close} -> {reference}). Either "
                    "the action or the bars are wrong, and trading on either is a guess."
                )

    def _refuse_unrecorded_jumps(
        self, history: PriceHistory, days: list[date], actions: list[CorporateAction]
    ) -> None:
        if not days:
            return
        window = set(days)
        recorded = {(a.symbol, a.effective_date) for a in self._split_like(actions)}
        recorded |= self.warehouse.price_move_acknowledgements()
        found = []
        for symbol in history.symbols:
            bars = history.bars_for(symbol)
            for previous, current in zip(bars, bars[1:]):
                if current.trade_date not in window:
                    continue
                if previous.close <= ZERO or current.close <= ZERO:
                    continue
                move = max(current.close / previous.close, previous.close / current.close)
                if move > UNRECORDED_JUMP_RATIO and (symbol, current.trade_date) not in recorded:
                    found.append(
                        f"{symbol} {current.trade_date}: {previous.close} -> "
                        f"{current.close} (ratio {move:.2f})"
                    )
        if found:
            raise RunnerRefusal(
                f"{len(found)} raw price move(s) beyond {UNRECORDED_JUMP_RATIO}x with no "
                "split or adjustment recorded, which would be traded as a crash:\n  "
                + "\n  ".join(found)
                + "\nIf one is a split the vendor is missing, record it with "
                "Warehouse.record_manual_split(symbol, date, ratio). If it is a real move "
                "you have inspected, record that with "
                "Warehouse.acknowledge_price_move(symbol, date, reason)."
            )

    @staticmethod
    def _coerce(params: dict) -> dict:
        """Rebuild Decimals and tuples lost to JSON round-tripping."""
        out: dict = {}
        for key, value in params.items():
            if key in ("symbols",) and isinstance(value, (list, tuple)):
                out[key] = tuple(str(v) for v in value)
            elif isinstance(value, str):
                try:
                    out[key] = Decimal(value)
                except Exception:
                    out[key] = value
            elif isinstance(value, float):
                out[key] = Decimal(str(value))
            else:
                out[key] = value
        return out
