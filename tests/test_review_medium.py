"""The four MEDIUM findings from code review, each measured before fixing.

M1 `canonical_json` did not normalise Decimal scale and did not order sets,
   so the same logical parameters produced different hold-out keys.
   Decimal("0.05"), Decimal("0.050") and float 0.05 gave three keys, i.e.
   three fresh looks at the hold-out for one logical weight; a set-valued
   parameter gave a key that differed between processes because str hashing
   is seed-randomised, so the guard could not fire at all.

M2 `BacktestEngine.fingerprint` interpolated str(Decimal), which preserves
   trailing zeros, so the same prices loaded via two paths fingerprinted
   differently. `Warehouse.fingerprint` already formatted to 6dp; the
   engine's did not. Since the fingerprint is a component of the hold-out
   key, a changed load path was a fresh look.

M3 `volume_known` was lost on a warehouse round trip, flipping "nobody told
   us" into "nothing traded". Three risk surfaces change meaning: the
   liquidity breaker starts blocking every buy, the participation cap
   becomes 0, and the run stops declaring that those checks were skipped.

M4 `Portfolio.realized_pnl` only accumulated when a position fully closed,
   so a partially closed position's realized gain never reached
   `EquityPoint.realized_pnl`. Reachable with the DEFAULT config, because a
   sell is partially filled at the participation cap.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.errors import ValidationError
from signalforge.core.types import Bar, Fill, Portfolio, Side
from signalforge.data.storage import Warehouse

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 40)


# ======================================================================
# M1 — canonical parameter hashing
# ======================================================================
class TestCanonicalParameterHashing:
    def test_decimal_scale_does_not_change_the_hash(self):
        from signalforge.learning.registry import ExperimentRegistry

        a = ExperimentRegistry.holdout_key("s", {"w": D("0.05")}, "fp")
        b = ExperimentRegistry.holdout_key("s", {"w": D("0.050")}, "fp")
        c = ExperimentRegistry.holdout_key("s", {"w": D("5e-2")}, "fp")
        assert a == b == c, (
            "the same logical weight produced different hold-out keys, which is "
            "three free looks at the test window"
        )

    def test_a_float_and_an_equal_decimal_agree(self):
        from signalforge.learning.registry import ExperimentRegistry

        assert ExperimentRegistry.holdout_key(
            "s", {"w": D("0.05")}, "fp"
        ) == ExperimentRegistry.holdout_key("s", {"w": 0.05}, "fp")

    def test_a_set_valued_parameter_hashes_deterministically(self):
        from signalforge.learning.registry import ExperimentRegistry

        keys = {
            ExperimentRegistry.holdout_key("s", {"syms": {"AAA", "BBB", "CCC"}}, "fp")
            for _ in range(5)
        }
        assert len(keys) == 1

    def test_a_set_and_the_equivalent_sorted_list_agree(self):
        from signalforge.learning.registry import ExperimentRegistry

        assert ExperimentRegistry.holdout_key(
            "s", {"syms": {"BBB", "AAA"}}, "fp"
        ) == ExperimentRegistry.holdout_key("s", {"syms": ["AAA", "BBB"]}, "fp")

    def test_a_nested_decimal_is_normalised_too(self):
        from signalforge.learning.registry import ExperimentRegistry

        assert ExperimentRegistry.holdout_key(
            "s", {"outer": {"w": D("0.10")}}, "fp"
        ) == ExperimentRegistry.holdout_key("s", {"outer": {"w": D("0.1")}}, "fp")

    def test_genuinely_different_parameters_still_differ(self):
        from signalforge.learning.registry import ExperimentRegistry

        assert ExperimentRegistry.holdout_key(
            "s", {"w": D("0.05")}, "fp"
        ) != ExperimentRegistry.holdout_key("s", {"w": D("0.06")}, "fp")


# ======================================================================
# M2 — fingerprint stability
# ======================================================================
class TestFingerprintIgnoresDecimalScale:
    def test_the_same_prices_at_different_scales_fingerprint_identically(self):
        from signalforge.backtesting.engine import BacktestEngine
        from signalforge.core.history import PriceHistory

        plain = PriceHistory.from_bars(
            [make_bar("AAA", d, D("100"), volume=1_000) for d in DAYS[:5]]
        )
        scaled = PriceHistory.from_bars(
            [make_bar("AAA", d, D("100.000000"), volume=1_000) for d in DAYS[:5]]
        )
        assert BacktestEngine.fingerprint(
            plain, DAYS[0], DAYS[4]
        ) == BacktestEngine.fingerprint(scaled, DAYS[0], DAYS[4])

    def test_a_genuine_price_change_still_changes_the_fingerprint(self):
        from signalforge.backtesting.engine import BacktestEngine
        from signalforge.core.history import PriceHistory

        a = PriceHistory.from_bars(
            [make_bar("AAA", d, D("100"), volume=1_000) for d in DAYS[:5]]
        )
        b = PriceHistory.from_bars(
            [make_bar("AAA", d, D("100.000001"), volume=1_000) for d in DAYS[:5]]
        )
        assert BacktestEngine.fingerprint(a, DAYS[0], DAYS[4]) != BacktestEngine.fingerprint(
            b, DAYS[0], DAYS[4]
        )

    def test_a_warehouse_round_trip_preserves_the_fingerprint(self):
        """The case that matters: load via two paths, get one fingerprint."""
        from signalforge.backtesting.engine import BacktestEngine
        from signalforge.core.history import PriceHistory

        bars = [make_bar("AAA", d, D("100"), volume=1_000) for d in DAYS[:5]]
        direct = PriceHistory.from_bars(bars)
        wh = Warehouse(":memory:")
        try:
            wh.upsert_bars(bars)
            reloaded = PriceHistory.from_bars(wh.load_bars(symbols=["AAA"]))
        finally:
            wh.close()
        assert BacktestEngine.fingerprint(
            direct, DAYS[0], DAYS[4]
        ) == BacktestEngine.fingerprint(reloaded, DAYS[0], DAYS[4])


# ======================================================================
# M3 — volume_known must survive persistence
# ======================================================================
def unknown_volume_bar(day: date) -> Bar:
    return Bar(
        symbol="IDX",
        trade_date=day,
        open=D("100"),
        high=D("100"),
        low=D("100"),
        close=D("100"),
        volume=0,
        adj_close=D("100"),
        source="fred",
        volume_known=False,
    )


class TestVolumeKnownSurvivesTheWarehouse:
    def test_it_round_trips_rather_than_defaulting_to_true(self):
        wh = Warehouse(":memory:")
        try:
            wh.upsert_bars([unknown_volume_bar(DAYS[0])])
            loaded = wh.load_bars(symbols=["IDX"])
            assert len(loaded) == 1
            assert loaded[0].volume_known is False, (
                "'nobody told us' was persisted as 'nothing traded', which makes "
                "the liquidity breaker block every buy and the participation cap 0"
            )
        finally:
            wh.close()

    def test_an_ordinary_bar_still_round_trips_as_known(self):
        wh = Warehouse(":memory:")
        try:
            wh.upsert_bars([make_bar("AAA", DAYS[0], D("10"), volume=500, source="s")])
            assert wh.load_bars(symbols=["AAA"])[0].volume_known is True
        finally:
            wh.close()

    def test_a_genuine_zero_volume_bar_stays_known(self):
        wh = Warehouse(":memory:")
        try:
            wh.upsert_bars([make_bar("AAA", DAYS[0], D("10"), volume=0, source="s")])
            loaded = wh.load_bars(symbols=["AAA"])[0]
            assert loaded.volume == 0
            assert loaded.volume_known is True
        finally:
            wh.close()

    def test_the_liquidity_breaker_behaves_the_same_before_and_after_storage(self):
        from signalforge.config.models import ExecutionConfig, RiskConfig
        from signalforge.core.history import PriceHistory
        from signalforge.risk import breakers

        bars = [unknown_volume_bar(d) for d in DAYS[:10]]
        before = PriceHistory.from_bars(bars)
        wh = Warehouse(":memory:")
        try:
            wh.upsert_bars(bars)
            after = PriceHistory.from_bars(wh.load_bars(symbols=["IDX"]))
        finally:
            wh.close()
        assert breakers.check_liquidity("IDX", before.view(DAYS[9])) is None
        assert breakers.check_liquidity("IDX", after.view(DAYS[9])) is None


# ======================================================================
# M4 — realized P&L of a partially closed position
# ======================================================================
class TestRealizedPnlIncludesOpenPositions:
    def test_a_partial_sell_realizes_pnl_on_the_portfolio(self):
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(
            Fill(order_id="b", fill_id="b", symbol="AAA", side=Side.BUY, quantity=10,
                 price=D("100"), fees=D("0"), slippage_cost=D("0"), fill_date=DAYS[0])
        )
        pf.apply_fill(
            Fill(order_id="s", fill_id="s", symbol="AAA", side=Side.SELL, quantity=4,
                 price=D("150"), fees=D("0"), slippage_cost=D("0"), fill_date=DAYS[1])
        )
        assert pf.realized_pnl == D("200"), (
            "a partially closed position's realized gain was invisible on the "
            "portfolio, so the equity curve's realized/unrealized split did not "
            "reconcile to equity"
        )

    def test_the_realized_unrealized_split_reconciles_to_equity(self):
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(
            Fill(order_id="b", fill_id="b", symbol="AAA", side=Side.BUY, quantity=10,
                 price=D("100"), fees=D("0"), slippage_cost=D("0"), fill_date=DAYS[0])
        )
        pf.apply_fill(
            Fill(order_id="s", fill_id="s", symbol="AAA", side=Side.SELL, quantity=4,
                 price=D("150"), fees=D("0"), slippage_cost=D("0"), fill_date=DAYS[1])
        )
        marks = {"AAA": D("120")}
        assert (
            D("100000") + pf.realized_pnl + pf.unrealized_pnl(marks)
            == pf.equity(marks)
        )

    def test_closing_fully_does_not_double_count(self):
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(
            Fill(order_id="b", fill_id="b", symbol="AAA", side=Side.BUY, quantity=10,
                 price=D("100"), fees=D("0"), slippage_cost=D("0"), fill_date=DAYS[0])
        )
        pf.apply_fill(
            Fill(order_id="s1", fill_id="s1", symbol="AAA", side=Side.SELL, quantity=4,
                 price=D("150"), fees=D("0"), slippage_cost=D("0"), fill_date=DAYS[1])
        )
        pf.apply_fill(
            Fill(order_id="s2", fill_id="s2", symbol="AAA", side=Side.SELL, quantity=6,
                 price=D("150"), fees=D("0"), slippage_cost=D("0"), fill_date=DAYS[2])
        )
        assert pf.realized_pnl == D("500")
        assert "AAA" not in pf.positions
        assert pf.cash == D("100500")

    def test_the_equity_identity_is_unaffected(self):
        pf = Portfolio(cash=D("100000"))
        pf.apply_fill(
            Fill(order_id="b", fill_id="b", symbol="AAA", side=Side.BUY, quantity=10,
                 price=D("100"), fees=D("1"), slippage_cost=D("0"), fill_date=DAYS[0])
        )
        pf.apply_fill(
            Fill(order_id="s", fill_id="s", symbol="AAA", side=Side.SELL, quantity=4,
                 price=D("150"), fees=D("1"), slippage_cost=D("0"), fill_date=DAYS[1])
        )
        marks = {"AAA": D("120")}
        assert pf.equity(marks) == pf.cash + pf.gross_exposure(marks)

    def test_a_default_config_backtest_reconciles_on_every_point(self):
        """The reachable case: a sell partially filled at the participation cap."""
        from signalforge.backtesting.engine import BacktestEngine
        from signalforge.config.models import ResearchConfig, RiskConfig
        from signalforge.core.history import PriceHistory
        from signalforge.core.interfaces import Signal, SignalDirection

        # Thin volume ONLY on the exit's fill day, so the sell is partially
        # filled at the participation cap and the position stays open with a
        # realized leg. That is the state that did not reconcile.
        hist = PriceHistory.from_bars(
            [
                make_bar(
                    "AAA", d, D(100 + i),
                    volume=2_000 if i == 9 else 10_000_000,
                )
                for i, d in enumerate(DAYS)
            ]
        )

        class EnterThenExit:
            strategy_id = "ee"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                if as_of == DAYS[1]:
                    direction = SignalDirection.ENTER_LONG
                elif as_of == DAYS[8]:
                    direction = SignalDirection.EXIT_LONG
                else:
                    return []
                return [
                    Signal(
                        symbol="AAA",
                        direction=direction,
                        as_of=as_of,
                        strategy_id="ee",
                        reason="scripted",
                        target_weight=(
                            D("0.05") if direction is SignalDirection.ENTER_LONG else D("0")
                        ),
                    )
                ]

        research = ResearchConfig(starting_cash=D("100000"), universe=("AAA",))
        result = BacktestEngine(research, RiskConfig()).run(EnterThenExit(), hist)
        # A sell is partially filled at the participation cap, so this run
        # holds a partially closed position: exactly the case that did not
        # reconcile before.
        statuses = {o.status.value for o in result.orders}
        assert "PARTIALLY_FILLED" in statuses, "setup check: expected a partial fill"
        for point in result.equity_curve:
            assert point.equity == point.cash + point.gross_exposure
            # Buy fees live in the cost basis and sell fees in realized, so
            # there is no separate fee term: the identity is exact.
            assert (
                result.starting_cash + point.realized_pnl + point.unrealized_pnl
                == point.equity
            ), f"split does not reconcile on {point.valuation_date}"
