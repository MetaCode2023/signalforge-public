"""The paper-trading runner.

`PaperBroker` is a library: something has to drive it, session by session.
That driver is where two new ways to be wrong appear, so both are pinned
here:

  1. **It must not pick a strategy nobody approved.** A loop that quietly
     selects one is the same failure as a strategy promoting itself, just
     moved one layer out. The runner refuses to start unless the registry
     holds an APPROVED promotion, or the operator explicitly overrides and
     the override is recorded.

  2. **It must be resumable without replaying trades.** A restart that
     re-runs yesterday's sessions would double-count every fill. Progress is
     persisted, and a second run over the same range is a no-op.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.errors import PromotionError
from signalforge.core.history import PriceHistory
from signalforge.data.storage import Warehouse
from signalforge.paper.runner import PaperRunner, RunnerRefusal

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 60)
SPEC = ("ma_trend", {
    "fast_window": 5,
    "slow_window": 15,
    "target_weight": D("0.05"),
    "symbols": ("AAA",),
})


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "paper.duckdb"))
    yield w
    w.close()


@pytest.fixture
def history() -> PriceHistory:
    return PriceHistory.from_bars(
        [make_bar("AAA", d, D(100 + i), volume=10_000_000) for i, d in enumerate(DAYS)]
    )


def runner(warehouse, **kw) -> PaperRunner:
    return PaperRunner(
        warehouse=warehouse,
        research=ResearchConfig(starting_cash=D("100000"), universe=("AAA",)),
        risk=RiskConfig(),
        **kw,
    )


class TestItRefusesAnUnapprovedStrategy:
    def test_it_will_not_start_with_no_approved_strategy(self, warehouse, history):
        r = runner(warehouse)
        with pytest.raises(RunnerRefusal) as excinfo:
            r.run(history, through=DAYS[30])
        assert "approved" in str(excinfo.value).lower()

    def test_the_refusal_names_what_would_unblock_it(self, warehouse, history):
        r = runner(warehouse)
        with pytest.raises(RunnerRefusal) as excinfo:
            r.run(history, through=DAYS[30])
        message = str(excinfo.value)
        assert "promotion" in message.lower() or "override" in message.lower()

    def test_an_explicit_override_is_allowed(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="operator dry run")
        result = r.run(history, through=DAYS[30])
        assert result.sessions > 0

    def test_an_override_without_a_reason_is_refused(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="")
        with pytest.raises(RunnerRefusal):
            r.run(history, through=DAYS[30])

    def test_the_override_is_recorded_in_the_ledger(self, warehouse, history):
        from signalforge.paper.ledger import EventType

        r = runner(warehouse, override_strategy=SPEC, override_reason="operator dry run")
        r.run(history, through=DAYS[20])
        overrides = [
            e for e in r.broker.ledger.events
            if e.event_type is EventType.PAUSED or "override" in str(e.payload).lower()
        ]
        assert overrides, "an unapproved strategy must leave a visible record"

    def test_the_override_is_recorded_in_the_warehouse(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="operator dry run")
        r.run(history, through=DAYS[20])
        rows = warehouse.con.execute(
            "SELECT count(*) FROM paper_runs WHERE approval_source = 'override'"
        ).fetchall()
        assert rows[0][0] >= 1


class TestItRunsSessions:
    def test_it_drives_one_session_per_trading_day(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        result = r.run(history, through=DAYS[20])
        assert result.sessions == 21

    def test_it_produces_a_statement_per_session(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.run(history, through=DAYS[20])
        assert len(r.broker.statements) == 21

    def test_the_equity_identity_holds_in_every_statement(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.run(history, through=DAYS[40])
        for s in r.broker.statements:
            assert s.equity == s.cash + s.gross_exposure
            assert s.cash >= 0

    def test_it_actually_trades_on_a_trending_series(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.run(history, through=DAYS[40])
        assert r.broker.portfolio().fills, "the runner drove no trades at all"

    def test_the_ledger_stays_verifiable(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.run(history, through=DAYS[40])
        assert r.broker.ledger.verify() is True

    def test_it_is_still_paper_only(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.run(history, through=DAYS[10])
        assert r.broker.is_paper is True


class TestItResumesWithoutReplaying:
    def test_progress_is_persisted(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.run(history, through=DAYS[20])
        assert r.last_completed_session(history) == DAYS[20]

    def test_a_second_run_over_the_same_range_does_nothing(self, warehouse, history):
        r1 = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r1.run(history, through=DAYS[20])
        r2 = runner(warehouse, override_strategy=SPEC, override_reason="test")
        result = r2.run(history, through=DAYS[20])
        assert result.sessions == 0, (
            "re-running a completed range would double-count every fill"
        )

    def test_restart_cannot_extend_without_restoring_the_book(self, warehouse, history):
        r1 = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r1.run(history, through=DAYS[20])
        r2 = runner(warehouse, override_strategy=SPEC, override_reason="test")
        with pytest.raises(RunnerRefusal, match="not restored"):
            r2.run(history, through=DAYS[25])
        assert warehouse.con.execute("SELECT count(*) FROM paper_sessions").fetchall()[0][0] == 21

    def test_a_resumed_run_reports_where_it_started(self, warehouse, history):
        r1 = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r1.run(history, through=DAYS[20])
        result = r1.run(history, through=DAYS[25])
        assert result.first_session == DAYS[21]


class TestPause:
    def test_a_paused_runner_opens_sessions_but_takes_no_new_risk(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.pause("operator halted")
        result = r.run(history, through=DAYS[40])
        assert result.sessions > 0
        assert r.broker.portfolio().quantity_of("AAA") == 0

    def test_resuming_lets_it_trade_again(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.pause("halted")
        r.run(history, through=DAYS[25])
        r.resume()
        r.run(history, through=DAYS[50])
        assert r.broker.portfolio().fills

    def test_the_pause_state_is_visible(self, warehouse, history):
        r = runner(warehouse, override_strategy=SPEC, override_reason="test")
        r.pause("maintenance")
        assert r.broker.status()["paused"] is True
        assert "maintenance" in r.broker.status()["pause_reason"]


class TestAnApprovedStrategyIsAccepted:
    def test_an_approved_promotion_in_the_registry_unblocks_the_runner(
        self, warehouse, history
    ):
        from signalforge.paper.runner import record_active_strategy

        record_active_strategy(
            warehouse,
            strategy_id="ma_trend",
            parameters={
                "fast_window": 5,
                "slow_window": 15,
                "target_weight": "0.05",
                "symbols": ["AAA"],
            },
            approved_by="austin",
            reason="approved for paper trading",
        )
        r = runner(warehouse)
        result = r.run(history, through=DAYS[30])
        assert result.sessions > 0
        assert result.approval_source == "promotion"

    def test_recording_an_active_strategy_requires_a_named_human(self, warehouse):
        from signalforge.paper.runner import record_active_strategy

        with pytest.raises(PromotionError):
            record_active_strategy(
                warehouse,
                strategy_id="ma_trend",
                parameters={},
                approved_by="",
                reason="no human",
            )

    def test_recording_an_active_strategy_requires_a_reason(self, warehouse):
        from signalforge.paper.runner import record_active_strategy

        with pytest.raises(Exception):
            record_active_strategy(
                warehouse,
                strategy_id="ma_trend",
                parameters={},
                approved_by="austin",
                reason="",
            )
