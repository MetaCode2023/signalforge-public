"""A real paper run, rendered on the real page.

The dashboard has had a PAPER SIMULATION provenance class since it was built,
with a CSS rule, a legend entry and a `paper_runs=` argument on
`render_dashboard`. It had never rendered, and the reason turned out to be
structural rather than an oversight: `PaperRunner.run` returns a `RunResult`
(strategy id, approval source, session count, dates, notes) and
`render_dashboard` needs a `BacktestResult`. Nothing converted one into the
other, so the only way to exercise the class was to hand-build a fake, which
is what `test_dashboard.py` did -- and a fake cannot tell you whether a real
paper run would render.

Two things came out of wiring it up for real.

* **The paper broker kept no order log.** The backtest engine accumulates
  every order with its final status; the broker recorded order events in the
  ledger and held only unfilled ones in `_pending`, so the submitted,
  rejected and expired orders could not be recovered as `Order` objects. The
  dashboard's risk section reads `result.orders`, so a paper run rendered
  with an empty order list and an order count of zero beside a non-zero fill
  count. `PaperBroker.orders` now mirrors the engine.

* **`RunResult` carried no result.** It now carries `result`, a
  `BacktestResult` built from the broker's own statements, so the wiring is
  `render_dashboard(paper_runs=[run.result])` rather than a caller
  reconstructing an equity curve by hand.
"""

import os
from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ResearchConfig, RiskConfig, load_all
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import BacktestResult, RiskAction
from signalforge.core.types import OrderStatus
from signalforge.dashboard.render import render_dashboard
from signalforge.data.archive import load_archive_history
from signalforge.data.providers import AAPL_FILE, DEFAULT_ARCHIVE_DIR
from signalforge.data.storage import Warehouse
from signalforge.paper.runner import PaperRunner, record_active_strategy
from signalforge.strategies.registry import build

from tests.conftest import business_days, make_bar

D = Decimal
OVERRIDE_REASON = (
    "overnight verification that the PAPER SIMULATION provenance class renders "
    "from an actual paper run; not an approval"
)


def research_config(universe) -> ResearchConfig:
    return ResearchConfig(starting_cash=D("100000"), universe=tuple(universe))


def bundle_for(research: ResearchConfig):
    """The real config bundle with this run's research config swapped in.

    A stub object is not enough: the dashboard's data-health section reads
    `config.data_sources.providers`, so a fake with only `research` and
    `risk` raises inside the renderer rather than rendering.
    """
    return load_all().model_copy(update={"research": research})


@pytest.fixture
def warehouse():
    w = Warehouse(":memory:")
    yield w
    w.close()


@pytest.fixture(scope="module")
def synthetic() -> PriceHistory:
    """200 sessions with enough shape for a 20/60 crossover to trade."""
    days = business_days(date(2020, 1, 1), 200)
    return PriceHistory.from_bars(
        [
            make_bar("AAA", day,
                     D(100) + D(index % 37) - D(index % 13) + D(index) / D(20),
                     volume=10_000_000)
            for index, day in enumerate(days)
        ]
    )


@pytest.fixture(scope="module")
def aapl() -> PriceHistory:
    path = os.path.join(str(DEFAULT_ARCHIVE_DIR), f"{AAPL_FILE}.csv")
    if not os.path.isfile(path):
        pytest.skip(f"{path} is licensed data and is not in the repo")
    return load_archive_history(datasets=[AAPL_FILE])


MA_20_60 = {"fast_window": 20, "slow_window": 60, "target_weight": "0.05"}


def drive(warehouse, history, symbol, *, approved=False):
    research = research_config(history.symbols)
    params = dict(MA_20_60, symbols=[symbol])
    if approved:
        record_active_strategy(
            warehouse, strategy_id="ma_trend", parameters=params,
            approved_by="the owner", reason="test approval",
        )
        runner = PaperRunner(warehouse=warehouse, research=research, risk=RiskConfig())
    else:
        runner = PaperRunner(
            warehouse=warehouse, research=research, risk=RiskConfig(),
            override_strategy=("ma_trend", params),
            override_reason=OVERRIDE_REASON,
        )
    run = runner.run(history, through=history.trading_days[-1])
    return runner, run, research


# --------------------------------------------------------------------------
# The broker's order log
# --------------------------------------------------------------------------
class TestThePaperBrokerKeepsAnOrderLog:
    def test_every_submitted_order_is_recoverable_with_its_final_status(
        self, warehouse, synthetic
    ):
        runner, run, _ = drive(warehouse, synthetic, "AAA")
        orders = runner.broker.orders
        assert orders, "a paper run that traded recorded no orders"
        assert len(orders) >= len(runner.broker.portfolio().fills)
        assert all(o.status is not OrderStatus.NEW for o in orders)
        assert {o.status for o in orders} <= {
            OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
            OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.ACCEPTED,
        }

    def test_the_filled_orders_match_the_fills(self, warehouse, synthetic):
        runner, _, _ = drive(warehouse, synthetic, "AAA")
        filled = [
            o for o in runner.broker.orders
            if o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
        ]
        fills = runner.broker.portfolio().fills
        assert len(filled) == len(fills)
        assert {o.order_id for o in filled} == {f.order_id for f in fills}

    def test_an_order_still_queued_at_the_end_is_accepted_not_filled(
        self, warehouse, synthetic
    ):
        """A one-session fill delay always leaves the last order pending."""
        runner, _, _ = drive(warehouse, synthetic, "AAA")
        pending = [o for o in runner.broker.orders if o.status is OrderStatus.ACCEPTED]
        assert len(pending) == runner.broker.pending_count

    def test_the_log_is_append_only_and_holds_each_order_once(
        self, warehouse, synthetic
    ):
        runner, _, _ = drive(warehouse, synthetic, "AAA")
        ids = [o.order_id for o in runner.broker.orders]
        assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# RunResult carries a renderable result
# --------------------------------------------------------------------------
class TestTheRunCarriesARenderableResult:
    def test_the_run_result_holds_a_backtest_result(self, warehouse, synthetic):
        _, run, _ = drive(warehouse, synthetic, "AAA")
        assert isinstance(run.result, BacktestResult)
        assert run.result.strategy_id == "ma_trend"
        assert run.result.run_id.startswith("paper:")

    def test_its_equity_curve_is_the_brokers_own_statements(self, warehouse, synthetic):
        runner, run, _ = drive(warehouse, synthetic, "AAA")
        statements = runner.broker.statements
        assert len(run.result.equity_curve) == len(statements) == run.sessions
        for point, statement in zip(run.result.equity_curve, statements):
            assert point.valuation_date == statement.statement_date
            assert point.equity == statement.equity
            assert point.cash == statement.cash
            assert point.gross_exposure == statement.gross_exposure
            assert point.drawdown == statement.drawdown
            assert point.peak_equity == statement.peak_equity

    def test_it_carries_the_fills_orders_and_risk_events(self, warehouse, synthetic):
        runner, run, _ = drive(warehouse, synthetic, "AAA")
        assert run.result.fills == list(runner.broker.portfolio().fills)
        assert run.result.orders == list(runner.broker.orders)
        assert run.result.risk_events == list(runner.broker.risk_events)

    def test_the_override_travels_with_the_result_as_a_limitation(
        self, warehouse, synthetic
    ):
        """An unapproved strategy must say so wherever its numbers appear."""
        _, run, _ = drive(warehouse, synthetic, "AAA")
        joined = " ".join(run.result.data_limitations)
        assert "UNAPPROVED" in joined
        assert OVERRIDE_REASON in joined
        assert "PAPER" in joined.upper()

    def test_an_approved_run_says_who_approved_it(self, warehouse, synthetic):
        _, run, _ = drive(warehouse, synthetic, "AAA", approved=True)
        joined = " ".join(run.result.data_limitations)
        assert "UNAPPROVED" not in joined
        assert "the owner" in joined

    def test_the_result_is_accounting_consistent(self, warehouse, synthetic):
        _, run, _ = drive(warehouse, synthetic, "AAA")
        final = run.result.equity_curve[-1]
        assert final.equity == final.cash + final.gross_exposure
        assert run.result.starting_cash == D("100000")
        assert run.result.final_equity == final.equity

    def test_a_run_with_no_due_sessions_carries_no_result_rather_than_a_blank_one(
        self, warehouse, synthetic
    ):
        """Resuming when nothing is due must not render an empty curve.

        An equity curve of zero points through compute_metrics is a page that
        says 0.00% rather than "nothing happened".
        """
        runner, first, _ = drive(warehouse, synthetic, "AAA")
        assert first.sessions == len(synthetic.trading_days)
        again = runner.run(synthetic, through=synthetic.trading_days[-1])
        assert again.sessions == 0
        assert again.result is None


# --------------------------------------------------------------------------
# The two provenance classes on one real page
# --------------------------------------------------------------------------
class TestThePageDistinguishesPaperFromBacktest:
    @pytest.fixture
    def page(self, warehouse, synthetic):
        _, run, research = drive(warehouse, synthetic, "AAA")
        backtest = BacktestEngine(research, RiskConfig()).run(
            build("ma_trend", fast_window=20, slow_window=60,
                  target_weight=D("0.05"), symbols=("AAA",)),
            synthetic, run_id="backtest:ma_trend",
        )
        html = render_dashboard(
            warehouse=warehouse,
            backtests=[backtest],
            paper_runs=[run.result],
            config=bundle_for(research),
        )
        return html, run, backtest

    def test_both_provenance_labels_appear(self, page):
        html, _, _ = page
        assert "PAPER SIMULATION" in html
        assert "HISTORICAL BACKTEST" in html

    def test_each_run_carries_its_own_label_not_one_shared_legend_entry(self, page):
        html, _, _ = page
        # Two in the legend, at least one more per run on the page itself.
        assert html.count("PAPER SIMULATION") >= 2
        assert html.count("HISTORICAL BACKTEST") >= 2

    def test_the_two_classes_are_visually_distinct_in_the_stylesheet(self, page):
        html, _, _ = page
        assert 'class="key prov-paper"' in html
        assert 'class="key prov-backtest"' in html
        assert ".prov-backtest{border:2px solid" in html
        assert ".prov-paper{border:2px dashed" in html
        # Different colour variables, not just a different border style.
        assert "var(--accent)" in html and "var(--warn)" in html

    def test_the_paper_run_id_is_on_the_page(self, page):
        html, run, _ = page
        assert run.result.run_id in html

    def test_the_backtest_run_id_is_on_the_page_too(self, page):
        html, _, backtest = page
        assert backtest.run_id in html

    def test_the_unapproved_override_is_visible_on_the_page(self, page):
        html, _, _ = page
        assert "UNAPPROVED" in html

    def test_the_page_never_claims_real_money(self, page):
        html, _, _ = page
        lowered = html.lower()
        assert "real money" not in lowered or "not real money" in lowered
        assert "paper" in lowered


class TestARealPaperRunOnRealBars:
    """The same wiring on the AAPL sample, which is skipped in any clone."""

    @pytest.fixture
    def driven(self, warehouse, aapl):
        return drive(warehouse, aapl, "AAPL")

    def test_the_run_covers_every_session_in_the_sample(self, driven, aapl):
        _, run, _ = driven
        assert run.sessions == 506
        assert run.first_session == date(2015, 2, 17)
        assert run.last_session == date(2017, 2, 16)
        assert run.approval_source == "override"

    def test_it_traded_and_the_result_reflects_it(self, driven):
        runner, run, _ = driven
        assert run.result.fills
        assert run.result.orders
        assert len(run.result.equity_curve) == 506
        assert run.result.final_equity != run.result.starting_cash

    def test_the_real_page_shows_the_paper_class(self, driven, warehouse, aapl):
        runner, run, research = driven
        html = render_dashboard(
            warehouse=warehouse,
            paper_runs=[run.result],
            config=bundle_for(research),
        )
        assert "PAPER SIMULATION" in html
        assert 'class="key prov-paper"' in html
        assert run.result.run_id in html
        assert len(html) > 10_000, "the page rendered but is suspiciously small"

    def test_the_risk_section_counts_real_orders_not_zero(self, driven, warehouse):
        """The symptom of the missing order log: orders beside fills, as zero."""
        runner, run, research = driven
        html = render_dashboard(
            warehouse=warehouse,
            paper_runs=[run.result],
            config=bundle_for(research),
        )
        assert len(run.result.orders) > 0
        rejected = sum(
            1 for e in run.result.risk_events if e.action is RiskAction.REJECT
        )
        assert run.result.rejected_order_count == rejected
        assert "PAPER SIMULATION" in html
