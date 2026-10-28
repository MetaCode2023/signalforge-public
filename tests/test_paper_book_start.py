"""A paper book starts on a session the operator chose.

Replaying all of history by default is a decision someone should make, not an
accident of the first run. The first session is therefore explicit, persisted
with the book, and never changed afterwards. Bars before it are indicator
warm-up only: no session, no corporate action, no breaker state.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.types import CorporateAction
from signalforge.data.storage import Warehouse
from signalforge.paper.ledger import EventType
from signalforge.paper.runner import PaperRunner, RunnerRefusal, ensure_schema

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 60)
SPEC = ("ma_trend", {
    "fast_window": 5,
    "slow_window": 15,
    "target_weight": D("0.05"),
    "symbols": ("AAA",),
})
HOLD = ("buy_and_hold", {"symbols": ("AAA",), "target_weight": D("0.05")})


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "start.duckdb"))
    yield w
    w.close()


@pytest.fixture
def history() -> PriceHistory:
    return PriceHistory.from_bars(
        [make_bar("AAA", d, D(100 + i), volume=10_000_000) for i, d in enumerate(DAYS)]
    )


def flat(closes: dict[int, str] | None = None, split_at: int | None = None) -> PriceHistory:
    bars = []
    for i, d in enumerate(DAYS):
        price = D(100) if split_at is None or i < split_at else D(50)
        if closes and i in closes:
            price = D(closes[i])
        bars.append(make_bar("AAA", d, price, volume=10_000_000))
    return PriceHistory.from_bars(bars)


def runner(warehouse, spec=SPEC, **kw) -> PaperRunner:
    return PaperRunner(
        warehouse=warehouse,
        research=ResearchConfig(starting_cash=D("100000"), universe=("AAA",)),
        risk=RiskConfig(),
        override_strategy=spec,
        override_reason="test",
        **kw,
    )


def count(warehouse, table) -> int:
    return warehouse.con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]


def columns(warehouse, table) -> set[str]:
    return {
        r[0]
        for r in warehouse.con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
        ).fetchall()
    }


class TestSchema:
    def test_ensure_schema_creates_paper_books_and_new_session_columns(self, warehouse):
        ensure_schema(warehouse.con)
        ensure_schema(warehouse.con)
        assert columns(warehouse, "paper_books") == {"book_id", "first_session", "created_at"}
        assert {"parameters", "paused_reason", "state_digest"} <= columns(
            warehouse, "paper_sessions"
        )

    def test_an_existing_sessions_table_gains_the_new_columns(self, warehouse):
        warehouse.con.execute(
            "CREATE TABLE paper_sessions (book_id VARCHAR NOT NULL, session_date DATE NOT NULL, "
            "strategy_id VARCHAR NOT NULL, equity DECIMAL(18,6), cash DECIMAL(18,6), "
            "gross_exposure DECIMAL(18,6), position_count BIGINT, fills BIGINT, "
            "completed_at TIMESTAMP, PRIMARY KEY (book_id, session_date))"
        )
        ensure_schema(warehouse.con)
        assert {"parameters", "paused_reason", "state_digest"} <= columns(
            warehouse, "paper_sessions"
        )


class TestTheStartIsExplicit:
    def test_a_new_book_without_start_is_refused(self, warehouse, history):
        r = runner(warehouse)
        with pytest.raises(RunnerRefusal) as excinfo:
            r.run(history, through=DAYS[30])
        message = str(excinfo.value)
        assert "--start" in message and "YYYY-MM-DD" in message
        assert "never the default" in message
        assert count(warehouse, "paper_sessions") == 0
        assert count(warehouse, "paper_runs") == 0
        assert count(warehouse, "paper_books") == 0

    def test_the_approval_refusal_still_comes_first(self, warehouse, history):
        r = PaperRunner(
            warehouse=warehouse,
            research=ResearchConfig(starting_cash=D("100000"), universe=("AAA",)),
            risk=RiskConfig(),
        )
        with pytest.raises(RunnerRefusal, match="approved"):
            r.run(history, through=DAYS[30])

    def test_a_start_that_is_not_a_trading_day_is_refused_naming_the_next(
        self, warehouse, history
    ):
        saturday = date(2020, 1, 4)
        assert saturday not in history.trading_days
        r = runner(warehouse, start=saturday)
        with pytest.raises(RunnerRefusal) as excinfo:
            r.run(history, through=DAYS[30])
        assert "not a trading day" in str(excinfo.value)
        assert str(DAYS[3]) in str(excinfo.value)
        assert count(warehouse, "paper_sessions") == 0

    def test_a_start_after_the_last_bar_is_refused(self, warehouse, history):
        r = runner(warehouse, start=date(2021, 6, 1))
        with pytest.raises(RunnerRefusal, match="after the last bar") as excinfo:
            r.run(history, through=date(2021, 6, 2))
        assert str(DAYS[-1]) in str(excinfo.value)
        assert count(warehouse, "paper_books") == 0

    def test_a_start_after_through_is_refused(self, warehouse, history):
        r = runner(warehouse, start=DAYS[30])
        with pytest.raises(RunnerRefusal, match="after"):
            r.run(history, through=DAYS[20])
        assert count(warehouse, "paper_sessions") == 0


class TestSessionsBeginAtStart:
    def test_sessions_begin_at_start(self, warehouse, history):
        r = runner(warehouse, start=DAYS[20])
        result = r.run(history, through=DAYS[30])
        assert result.sessions == 11
        assert result.first_session == DAYS[20]
        assert all(e.event_date >= DAYS[20] for e in r.broker.ledger.events)
        assert [s.statement_date for s in r.broker.statements][0] == DAYS[20]

    def test_bars_before_start_are_indicator_warm_up(self, warehouse, history):
        r = runner(warehouse, start=DAYS[15])
        r.run(history, through=DAYS[25])
        submitted = [
            e for e in r.broker.ledger.events if e.event_type is EventType.ORDER_SUBMITTED
        ]
        assert submitted and submitted[0].event_date == DAYS[15]

    def test_no_corporate_action_is_booked_before_start(self, warehouse):
        split = CorporateAction.split("AAA", DAYS[10], D("2"), "test")
        r = runner(warehouse, spec=HOLD, corporate_actions=[split], start=DAYS[12])
        r.run(flat(split_at=10), through=DAYS[20])
        assert count(warehouse, "paper_corporate_actions") == 0
        assert not [
            e for e in r.broker.ledger.events if e.event_type is EventType.CORPORATE_ACTION
        ]

    def test_no_breaker_state_before_start(self, warehouse):
        crash = {i: "50" for i in range(5, 60)}
        r = runner(warehouse, spec=HOLD, start=DAYS[10])
        r.run(flat(closes=crash), through=DAYS[15])
        first = r.broker.statements[0]
        assert first.peak_equity == D("100000")
        assert first.drawdown == D("0")


class TestTheStartIsPersisted:
    def test_the_book_start_is_persisted_with_the_first_session(self, warehouse, history):
        r = runner(warehouse, start=DAYS[20])
        r.run(history, through=DAYS[25])
        assert warehouse.con.execute(
            "SELECT book_id, first_session FROM paper_books"
        ).fetchall() == [("series:NASDAQCOM", DAYS[20])]
        assert r.book_start() == DAYS[20]

    def test_a_different_start_on_an_existing_book_is_refused(self, warehouse, history):
        runner(warehouse, start=DAYS[5]).run(history, through=DAYS[10])
        different = runner(warehouse, start=DAYS[6])
        with pytest.raises(RunnerRefusal) as excinfo:
            different.run(history, through=DAYS[12])
        assert str(DAYS[5]) in str(excinfo.value)
        assert count(warehouse, "paper_sessions") == 6

    def test_the_same_start_on_an_existing_book_is_accepted(self, warehouse, history):
        runner(warehouse, start=DAYS[5]).run(history, through=DAYS[10])
        same = runner(warehouse, start=DAYS[5])
        assert same.resolve_start(history, DAYS[12]) == DAYS[5]

    def test_an_existing_book_resolves_its_recorded_start_without_one(self, warehouse, history):
        runner(warehouse, start=DAYS[5]).run(history, through=DAYS[10])
        assert runner(warehouse).resolve_start(history, DAYS[12]) == DAYS[5]

    def test_a_refused_run_writes_no_paper_books_row(self, warehouse):
        split_history = flat(split_at=10)
        r = runner(warehouse, spec=HOLD, raw_basis=True, start=DAYS[0])
        with pytest.raises(RunnerRefusal, match="no split or adjustment recorded"):
            r.run(split_history, through=DAYS[12])
        assert count(warehouse, "paper_books") == 0
        assert count(warehouse, "paper_sessions") == 0

    def test_book_start_falls_back_to_the_first_session_without_a_books_row(
        self, warehouse, history
    ):
        r = runner(warehouse, start=DAYS[5])
        r.run(history, through=DAYS[8])
        warehouse.con.execute("DELETE FROM paper_books")
        assert r.book_start() == DAYS[5]


class TestWarmUpNote:
    def test_a_short_warm_up_is_a_note_not_a_refusal(self, warehouse, history):
        r = runner(warehouse, start=DAYS[2])
        result = r.run(history, through=DAYS[6])
        assert result.sessions == 5
        assert any(n.startswith("WARM-UP SHORT") for n in result.notes)
        later = r.run(history, through=DAYS[9])
        assert not any(n.startswith("WARM-UP SHORT") for n in later.notes)

    def test_a_sufficient_warm_up_has_no_note(self, warehouse, history):
        result = runner(warehouse, start=DAYS[20]).run(history, through=DAYS[22])
        assert not any(n.startswith("WARM-UP SHORT") for n in result.notes)


class TestRunResult:
    def test_run_result_carries_the_book_start(self, warehouse, history):
        result = runner(warehouse, start=DAYS[20]).run(history, through=DAYS[22])
        assert result.book_start == DAYS[20]
