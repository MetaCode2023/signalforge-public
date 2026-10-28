"""A reservation must release exactly what it committed.

Found by code review, with measured reproduction. `_reserve` valued the
commitment at the SIGNAL day's close; `_release` valued it at the close of
the day before the fill. With `fill_delay_days == 1` those are the same dict
and the release is exact. With `fill_delay_days >= 2` (a supported, tested
config) they differ, and the residual accrues in `Reservations.cash` with
nothing to clear it.

Measured: after a run with every order filled and the queue empty, 9,060 of
phantom committed cash remained against a 95,457 book — buying power
understated by 9.5% with zero orders outstanding. The residual grows with
trade count, so a long run eventually rejects every buy with
`cash_available` while the log reads as if the book were out of money. The
`release` clamp at zero also silently discarded the error in the other
direction, leaving the book UNDER-reserved on a rising path.

The fix removes the whole class of bug rather than re-deriving the mark:
a reservation is keyed by order id and releases precisely the amount it
recorded, so no arithmetic can drift.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.risk.engine import Reservations

D = Decimal


class TestExactRelease:
    def test_releasing_returns_cash_to_zero(self):
        r = Reservations()
        r.add("o1", "AAA", 10, D("4000"))
        r.release("o1")
        assert r.cash == D("0")
        assert r.quantity_for("AAA") == 0

    def test_many_round_trips_leave_no_residual(self):
        r = Reservations()
        for i in range(50):
            r.add(f"o{i}", "AAA", 10, D("4000") - D(i))
            r.release(f"o{i}")
        assert r.cash == D("0"), f"residual {r.cash} after 50 exact round trips"

    def test_a_release_does_not_depend_on_a_recomputed_mark(self):
        # The whole point: the caller supplies no price at release time, so
        # there is nothing to get wrong.
        r = Reservations()
        r.add("o1", "AAA", 10, D("4000"))
        r.release("o1")  # no mark argument exists
        assert r.cash == D("0")

    def test_releasing_an_unknown_order_is_a_no_op(self):
        r = Reservations()
        r.add("o1", "AAA", 10, D("4000"))
        r.release("never-reserved")
        assert r.cash == D("4000")
        assert r.quantity_for("AAA") == 10

    def test_releasing_twice_does_not_double_credit(self):
        r = Reservations()
        r.add("o1", "AAA", 10, D("4000"))
        r.release("o1")
        r.release("o1")
        assert r.cash == D("0")
        assert r.quantity_for("AAA") == 0

    def test_cash_never_goes_negative_through_any_sequence(self):
        r = Reservations()
        r.add("o1", "AAA", 10, D("100"))
        r.add("o2", "AAA", 10, D("200"))
        r.release("o2")
        r.release("o1")
        r.release("o1")
        assert r.cash >= D("0")

    def test_concurrent_reservations_sum_and_unwind_cleanly(self):
        r = Reservations()
        r.add("o1", "AAA", 10, D("1000"))
        r.add("o2", "AAA", 5, D("500"))
        r.add("o3", "BBB", 7, D("700"))
        assert r.cash == D("2200")
        assert r.quantity_for("AAA") == 15
        assert r.symbols == {"AAA", "BBB"}
        r.release("o2")
        assert r.cash == D("1700")
        assert r.quantity_for("AAA") == 10
        r.release("o1")
        r.release("o3")
        assert r.cash == D("0")
        assert r.symbols == set()

    def test_a_symbol_drops_out_of_symbols_once_fully_released(self):
        r = Reservations()
        r.add("o1", "AAA", 10, D("1000"))
        r.release("o1")
        assert "AAA" not in r.symbols


class TestEngineLeavesNoResidual:
    @pytest.mark.parametrize("delay", [1, 2, 3, 5])
    def test_a_completed_run_strands_no_committed_cash(self, delay):
        """The measured failure, parameterised over the delay that caused it."""
        from signalforge.backtesting.engine import BacktestEngine
        from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
        from signalforge.core.history import PriceHistory
        from signalforge.core.interfaces import Signal, SignalDirection

        from tests.conftest import business_days, make_bar

        days = business_days(date(2020, 1, 1), 200)
        # A moving price, so a signal-day mark and a fill-day mark differ.
        hist = PriceHistory.from_bars(
            [
                make_bar("AAA", d, D(400) - D(i), volume=10_000_000)
                for i, d in enumerate(days)
            ]
        )

        class Churn:
            strategy_id = "churn"
            warmup_days = 0

            def parameters(self):
                return {}

            def generate_signals(self, as_of, view):
                direction = (
                    SignalDirection.ENTER_LONG
                    if (as_of.toordinal() // 5) % 2 == 0
                    else SignalDirection.EXIT_LONG
                )
                return [
                    Signal(
                        symbol="AAA",
                        direction=direction,
                        as_of=as_of,
                        strategy_id="churn",
                        reason="alternating",
                        target_weight=(
                            D("0.05")
                            if direction is SignalDirection.ENTER_LONG
                            else D("0")
                        ),
                    )
                ]

        research = ResearchConfig(
            starting_cash=D("100000"),
            universe=("AAA",),
            execution=ExecutionConfig(fill_delay_days=delay),
        )
        engine = BacktestEngine(research, RiskConfig())
        residual = engine.run_and_report_residual(Churn(), hist)
        assert residual == D("0"), (
            f"fill_delay_days={delay} left {residual} of phantom committed cash; "
            "buying power is understated by that amount for the rest of the run"
        )
