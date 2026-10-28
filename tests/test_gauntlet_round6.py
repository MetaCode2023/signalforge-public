"""Adversarial gauntlet, round 6: boundary and hostile conditions.

Round 6 attacked the inputs nothing had attacked yet: a history one bar long,
a window with start == end, a strategy that returns None, raises, names a
symbol that does not exist, or emits the same signal three times, prices at
0.0001 and at 813,595, a book collapsing toward zero equity, a fill that
costs exactly the cash on hand, and a single-position limit met by two
simultaneous entries.

Five defects came out of it. Three were silent; two stopped the engine dead.

* **R6-1 (HIGH) `limits.cash_cap` walked down one share at a time.** It
  started from an upper bound that ignores per-share commission and
  decremented until the total cost fitted. At a 0.0001 price with 100,000 of
  cash that is 980,382,157 iterations. Measured on the real MarketParquet
  cross-section (6,799 symbols, 85 of them priced under a cent, minimum close
  0.000001): 442.094 of 443.030 seconds of the run, 99.8%, inside 41 calls to
  this one function, 10.8s per call. The cap it returned was correct; it just
  took minutes to say so. Now solved in closed form.

* **R6-2 (MEDIUM) a signal for a symbol absent from the history vanished.**
  `_order_from_signal` found no bar, returned None, and the run recorded
  nothing at all: no order, no rejection, no risk event. A renamed or
  mistyped ticker therefore produced a strategy that did nothing and a run
  that looked perfectly healthy. The risk engine has a rule for exactly this
  (`check_price_available`) but the order never reached it.

* **R6-3 (MEDIUM) a strategy exception aborted the run with no context.**
  The exception propagated bare, so a 38-fold walk-forward dying inside fold
  17 said only "ZeroDivisionError". Aborting is the right behaviour and is
  kept deliberately (see TestAStrategyThatRaises); what was wrong is that
  nothing said which strategy, which date, or which window.

* **R6-4 (LOW) a limitation note interpolated the whole symbol list.** Every
  one of 6,799 tickers went into the "only N trading days available" string,
  producing a 52KB limitation that travels into `BacktestResult`, the reports
  and the dashboard page.

* **R6-5 (MEDIUM) the same search lived a second time in
  `FillModel.affordable_quantity`.** Found by grepping for the shape of R6-1
  rather than waiting for it to hurt: the fill-time reconciliation reduces a
  buy whose cost has risen since it was sized, and it did so by decrementing
  one share at a time from the same commission-ignoring upper bound. Bounded
  by the order size rather than by cash/price, so it could not run for
  minutes -- but a measured 1.556 seconds for ONE call on a 10,000,000-share
  penny position, and R6-1's fix makes positions that large legitimately
  reachable, so the path got hotter rather than cooler. Both call sites now
  share one closed-form solver, which lives on `ExecutionConfig` beside the
  commission schedule it inverts.

Everything else held. The confirmations are kept because an omitted case
makes "no problem" indistinguishable from "not checked".
"""

from datetime import date
from decimal import Decimal
from time import perf_counter

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.backtesting.execution import FillModel
from signalforge.backtesting.metrics import EvidenceLevel, compute_metrics
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.errors import AccountingError, LookAheadError, ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import RiskAction, Signal, SignalDirection
from signalforge.core.types import Fill, OrderStatus, Portfolio, Position, Side
from signalforge.data.storage import Warehouse
from signalforge.pipeline import ohlcv_history
from signalforge.risk import limits

from tests.conftest import business_days, make_bar

D = Decimal

DAYS = business_days(date(2020, 1, 1), 40)


# --------------------------------------------------------------------------
# Fixtures and local strategies
# --------------------------------------------------------------------------
def engine(**kw) -> BacktestEngine:
    research = ResearchConfig(
        starting_cash=D(kw.pop("cash", "100000")),
        universe=tuple(kw.pop("universe", ("AAPL",))),
        execution=ExecutionConfig(**kw.pop("execution", {})),
    )
    return BacktestEngine(research, RiskConfig(**kw.pop("risk", {})))


@pytest.fixture
def rising() -> PriceHistory:
    return PriceHistory.from_bars(
        [make_bar("AAPL", d, D(100 + i), volume=10_000_000) for i, d in enumerate(DAYS)]
    )


@pytest.fixture
def flat() -> PriceHistory:
    return PriceHistory.from_bars(
        [make_bar("AAPL", d, D("100"), volume=10_000_000) for d in DAYS]
    )


class EnterLong:
    """Asks to enter one symbol, either every day or on a single named date."""

    strategy_id = "enter_long"
    warmup_days = 0

    def __init__(self, symbol: str = "AAPL", weight: str = "0.05", on: date | None = None):
        self.symbol = symbol
        self.weight = D(weight)
        self.on = on

    def parameters(self):
        return {"symbol": self.symbol, "weight": str(self.weight)}

    def generate_signals(self, as_of, view):
        if self.on is not None and as_of != self.on:
            return []
        return [
            Signal(
                symbol=self.symbol,
                direction=SignalDirection.ENTER_LONG,
                as_of=as_of,
                strategy_id=self.strategy_id,
                reason="entry",
                target_weight=self.weight,
            )
        ]


class ReturnsNone:
    strategy_id = "returns_none"
    warmup_days = 0

    def parameters(self):
        return {}

    def generate_signals(self, as_of, view):
        return None


class RaisesOn:
    strategy_id = "raises"
    warmup_days = 0

    def __init__(self, on: date):
        self.on = on

    def parameters(self):
        return {}

    def generate_signals(self, as_of, view):
        if as_of == self.on:
            raise ZeroDivisionError("the strategy divided by zero")
        return []


class SameSignalThrice:
    strategy_id = "thrice"
    warmup_days = 0

    def __init__(self, on: date):
        self.on = on

    def parameters(self):
        return {}

    def generate_signals(self, as_of, view):
        if as_of != self.on:
            return []
        signal = Signal(
            symbol="AAPL",
            direction=SignalDirection.ENTER_LONG,
            as_of=as_of,
            strategy_id=self.strategy_id,
            reason="duplicate intent",
            target_weight=D("0.05"),
        )
        return [signal, signal, signal]


class SignalDatedWrong:
    strategy_id = "wrong_date"
    warmup_days = 0

    def __init__(self, stamped: date):
        self.stamped = stamped

    def parameters(self):
        return {}

    def generate_signals(self, as_of, view):
        return [
            Signal(
                symbol="AAPL",
                direction=SignalDirection.ENTER_LONG,
                as_of=self.stamped,
                strategy_id=self.strategy_id,
                reason="misdated",
                target_weight=D("0.05"),
            )
        ]


class EnterBoth:
    strategy_id = "enter_both"
    warmup_days = 0

    def __init__(self, on: date, symbols=("AAA", "BBB")):
        self.on = on
        self.symbols = symbols

    def parameters(self):
        return {"symbols": list(self.symbols)}

    def generate_signals(self, as_of, view):
        if as_of != self.on:
            return []
        return [
            Signal(
                symbol=symbol,
                direction=SignalDirection.ENTER_LONG,
                as_of=as_of,
                strategy_id=self.strategy_id,
                reason="simultaneous entry",
                target_weight=D("0.05"),
            )
            for symbol in self.symbols
        ]


# --------------------------------------------------------------------------
# R6-1 cash_cap at a penny price
# --------------------------------------------------------------------------
def _brute_force_cash_cap(price: Decimal, cash: Decimal, execution) -> int:
    """The obvious O(n) reference, used only where n is small enough to run."""
    quantity = 0
    while limits._total_cost(quantity + 1, price, execution) <= cash:
        quantity += 1
    return quantity


class TestCashCapAtPennyPrices:
    """The cap it computed was right. Getting there took minutes."""

    def test_a_penny_price_cap_is_computed_promptly(self):
        """The bound is set at a price the DEFECT could still return from.

        0.005 was chosen deliberately over 0.0001. Both were wrong before the
        fix, but 0.0001 needs 980,382,157 single-share decrements and takes
        roughly 400 seconds, so a test on it would have hung rather than
        failed, which is useless as a regression guard. At 0.005 the old code
        took a measured 4.076s; the closed form takes microseconds. One
        second sits four times under the defect and five orders of magnitude
        over the fix, so it neither flakes nor passes by accident.
        """
        execution = ExecutionConfig()
        start = perf_counter()
        cap = limits.cash_cap(D("0.005"), D("100000"), execution)
        elapsed = perf_counter() - start
        assert cap == 10_000_000
        assert elapsed < 1.0, f"cash_cap took {elapsed:.1f}s at a 0.005 price"

    @pytest.mark.parametrize(
        "price,cash",
        [
            ("0.0001", "100000"),
            ("0.005", "100000"),
            ("0.01", "100000"),
            ("1", "100000"),
            ("100", "100000"),
            ("813595", "100000"),
            ("100", "1001"),
            ("100", "1000.99"),
            ("100", "100"),
            ("100", "0"),
        ],
    )
    def test_the_cap_is_the_largest_affordable_quantity(self, price, cash):
        """The cap must be affordable, and one more share must not be."""
        execution = ExecutionConfig()
        price, cash = D(price), D(cash)
        cap = limits.cash_cap(price, cash, execution)
        assert cap >= 0
        if cap:
            assert limits._total_cost(cap, price, execution) <= cash
        assert limits._total_cost(cap + 1, price, execution) > cash

    @pytest.mark.parametrize("price", ["7", "19.50", "100", "250.25"])
    def test_the_cap_matches_a_brute_force_reference(self, price):
        execution = ExecutionConfig()
        price, cash = D(price), D("5000")
        assert limits.cash_cap(price, cash, execution) == _brute_force_cash_cap(
            price, cash, execution
        )

    def test_a_zero_commission_schedule_still_caps_correctly(self):
        execution = ExecutionConfig(commission_per_share="0", min_commission="0")
        cap = limits.cash_cap(D("0.0001"), D("100"), execution)
        assert cap == 1_000_000

    def test_a_non_positive_price_permits_nothing(self):
        execution = ExecutionConfig()
        assert limits.cash_cap(D("0"), D("100000"), execution) == 0
        assert limits.cash_cap(D("-1"), D("100000"), execution) == 0

    def test_a_run_over_sub_penny_prices_completes_promptly(self):
        """The engine-level symptom: one buy per sub-penny symbol per day."""
        prices = ["0.0001", "0.000001", "0.005", "0.01"]
        bars = [
            make_bar(f"P{i}", day, D(price), volume=10_000_000)
            for i, price in enumerate(prices)
            for day in DAYS[:4]
        ]
        history = PriceHistory.from_bars(bars)
        start = perf_counter()
        result = engine(universe=tuple(f"P{i}" for i in range(len(prices)))).run(
            EnterBoth(DAYS[0], symbols=tuple(f"P{i}" for i in range(len(prices)))),
            history,
        )
        elapsed = perf_counter() - start
        assert result.equity_curve
        assert elapsed < 10.0, f"a four-symbol sub-penny run took {elapsed:.1f}s"


# --------------------------------------------------------------------------
# R6-5 the same search, a second time, at fill-time reconciliation
# --------------------------------------------------------------------------
class TestAffordableQuantityAtPennyPrices:
    """`reconcile_fill` reduces a buy whose cost rose since it was sized.

    The reduction used the same decrementing search as R6-1. Bounded by the
    order size, so it could not take minutes -- but a measured 1.556s for one
    call on a 10,000,000-share penny position, and R6-1's fix makes positions
    that large reachable, so this path got hotter rather than cooler.
    """

    def _penny_fill(self, quantity=10_000_000):
        return Fill(
            order_id="o", symbol="PEN", side=Side.BUY, quantity=quantity,
            price=D("0.005"), fees=D("50000"), slippage_cost=D("0"),
            fill_date=date(2020, 1, 2),
        )

    def test_a_penny_reduction_is_computed_promptly(self):
        model = FillModel(ExecutionConfig())
        start = perf_counter()
        affordable = model.affordable_quantity(self._penny_fill(), D("60000"))
        elapsed = perf_counter() - start
        assert affordable == 6_000_000
        assert elapsed < 0.5, f"affordable_quantity took {elapsed:.3f}s"

    @pytest.mark.parametrize("cash", ["100000", "99000", "60000", "20000", "1", "0"])
    def test_the_reduction_is_affordable_and_one_more_is_not(self, cash):
        execution = ExecutionConfig()
        model = FillModel(execution)
        fill = self._penny_fill()
        cash = D(cash)
        affordable = model.affordable_quantity(fill, cash)
        assert 0 <= affordable <= fill.quantity
        if affordable:
            assert limits._total_cost(affordable, fill.price, execution) <= cash
        if affordable < fill.quantity:
            assert limits._total_cost(affordable + 1, fill.price, execution) > cash

    def test_it_never_exceeds_the_order_it_is_reducing(self):
        model = FillModel(ExecutionConfig())
        fill = self._penny_fill(quantity=40)
        assert model.affordable_quantity(fill, D("1000000")) == 40

    @pytest.mark.parametrize("price", ["7", "19.50", "100", "250.25"])
    def test_it_agrees_with_the_cap_the_risk_engine_uses(self, price):
        """One solver, two call sites: they must not be able to disagree."""
        execution = ExecutionConfig()
        model = FillModel(execution)
        price, cash = D(price), D("5000")
        fill = Fill(
            order_id="o", symbol="X", side=Side.BUY, quantity=10_000,
            price=price, fees=D("1.00"), slippage_cost=D("0"),
            fill_date=date(2020, 1, 2),
        )
        assert model.affordable_quantity(fill, cash) == limits.cash_cap(
            price, cash, execution
        )

    def test_the_commission_schedule_is_the_one_both_sides_charge(self):
        execution = ExecutionConfig()
        model = FillModel(execution)
        for quantity in (1, 100, 199, 200, 201, 10_000):
            assert model._commission(quantity) == execution.commission(quantity)
            assert execution.total_cost(quantity, D("10")) == (
                D("10") * quantity + execution.commission(quantity)
            )


# --------------------------------------------------------------------------
# R6-2 a signal for a symbol the history has never held
# --------------------------------------------------------------------------
class TestASignalForAnUnknownSymbol:
    """It used to vanish: no order, no rejection, no event, no error."""

    def test_a_symbol_absent_from_the_history_is_refused(self, rising):
        with pytest.raises(ValidationError) as excinfo:
            engine().run(EnterLong("NOPE"), rising)
        message = str(excinfo.value)
        assert "NOPE" in message
        assert "enter_long" in message

    def test_the_refusal_says_what_the_history_does_hold(self, rising):
        with pytest.raises(ValidationError) as excinfo:
            engine().run(EnterLong("NOPE"), rising)
        assert "AAPL" in str(excinfo.value)

    def test_an_exit_for_an_unknown_symbol_is_refused_too(self, rising):
        class ExitUnknown:
            strategy_id = "exit_unknown"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                return [
                    Signal(
                        symbol="GONE",
                        direction=SignalDirection.EXIT_LONG,
                        as_of=as_of,
                        strategy_id=self.strategy_id,
                        reason="exit a symbol we never had",
                    )
                ]

        with pytest.raises(ValidationError) as excinfo:
            engine().run(ExitUnknown(), rising)
        assert "GONE" in str(excinfo.value)

    def test_a_known_symbol_with_no_bar_yet_stays_a_quiet_no_op(self):
        """A late listing is a market fact, not a programming error.

        LATE has no bar until session 20. A strategy asking for it on day one
        is asking for something that does not exist yet, which is ordinary,
        so it must not raise and must not invent an order.
        """
        bars = [make_bar("AAPL", day, D("100"), volume=10_000_000) for day in DAYS]
        bars += [make_bar("LATE", day, D("50"), volume=10_000_000) for day in DAYS[20:]]
        history = PriceHistory.from_bars(bars)
        result = engine(universe=("AAPL", "LATE")).run(EnterLong("LATE"), history)
        first_order = min(o.signal_date for o in result.orders)
        assert first_order == DAYS[20]
        assert all(o.symbol == "LATE" for o in result.orders)


# --------------------------------------------------------------------------
# R6-3 a strategy that raises mid-run
# --------------------------------------------------------------------------
class TestAStrategyThatRaises:
    """Deliberate choice: abort the run, loudly, naming where it died.

    The alternative was to catch the exception and carry on with no signals
    for that session. That was rejected: the run would finish, produce an
    equity curve and a set of metrics, and nothing in the result would say a
    day of the strategy's decisions had been silently dropped. A partial
    backtest reported as a complete one is the failure mode this whole
    project exists to avoid. So the run dies -- but it now says which
    strategy, which date, and keeps the original exception as the cause.
    """

    def test_the_run_aborts_rather_than_skipping_the_session(self, rising):
        with pytest.raises(Exception) as excinfo:
            engine().run(RaisesOn(DAYS[5]), rising)
        assert excinfo.type is not AssertionError

    def test_the_abort_names_the_strategy_and_the_date(self, rising):
        with pytest.raises(ValidationError) as excinfo:
            engine().run(RaisesOn(DAYS[5]), rising)
        message = str(excinfo.value)
        assert "raises" in message
        assert DAYS[5].isoformat() in message

    def test_the_original_exception_is_preserved_as_the_cause(self, rising):
        with pytest.raises(ValidationError) as excinfo:
            engine().run(RaisesOn(DAYS[5]), rising)
        cause = excinfo.value.__cause__
        assert isinstance(cause, ZeroDivisionError)
        assert "divided by zero" in str(cause)

    def test_a_look_ahead_attempt_is_not_rewrapped(self, rising):
        """The one strategy failure whose type must survive the wrapper.

        Adding context to strategy exceptions turned `LookAheadError` into a
        generic `ValidationError`, which hid the single most important thing
        this engine detects. Caught by `tests/test_engine.py`; pinned here
        too, because the next person to add a handler here needs to see why
        the exemption exists.
        """

        class Peeks:
            strategy_id = "peeks"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                view.bar("AAPL", DAYS[-1])
                return []

        with pytest.raises(LookAheadError) as excinfo:
            engine().run(Peeks(), rising)
        assert "AAPL" in str(excinfo.value)


# --------------------------------------------------------------------------
# R6-4 the unbounded limitation string
# --------------------------------------------------------------------------
class TestLimitationNotesStayReadable:
    @pytest.fixture
    def warehouse(self):
        w = Warehouse(":memory:")
        yield w
        w.close()

    def test_a_wide_universe_does_not_inline_every_ticker(self, warehouse):
        symbols = [f"SYM{i:04d}" for i in range(500)]
        warehouse.upsert_bars(
            [
                make_bar(symbol, day, D("10"), volume=1_000_000, source="wide")
                for symbol in symbols
                for day in DAYS[:4]
            ]
        )
        bundle = ohlcv_history(warehouse, symbols, sources=["wide"])
        assert bundle.limitations
        for note in bundle.limitations:
            assert len(note) < 400, f"a limitation note is {len(note)} characters long"
        joined = " ".join(bundle.limitations)
        assert "500" in joined
        assert "4 distinct trading days" in joined

    def test_the_note_still_names_the_symbol_when_there_is_only_one(self, warehouse):
        warehouse.upsert_bars(
            [
                make_bar("AAPL", day, D("10"), volume=1_000_000, source="narrow")
                for day in DAYS[:4]
            ]
        )
        bundle = ohlcv_history(warehouse, ["AAPL"], sources=["narrow"])
        assert any("AAPL" in note for note in bundle.limitations)

    def test_an_empty_result_does_not_inline_every_requested_ticker(self, warehouse):
        symbols = [f"SYM{i:04d}" for i in range(500)]
        with pytest.raises(ValidationError) as excinfo:
            ohlcv_history(warehouse, symbols)
        assert len(str(excinfo.value)) < 400
        assert "500" in str(excinfo.value)


# --------------------------------------------------------------------------
# Confirmations: hostile conditions that were already handled
# --------------------------------------------------------------------------
class TestDegenerateWindows:
    def test_a_single_bar_history_runs_and_fills_nothing(self):
        history = PriceHistory.from_bars(
            [make_bar("AAPL", DAYS[0], D("100"), volume=10_000_000)]
        )
        result = engine().run(EnterLong(), history)
        assert len(result.equity_curve) == 1
        assert len(result.fills) == 0
        assert [o.status for o in result.orders] == [OrderStatus.EXPIRED]
        assert result.final_equity == D("100000")

    def test_a_single_bar_history_leaves_no_committed_cash(self):
        history = PriceHistory.from_bars(
            [make_bar("AAPL", DAYS[0], D("100"), volume=10_000_000)]
        )
        residual = engine().run_and_report_residual(EnterLong(), history)
        assert residual == D("0")

    def test_start_equal_to_end_runs_exactly_one_session(self, rising):
        result = engine().run(EnterLong(on=DAYS[5]), rising, start=DAYS[5], end=DAYS[5])
        assert len(result.equity_curve) == 1
        assert result.start_date == result.end_date == DAYS[5]
        assert len(result.fills) == 0
        assert [o.status for o in result.orders] == [OrderStatus.EXPIRED]

    def test_a_window_outside_the_history_is_refused(self, rising):
        from signalforge.core.errors import ConfigError

        with pytest.raises(ConfigError):
            engine().run(EnterLong(), rising, start=date(2030, 1, 1), end=date(2030, 2, 1))


class TestMisbehavingStrategyReturnValues:
    def test_a_strategy_returning_none_is_treated_as_no_signals(self, rising):
        result = engine().run(ReturnsNone(), rising)
        assert len(result.equity_curve) == len(DAYS)
        assert result.orders == []
        assert result.final_equity == D("100000")

    def test_three_identical_signals_produce_one_fill_and_two_rejections(self, rising):
        result = engine().run(SameSignalThrice(DAYS[5]), rising)
        assert len(result.orders) == 3
        statuses = [o.status for o in result.orders]
        assert statuses.count(OrderStatus.FILLED) == 1
        assert statuses.count(OrderStatus.REJECTED) == 2
        assert {e.rule for e in result.risk_events} == {"duplicate_order"}
        assert all(e.action is RiskAction.REJECT for e in result.risk_events)
        assert len(result.fills) == 1

    def test_a_signal_dated_before_the_session_is_refused(self, rising):
        with pytest.raises(ValidationError) as excinfo:
            engine().run(SignalDatedWrong(DAYS[0]), rising, start=DAYS[1])
        assert DAYS[0].isoformat() in str(excinfo.value)

    def test_a_signal_dated_after_the_session_is_refused(self, rising):
        with pytest.raises(ValidationError) as excinfo:
            engine().run(SignalDatedWrong(DAYS[39]), rising, start=DAYS[1], end=DAYS[5])
        assert DAYS[39].isoformat() in str(excinfo.value)


class TestExtremePrices:
    def test_a_price_above_the_whole_budget_produces_no_order(self):
        """813,595 is the real maximum close in the MarketParquet snapshot."""
        history = PriceHistory.from_bars(
            [make_bar("HUGE", day, D("813595"), volume=10_000_000) for day in DAYS]
        )
        result = engine(universe=("HUGE",)).run(EnterLong("HUGE"), history)
        assert result.orders == []
        assert result.fills == []
        assert result.final_equity == D("100000")

    def test_a_sub_penny_price_still_respects_the_single_name_cap(self):
        history = PriceHistory.from_bars(
            [make_bar("PEN", day, D("0.0001"), volume=10_000_000) for day in DAYS]
        )
        result = engine(universe=("PEN",)).run(EnterLong("PEN"), history)
        assert result.fills
        exposure = sum(f.quantity for f in result.fills) * D("0.0001")
        # 5% of 100,000 is 5,000. Participation (1% of 10,000,000 = 100,000
        # shares, worth 10) binds far tighter, so the cap is never breached.
        assert exposure <= D("5000")


class TestEquityApproachingZero:
    @pytest.fixture
    def collapsing(self) -> PriceHistory:
        return PriceHistory.from_bars(
            [
                make_bar("CRA", day, D("100") if i < 5 else D("0.000001"), volume=10_000_000)
                for i, day in enumerate(DAYS)
            ]
        )

    def _engine(self):
        return engine(
            universe=("CRA",),
            risk={
                "max_single_name_exposure": "1.0",
                "max_gross_leverage": "1.0",
                "max_daily_loss": "0.99",
                "max_portfolio_drawdown": "1.0",
            },
            execution={"max_participation_of_volume": "1.0"},
        )

    def test_a_near_total_loss_does_not_crash_the_run(self, collapsing):
        result = self._engine().run(EnterLong("CRA", weight="1.0", on=DAYS[0]), collapsing)
        assert result.final_equity > D("0")
        assert result.equity_curve[-1].drawdown < D("1")

    def test_the_metrics_of_a_near_total_loss_are_finite_and_gated(self, collapsing):
        runner = self._engine()
        result = runner.run(EnterLong("CRA", weight="1.0", on=DAYS[0]), collapsing)
        metrics = compute_metrics(result, config=runner.research)
        assert metrics.total_return < D("-0.99")
        assert metrics.cagr is not None and metrics.cagr > D("-2")
        assert metrics.max_drawdown < D("1")
        assert metrics.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE

    def test_a_book_cannot_be_valued_at_a_non_positive_mark(self):
        """Exactly zero equity is unreachable, and that is the design.

        Reaching it would mean marking an open position at zero. The book
        refuses to do that, because valuing an unmarkable holding at nothing
        understates a drawdown instead of reporting that the price is gone.
        """
        book = Portfolio(cash=D("0"))
        book.positions["CRA"] = Position(symbol="CRA")
        book.positions["CRA"].apply_buy(1, D("100"), D("0"))
        with pytest.raises(AccountingError):
            book.equity({"CRA": D("0")})
        with pytest.raises(AccountingError):
            book.equity({})


class TestCashBoundaryFills:
    @pytest.fixture
    def exact(self):
        return dict(
            universe=("AAPL",),
            risk={"max_single_name_exposure": "1.0", "max_gross_leverage": "1.0"},
            execution={
                "slippage_bps": "0",
                "commission_per_share": "0",
                "min_commission": "1.00",
                "max_participation_of_volume": "1.0",
            },
        )

    def test_a_fill_costing_exactly_the_cash_on_hand_is_allowed(self, flat, exact):
        """1001 of cash buys 10 shares at 100 plus the 1.00 commission floor."""
        result = engine(cash="1001", **exact).run(EnterLong(weight="1.0", on=DAYS[0]), flat)
        assert len(result.fills) == 1
        fill = result.fills[0]
        assert fill.quantity == 10
        assert fill.price == D("100.000000")
        assert fill.fees == D("1.00")
        assert result.equity_curve[-1].cash == D("0")
        assert result.risk_events == []

    def test_one_cent_short_reduces_rather_than_overdrawing(self, flat, exact):
        result = engine(cash="1000.99", **exact).run(
            EnterLong(weight="1.0", on=DAYS[0]), flat
        )
        assert len(result.fills) == 1
        assert result.fills[0].quantity == 9
        assert [e.rule for e in result.risk_events] == ["cash_available"]
        assert [e.action for e in result.risk_events] == [RiskAction.REDUCE]
        assert result.equity_curve[-1].cash >= D("0")

    def test_cash_never_goes_negative_on_the_boundary(self, flat, exact):
        for cash in ("1001", "1000.99", "101", "100.99", "1.00", "0.99"):
            result = engine(cash=cash, **exact).run(
                EnterLong(weight="1.0", on=DAYS[0]), flat
            )
            assert all(point.cash >= D("0") for point in result.equity_curve), cash


class TestMaxPositionsOfOne:
    @pytest.fixture
    def two_symbols(self) -> PriceHistory:
        return PriceHistory.from_bars(
            [
                make_bar(symbol, day, D("100"), volume=10_000_000)
                for symbol in ("AAA", "BBB")
                for day in DAYS
            ]
        )

    def test_two_simultaneous_entries_fill_only_one(self, two_symbols):
        result = engine(universe=("AAA", "BBB"), risk={"max_positions": 1}).run(
            EnterBoth(DAYS[5]), two_symbols
        )
        assert len(result.fills) == 1
        assert result.fills[0].symbol == "AAA"
        rejected = [o for o in result.orders if o.status is OrderStatus.REJECTED]
        assert [o.symbol for o in rejected] == ["BBB"]
        assert [e.rule for e in result.risk_events] == ["max_positions"]

    def test_the_rejection_counts_the_committed_position_not_the_filled_one(
        self, two_symbols
    ):
        """BBB is refused on the signal bar, before AAA has filled at all."""
        result = engine(universe=("AAA", "BBB"), risk={"max_positions": 1}).run(
            EnterBoth(DAYS[5]), two_symbols
        )
        event = result.risk_events[0]
        assert event.event_date == DAYS[5]
        assert event.symbol == "BBB"
        assert event.approved_quantity == 0
