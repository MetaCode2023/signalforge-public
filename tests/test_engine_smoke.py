"""Small engine checks that need only the existing config dependencies."""

import socket
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from signalforge.backtesting.engine import BacktestEngine
from signalforge.backtesting.metrics import compute_metrics
from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.errors import ConfigError
from signalforge.core.history import PriceHistory
from signalforge.core.types import Bar
from signalforge.strategies.benchmark import BuyAndHold


class EngineSmokeTests(unittest.TestCase):
    def setUp(self):
        days = []
        day = date(2020, 1, 1)
        while len(days) < 300:
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)
        self.days = days
        self.history = PriceHistory.from_bars([
            Bar(symbol="TEST", trade_date=d, open=Decimal(100), high=Decimal(100),
                low=Decimal(100), close=Decimal(100), volume=1_000_000,
                adj_close=Decimal(100), source="fixture") for d in days
        ])
        self.research = ResearchConfig(universe=("TEST",))

    def run_engine(self):
        return BacktestEngine(self.research, RiskConfig()).run(
            BuyAndHold(("TEST",), Decimal("0.05")), self.history,
        )

    def test_run_is_socket_free(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
             patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            self.assertGreater(len(self.run_engine().fills), 0)

    def test_fills_are_later_than_signal_and_costed(self):
        result = self.run_engine()
        self.assertEqual(len(result.fills), 1)
        fill = result.fills[0]
        order = next(o for o in result.orders if o.order_id == fill.order_id)
        self.assertGreater(fill.fill_date, order.signal_date)
        self.assertEqual(fill.price, Decimal("100.05"))
        self.assertEqual(fill.fees, max(Decimal("1"), Decimal("0.005") * fill.quantity))
        self.assertEqual(fill.slippage_cost, Decimal("0.05") * fill.quantity)

    def test_cash_and_equity_reconcile_exactly(self):
        result = self.run_engine()
        for point in result.equity_curve:
            self.assertGreaterEqual(point.cash, 0)
            self.assertEqual(point.equity, point.cash + point.gross_exposure)
        fill = result.fills[0]
        self.assertEqual(result.final_equity, result.starting_cash - fill.fees - fill.slippage_cost)

    def test_repeat_is_deterministic(self):
        left, right = self.run_engine(), self.run_engine()
        self.assertEqual(left.equity_curve, right.equity_curve)
        self.assertEqual(left.fills, right.fills)
        self.assertEqual(left.dataset_fingerprint, right.dataset_fingerprint)

    def test_history_view_has_no_future_bars(self):
        as_of = self.days[20]
        view = self.history.view(as_of)
        self.assertTrue(all(b.trade_date <= as_of for b in view.bars("TEST")))
        self.assertEqual(view.latest("TEST").trade_date, as_of)

    def test_insufficient_trade_evidence_is_labelled(self):
        metrics = compute_metrics(self.run_engine(), config=self.research)
        self.assertEqual(metrics.evidence.value, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(metrics.fill_count, 1)

    def test_paper_mode_cannot_be_disabled(self):
        with self.assertRaises(ConfigError):
            ResearchConfig(paper_only=False)


if __name__ == "__main__":
    unittest.main()
