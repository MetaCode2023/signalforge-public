"""The risk engine.

Strategies propose; this engine decides. Every test here is written from the
engine's side of the seam: it is handed an order and a book and must reach a
verdict without knowing or caring why the order was wanted.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.config.models import ExecutionConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import RiskAction
from signalforge.core.types import Fill, Order, OrderType, Portfolio, Side
from signalforge.risk.engine import RiskEngine, RiskState

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 300)


def history_at(price: str = "100", symbol: str = "AAPL", volume: int = 10_000_000):
    """Flat series: trailing volatility is zero, so vol-based caps don't bind."""
    return PriceHistory.from_bars(
        [make_bar(symbol, d, D(price), volume=volume) for d in DAYS]
    )


def multi_history(prices: dict[str, str]):
    bars = []
    for symbol, price in prices.items():
        bars.extend(make_bar(symbol, d, D(price), volume=10_000_000) for d in DAYS)
    return PriceHistory.from_bars(bars)


def declining_history(symbol: str = "AAPL", start: int = 200, step: int = 1):
    return PriceHistory.from_bars(
        [
            make_bar(symbol, d, D(max(start - i * step, 1)), volume=10_000_000)
            for i, d in enumerate(DAYS)
        ]
    )


def mk_order(**kw) -> Order:
    base = dict(
        order_id="o1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        signal_date=DAYS[100],
        strategy_id="strat",
        reason="test",
    )
    base.update(kw)
    return Order(**base)


def buy(pf: Portfolio, symbol: str, qty: int, price: str, fill_id: str, day=None) -> None:
    pf.apply_fill(
        Fill(
            order_id=fill_id,
            fill_id=fill_id,
            symbol=symbol,
            side=Side.BUY,
            quantity=qty,
            price=D(price),
            fees=D("0"),
            slippage_cost=D("0"),
            fill_date=day or DAYS[100],
        )
    )


def engine(**overrides) -> RiskEngine:
    return RiskEngine(RiskConfig(**overrides), ExecutionConfig())


def fresh_state(equity: str = "100000") -> RiskState:
    state = RiskState()
    state.open_day(DAYS[100], D(equity))
    return state


class TestEngineIndependence:
    def test_engine_needs_no_strategy_object_to_decide(self):
        # The engine's signature is the proof of independence: it receives an
        # order, a book and market data. There is no hook a strategy can use.
        eng = engine()
        decision = eng.evaluate(
            mk_order(), Portfolio(cash=D("100000")), history_at().view(DAYS[100]), fresh_state()
        )
        assert decision.action in (RiskAction.ACCEPT, RiskAction.REDUCE, RiskAction.REJECT)

    def test_every_decision_names_the_rule_that_produced_it(self):
        eng = engine()
        decision = eng.evaluate(
            mk_order(quantity=100_000),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[100]),
            fresh_state(),
        )
        assert decision.rule
        assert decision.reason


class TestExposureLimits:
    def test_accepts_an_order_inside_every_limit(self):
        eng = engine()
        decision = eng.evaluate(
            mk_order(quantity=10),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[100]),
            fresh_state(),
        )
        assert decision.action is RiskAction.ACCEPT
        assert decision.approved_quantity == 10

    def test_reduces_an_order_that_would_breach_the_single_name_cap(self):
        # 5% of 100k equity at 100/share = 50 shares maximum.
        eng = engine()
        decision = eng.evaluate(
            mk_order(quantity=200),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[100]),
            fresh_state(),
        )
        assert decision.action is RiskAction.REDUCE
        assert decision.approved_quantity == 50
        assert "single_name" in decision.rule

    def test_counts_the_existing_holding_towards_the_single_name_cap(self):
        eng = engine(allow_pyramiding=True)
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 45, "100", "f1")
        state = fresh_state()
        decision = eng.evaluate(
            mk_order(quantity=20), pf, history_at().view(DAYS[100]), state
        )
        # Already holds 45 of a 50-share cap, so only 5 more are allowed.
        assert decision.action is RiskAction.REDUCE
        assert decision.approved_quantity == 5

    def test_rejects_when_the_single_name_cap_is_already_full(self):
        eng = engine(allow_pyramiding=True)
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 50, "100", "f1")
        decision = eng.evaluate(
            mk_order(quantity=10), pf, history_at().view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REJECT

    def test_rejects_a_buy_with_insufficient_cash_rather_than_borrowing(self):
        eng = engine()
        decision = eng.evaluate(
            mk_order(quantity=10),
            Portfolio(cash=D("50")),
            history_at().view(DAYS[100]),
            fresh_state("50"),
        )
        assert decision.action is RiskAction.REJECT
        assert "cash" in decision.rule or "margin" in decision.rule

    def test_reduces_to_what_cash_affords(self):
        # The exposure cap is widened so that cash is the only binding
        # constraint; at a 5% cap a 505 book cannot buy a 100 share at all,
        # and rejecting outright would be the correct answer there.
        eng = engine(max_single_name_exposure=D("1.0"))
        decision = eng.evaluate(
            mk_order(quantity=10),
            Portfolio(cash=D("505")),
            history_at().view(DAYS[100]),
            fresh_state("505"),
        )
        assert decision.action is RiskAction.REDUCE
        assert decision.approved_quantity == 5

    def test_rejects_a_buy_the_exposure_cap_cannot_fit_a_single_share_into(self):
        eng = engine()
        decision = eng.evaluate(
            mk_order(quantity=1),
            Portfolio(cash=D("505")),
            history_at().view(DAYS[100]),
            fresh_state("505"),
        )
        assert decision.action is RiskAction.REJECT

    def test_enforces_gross_leverage_of_one(self):
        eng = engine(max_positions=50, max_single_name_exposure=D("1.0"))
        pf = Portfolio(cash=D("100000"))
        state = fresh_state()
        decision = eng.evaluate(
            mk_order(quantity=2000), pf, history_at().view(DAYS[100]), state
        )
        # 1000 shares at 100 is 100% of equity; cash binds at the same point.
        assert decision.approved_quantity <= 1000

    def test_rejects_a_new_position_beyond_the_position_count_cap(self):
        eng = engine(max_positions=2)
        hist = multi_history({"A": "100", "B": "100", "C": "100"})
        pf = Portfolio(cash=D("100000"))
        buy(pf, "A", 1, "100", "f1")
        buy(pf, "B", 1, "100", "f2")
        decision = eng.evaluate(
            mk_order(symbol="C", quantity=1), pf, hist.view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REJECT
        assert "max_positions" in decision.rule

    def test_adding_to_an_existing_symbol_does_not_consume_a_new_position_slot(self):
        eng = engine(max_positions=1, allow_pyramiding=True)
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 1, "100", "f1")
        decision = eng.evaluate(
            mk_order(quantity=1), pf, history_at().view(DAYS[100]), fresh_state()
        )
        assert decision.action is not RiskAction.REJECT


class TestSellSideRules:
    def test_rejects_a_sell_of_more_than_is_held(self):
        eng = engine()
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 10, "100", "f1")
        decision = eng.evaluate(
            mk_order(side=Side.SELL, quantity=20), pf, history_at().view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REDUCE
        assert decision.approved_quantity == 10

    def test_rejects_a_sell_with_nothing_held(self):
        eng = engine()
        decision = eng.evaluate(
            mk_order(side=Side.SELL, quantity=1),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[100]),
            fresh_state(),
        )
        assert decision.action is RiskAction.REJECT
        assert "short" in decision.rule

    def test_a_sell_is_exempt_from_exposure_caps(self):
        # Exiting reduces risk; a cap that blocked an exit would be a trap.
        eng = engine(max_single_name_exposure=D("0.0001"))
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 50, "100", "f1")
        decision = eng.evaluate(
            mk_order(side=Side.SELL, quantity=50), pf, history_at().view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.ACCEPT


class TestCircuitBreakers:
    def test_daily_loss_breaker_blocks_new_buys(self):
        eng = engine()
        state = fresh_state("100000")
        state.mark_equity(DAYS[100], D("97000"))  # -3%, past the 2% limit
        decision = eng.evaluate(
            mk_order(), Portfolio(cash=D("97000")), history_at().view(DAYS[100]), state
        )
        assert decision.action is RiskAction.REJECT
        assert "daily_loss" in decision.rule

    def test_daily_loss_breaker_still_permits_exits(self):
        eng = engine()
        pf = Portfolio(cash=D("90000"))
        buy(pf, "AAPL", 10, "100", "f1")
        state = fresh_state("100000")
        state.mark_equity(DAYS[100], D("97000"))
        decision = eng.evaluate(
            mk_order(side=Side.SELL, quantity=10), pf, history_at().view(DAYS[100]), state
        )
        assert decision.allows_trade

    def test_drawdown_breaker_blocks_new_buys(self):
        eng = engine()
        state = RiskState()
        state.open_day(DAYS[50], D("100000"))
        state.mark_equity(DAYS[50], D("100000"))
        state.open_day(DAYS[100], D("89000"))
        state.mark_equity(DAYS[100], D("89000"))  # -11% from the 100k peak
        decision = eng.evaluate(
            mk_order(), Portfolio(cash=D("89000")), history_at().view(DAYS[100]), state
        )
        assert decision.action is RiskAction.REJECT
        assert "drawdown" in decision.rule

    def test_drawdown_is_measured_from_the_peak_not_the_start(self):
        eng = engine()
        state = RiskState()
        state.open_day(DAYS[10], D("100000"))
        state.mark_equity(DAYS[10], D("200000"))  # doubled
        state.open_day(DAYS[100], D("185000"))
        state.mark_equity(DAYS[100], D("175000"))  # -12.5% from the 200k peak
        decision = eng.evaluate(
            mk_order(), Portfolio(cash=D("175000")), history_at().view(DAYS[100]), state
        )
        assert decision.action is RiskAction.REJECT
        assert "drawdown" in decision.rule

    def test_a_breaker_trip_is_recorded_as_an_event(self):
        eng = engine()
        state = fresh_state("100000")
        state.mark_equity(DAYS[100], D("97000"))
        eng.evaluate(mk_order(), Portfolio(cash=D("97000")), history_at().view(DAYS[100]), state)
        assert any("daily_loss" in e.rule for e in eng.events)

    def test_manual_pause_blocks_every_new_buy(self):
        eng = engine()
        state = fresh_state()
        state.paused = True
        state.pause_reason = "operator halted paper trading"
        decision = eng.evaluate(
            mk_order(), Portfolio(cash=D("100000")), history_at().view(DAYS[100]), state
        )
        assert decision.action is RiskAction.REJECT
        assert "pause" in decision.rule

    def test_manual_pause_still_permits_exits(self):
        eng = engine()
        pf = Portfolio(cash=D("90000"))
        buy(pf, "AAPL", 10, "100", "f1")
        state = fresh_state()
        state.paused = True
        decision = eng.evaluate(
            mk_order(side=Side.SELL, quantity=10), pf, history_at().view(DAYS[100]), state
        )
        assert decision.allows_trade


class TestDataIntegrityGuards:
    def test_rejects_an_order_on_stale_data(self):
        eng = engine(stale_data_max_age_days=5)
        hist = PriceHistory.from_bars([make_bar("AAPL", DAYS[50], D("100"))])
        decision = eng.evaluate(
            mk_order(), Portfolio(cash=D("100000")), hist.view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REJECT
        assert "stale" in decision.rule

    def test_rejects_an_order_with_no_price_at_all(self):
        eng = engine()
        hist = PriceHistory.from_bars([make_bar("OTHER", DAYS[100], D("100"))])
        decision = eng.evaluate(
            mk_order(), Portfolio(cash=D("100000")), hist.view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REJECT
        assert "price" in decision.rule

    def test_rejects_an_order_in_a_zero_volume_bar(self):
        eng = engine()
        hist = PriceHistory.from_bars(
            [make_bar("AAPL", d, D("100"), volume=0) for d in DAYS]
        )
        decision = eng.evaluate(
            mk_order(), Portfolio(cash=D("100000")), hist.view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REJECT
        assert "liquidity" in decision.rule or "volume" in decision.rule


class TestDuplicateAndRateLimits:
    def test_rejects_an_exact_duplicate_order(self):
        eng = engine()
        state = fresh_state()
        order = mk_order()
        first = eng.evaluate(order, Portfolio(cash=D("100000")), history_at().view(DAYS[100]), state)
        assert first.allows_trade
        state.record_submission(order)
        second = eng.evaluate(
            mk_order(order_id="o2"), Portfolio(cash=D("100000")), history_at().view(DAYS[100]), state
        )
        assert second.action is RiskAction.REJECT
        assert "duplicate" in second.rule

    def test_rejects_beyond_the_per_symbol_daily_order_cap(self):
        eng = engine(max_orders_per_symbol_per_day=2, allow_pyramiding=True)
        state = fresh_state()
        for i in range(2):
            state.record_submission(mk_order(order_id=f"o{i}", quantity=1 + i))
        decision = eng.evaluate(
            mk_order(order_id="o9", quantity=7),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[100]),
            state,
        )
        assert decision.action is RiskAction.REJECT
        assert "per_symbol" in decision.rule

    def test_rejects_beyond_the_portfolio_wide_daily_order_cap(self):
        eng = engine(max_orders_per_day=3, max_positions=50)
        hist = multi_history({"A": "100", "B": "100", "C": "100", "D": "100"})
        state = fresh_state()
        for i, sym in enumerate("ABC"):
            state.record_submission(mk_order(order_id=f"o{i}", symbol=sym))
        decision = eng.evaluate(
            mk_order(order_id="o9", symbol="D"),
            Portfolio(cash=D("100000")),
            hist.view(DAYS[100]),
            state,
        )
        assert decision.action is RiskAction.REJECT
        assert "orders_per_day" in decision.rule

    def test_the_daily_order_counter_resets_on_a_new_day(self):
        eng = engine(max_orders_per_day=1)
        state = fresh_state()
        state.record_submission(mk_order())
        state.open_day(DAYS[101], D("100000"))
        decision = eng.evaluate(
            mk_order(order_id="o2", signal_date=DAYS[101]),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[101]),
            state,
        )
        assert decision.allows_trade


class TestMartingaleDetection:
    def test_blocks_averaging_down_by_default(self):
        eng = engine()
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 10, "200", "f1")  # cost basis 200, now at 100
        decision = eng.evaluate(
            mk_order(quantity=10), pf, history_at("100").view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REJECT
        assert "averaging_down" in decision.rule

    def test_permits_averaging_down_only_when_explicitly_configured(self):
        eng = engine(allow_averaging_down=True, allow_pyramiding=True)
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 10, "200", "f1")
        decision = eng.evaluate(
            mk_order(quantity=10), pf, history_at("100").view(DAYS[100]), fresh_state()
        )
        assert decision.allows_trade

    def test_averaging_down_remains_subject_to_the_exposure_cap(self):
        # An explicitly permitted experiment is still capped in aggregate.
        eng = engine(allow_averaging_down=True, allow_pyramiding=True)
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 45, "200", "f1")
        decision = eng.evaluate(
            mk_order(quantity=100), pf, history_at("100").view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REDUCE

    def test_blocks_pyramiding_by_default(self):
        eng = engine()
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 10, "100", "f1")
        decision = eng.evaluate(
            mk_order(quantity=1), pf, history_at("100").view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REJECT
        assert "pyramiding" in decision.rule

    def test_blocks_a_doubled_order_after_a_losing_trade(self):
        eng = engine()
        state = fresh_state()
        state.record_closed_trade(symbol="AAPL", strategy_id="strat", quantity=10, pnl=D("-500"))
        decision = eng.evaluate(
            mk_order(quantity=20), Portfolio(cash=D("100000")), history_at().view(DAYS[100]), state
        )
        assert decision.action is RiskAction.REJECT
        assert "martingale" in decision.rule

    def test_permits_the_same_size_after_a_losing_trade(self):
        eng = engine()
        state = fresh_state()
        state.record_closed_trade(symbol="AAPL", strategy_id="strat", quantity=10, pnl=D("-500"))
        decision = eng.evaluate(
            mk_order(quantity=10), Portfolio(cash=D("100000")), history_at().view(DAYS[100]), state
        )
        assert decision.allows_trade

    def test_permits_a_larger_size_after_a_winning_trade(self):
        eng = engine()
        state = fresh_state()
        state.record_closed_trade(symbol="AAPL", strategy_id="strat", quantity=10, pnl=D("500"))
        decision = eng.evaluate(
            mk_order(quantity=20), Portfolio(cash=D("100000")), history_at().view(DAYS[100]), state
        )
        assert decision.allows_trade

    def test_blocks_increasing_exposure_during_a_sustained_decline(self):
        eng = engine()
        decision = eng.evaluate(
            mk_order(quantity=10),
            Portfolio(cash=D("100000")),
            declining_history().view(DAYS[150]),
            fresh_state(),
        )
        assert decision.action is RiskAction.REJECT
        assert "sustained_decline" in decision.rule

    def test_a_sustained_decline_does_not_block_an_exit(self):
        eng = engine()
        pf = Portfolio(cash=D("90000"))
        buy(pf, "AAPL", 10, "100", "f1")
        decision = eng.evaluate(
            mk_order(side=Side.SELL, quantity=10),
            pf,
            declining_history().view(DAYS[150]),
            fresh_state(),
        )
        assert decision.allows_trade

    def test_detects_limit_circumvention_by_order_splitting(self):
        # Three small orders that individually pass but together breach the cap.
        eng = engine(max_orders_per_symbol_per_day=10, allow_pyramiding=True)
        state = fresh_state()
        pf = Portfolio(cash=D("100000"))
        hist = history_at().view(DAYS[100])
        approved_total = 0
        for i in range(5):
            # Quantities vary so the duplicate-order rule is not what stops
            # this; the aggregate exposure cap has to be what binds.
            decision = eng.evaluate(
                mk_order(order_id=f"o{i}", quantity=20 + i), pf, hist, state
            )
            if decision.allows_trade:
                approved_total += decision.approved_quantity
                state.record_submission(
                    mk_order(order_id=f"o{i}", quantity=decision.approved_quantity)
                )
                buy(pf, "AAPL", decision.approved_quantity, "100", f"f{i}")
        # 5% of 100k at 100/share is a hard 50-share ceiling, however it is sliced.
        assert approved_total <= 50

    def test_aggregates_correlated_exposure_across_strategies(self):
        eng = engine(
            max_positions=50,
            max_correlated_cluster_exposure=D("0.10"),
            allow_pyramiding=True,
        )
        hist = multi_history({"A": "100", "B": "100"})
        pf = Portfolio(cash=D("100000"))
        buy(pf, "A", 100, "100", "f1")  # 10% of equity already in the cluster
        eng.set_correlation_clusters({"cluster1": ("A", "B")})
        decision = eng.evaluate(
            mk_order(symbol="B", quantity=10, strategy_id="other"),
            pf,
            hist.view(DAYS[100]),
            fresh_state(),
        )
        assert decision.action is RiskAction.REJECT
        assert "correlated" in decision.rule


class TestRiskBudgetPerTrade:
    def test_sizes_down_to_the_per_trade_risk_budget(self):
        # 0.5% of 100k is 500. With a volatile series the per-share risk is
        # large, so the budget must bind before the exposure cap does.
        eng = engine()
        volatile = PriceHistory.from_bars(
            [
                make_bar("AAPL", d, D(100 + (20 if i % 2 else -20)), volume=10_000_000)
                for i, d in enumerate(DAYS)
            ]
        )
        decision = eng.evaluate(
            mk_order(quantity=50), Portfolio(cash=D("100000")), volatile.view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.REDUCE
        assert "risk_budget" in decision.rule

    def test_zero_volatility_does_not_divide_by_zero(self):
        eng = engine()
        decision = eng.evaluate(
            mk_order(quantity=10), Portfolio(cash=D("100000")), history_at().view(DAYS[100]), fresh_state()
        )
        assert decision.action is RiskAction.ACCEPT


class TestReconciliation:
    def test_reconciles_a_matching_book(self):
        eng = engine()
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 10, "100", "f1")
        report = eng.reconcile(pf, {"AAPL": 10}, expected_cash=D("99000"))
        assert report.ok
        assert report.breaks == []

    def test_detects_a_quantity_break(self):
        eng = engine()
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 10, "100", "f1")
        report = eng.reconcile(pf, {"AAPL": 11}, expected_cash=D("99000"))
        assert not report.ok
        assert any("AAPL" in b for b in report.breaks)

    def test_detects_a_cash_break(self):
        eng = engine()
        pf = Portfolio(cash=D("100000"))
        report = eng.reconcile(pf, {}, expected_cash=D("99999"))
        assert not report.ok
        assert any("cash" in b for b in report.breaks)

    def test_detects_a_position_present_only_in_the_book(self):
        eng = engine()
        pf = Portfolio(cash=D("100000"))
        buy(pf, "AAPL", 10, "100", "f1")
        report = eng.reconcile(pf, {}, expected_cash=D("99000"))
        assert not report.ok


class TestRejectionsAreLogged:
    def test_every_rejection_is_appended_to_the_event_log(self):
        eng = engine()
        before = len(eng.events)
        eng.evaluate(
            mk_order(side=Side.SELL, quantity=1),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[100]),
            fresh_state(),
        )
        assert len(eng.events) == before + 1
        assert eng.events[-1].action is RiskAction.REJECT

    def test_a_reduction_is_logged_too(self):
        eng = engine()
        eng.evaluate(
            mk_order(quantity=500),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[100]),
            fresh_state(),
        )
        assert any(e.action is RiskAction.REDUCE for e in eng.events)

    def test_an_acceptance_is_not_noise_in_the_event_log(self):
        eng = engine()
        eng.evaluate(
            mk_order(quantity=1),
            Portfolio(cash=D("100000")),
            history_at().view(DAYS[100]),
            fresh_state(),
        )
        assert all(e.action is not RiskAction.ACCEPT for e in eng.events)


class TestConfigRespected:
    def test_a_tighter_single_name_cap_binds_sooner(self):
        eng = engine(max_single_name_exposure=D("0.01"))
        decision = eng.evaluate(
            mk_order(quantity=100), Portfolio(cash=D("100000")), history_at().view(DAYS[100]), fresh_state()
        )
        assert decision.approved_quantity == 10

    def test_a_looser_daily_loss_limit_does_not_trip_as_early(self):
        eng = engine(max_daily_loss=D("0.05"))
        state = fresh_state("100000")
        state.mark_equity(DAYS[100], D("97000"))  # -3%, inside a 5% limit
        decision = eng.evaluate(
            mk_order(quantity=1), Portfolio(cash=D("97000")), history_at().view(DAYS[100]), state
        )
        assert decision.allows_trade
