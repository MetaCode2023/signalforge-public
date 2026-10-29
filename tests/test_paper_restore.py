"""A paper book survives a process restart, and proves it did.

A restart used to refuse, because a fresh runner has a fresh-cash broker and
the persisted sessions say otherwise. The book is now restored by replaying
the persisted sessions in memory and checking every replayed session against
the digest and figures stored when it was traded. The pinned properties:

  1. A restart is invisible: two processes equal one uninterrupted run, down
     to the ledger and the stored rows.
  2. Replay writes nothing, and a corporate action is booked exactly once.
  3. Anything that changed since the sessions were traded (a figure, a bar,
     the configuration, a missing row) refuses the run and writes nothing.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.types import CorporateAction
from signalforge.data.storage import Warehouse
from signalforge.paper.ledger import EventType
from signalforge.paper.runner import PaperRunner, RunnerRefusal, record_active_strategy

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
SESSION_COLUMNS = (
    "book_id, session_date, strategy_id, equity, cash, gross_exposure, position_count, "
    "fills, parameters, paused_reason, state_digest"
)


def new_warehouse(tmp_path, name):
    return Warehouse(str(tmp_path / f"{name}.duckdb"))


@pytest.fixture
def warehouse(tmp_path):
    w = new_warehouse(tmp_path, "restore")
    yield w
    w.close()


@pytest.fixture
def other(tmp_path):
    w = new_warehouse(tmp_path, "other")
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


def make(warehouse, spec=SPEC, cash="100000", risk=None, **kw) -> PaperRunner:
    kw.setdefault("start", DAYS[0])
    if spec is not None:
        kw.setdefault("override_strategy", spec)
        kw.setdefault("override_reason", "test")
    return PaperRunner(
        warehouse=warehouse,
        research=ResearchConfig(starting_cash=D(cash), universe=("AAA",)),
        risk=risk or RiskConfig(),
        **kw,
    )


def sessions(warehouse):
    return warehouse.con.execute(
        f"SELECT {SESSION_COLUMNS} FROM paper_sessions ORDER BY session_date"
    ).fetchall()


def written(warehouse):
    return {
        table: warehouse.con.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in ("paper_sessions", "paper_runs", "paper_books", "paper_corporate_actions")
    }


def ledger_view(runner):
    return [
        (e.event_type, e.event_date, dict(e.payload))
        for e in runner.broker.ledger.events
        if e.event_type is not EventType.STRATEGY_OVERRIDE
    ]


def actions_of(runner):
    return [
        e for e in runner.broker.ledger.events if e.event_type is EventType.CORPORATE_ACTION
    ]


def split(day=DAYS[10]) -> CorporateAction:
    return CorporateAction.split("AAA", day, D("2"), "test")


class TestWhatIsStored:
    def test_session_rows_record_parameters_pause_and_digest(self, warehouse, history):
        r = make(warehouse)
        r.pause("operator halted")
        r.run(history, through=DAYS[3])
        rows = warehouse.con.execute(
            "SELECT parameters, paused_reason, state_digest FROM paper_sessions "
            "ORDER BY session_date"
        ).fetchall()
        assert len(rows) == 4
        for parameters, paused_reason, digest in rows:
            assert '"fast_window": 5' in parameters and '"target_weight": "0.05"' in parameters
            assert paused_reason == "operator halted"
            assert len(digest) == 64
        assert rows[-1][2] == r.broker.state_digest()

    def test_an_unpaused_session_stores_an_empty_reason(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[2])
        assert {r[0] for r in warehouse.con.execute(
            "SELECT paused_reason FROM paper_sessions"
        ).fetchall()} == {""}


class TestRestartContinuity:
    def test_two_process_continuity_equals_one_uninterrupted_run(
        self, warehouse, other, history
    ):
        r1 = make(warehouse)
        r1.run(history, through=DAYS[20])
        r2 = make(warehouse)
        r2.run(history, through=DAYS[45])
        single = make(other)
        single.run(history, through=DAYS[45])
        assert single.broker.portfolio().fills
        assert r2.broker.statements == single.broker.statements
        assert sessions(warehouse) == sessions(other)
        assert r2.broker.state_digest() == single.broker.state_digest()
        assert r2.broker.portfolio().fills == single.broker.portfolio().fills
        assert ledger_view(r2) == ledger_view(single)
        assert r2.broker.ledger.verify() is True

    def test_a_three_process_chain_equals_one_run(self, warehouse, other, history):
        make(warehouse).run(history, through=DAYS[20])
        make(warehouse).run(history, through=DAYS[30])
        r3 = make(warehouse)
        result = r3.run(history, through=DAYS[45])
        single = make(other)
        single.run(history, through=DAYS[45])
        assert result.restored_sessions == 31
        assert r3.broker.statements == single.broker.statements
        assert sessions(warehouse) == sessions(other)
        assert ledger_view(r3) == ledger_view(single)

    def test_a_no_op_restart_shows_the_restored_book(self, warehouse, history):
        r1 = make(warehouse)
        r1.run(history, through=DAYS[20])
        before = written(warehouse)
        r2 = make(warehouse)
        result = r2.run(history, through=DAYS[20])
        assert result.sessions == 0
        assert result.restored_sessions == 21
        assert r2.broker.status()["equity"] == r1.broker.status()["equity"]
        assert r2.broker.statements == r1.broker.statements
        sessions_after = written(warehouse)
        assert sessions_after["paper_sessions"] == before["paper_sessions"]
        assert sessions_after["paper_corporate_actions"] == before["paper_corporate_actions"]

    def test_the_run_that_traded_restores_nothing(self, warehouse, history):
        r = make(warehouse)
        assert r.run(history, through=DAYS[10]).restored_sessions == 0
        assert r.run(history, through=DAYS[12]).restored_sessions == 0

    def test_replay_writes_nothing(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[20])
        before = written(warehouse)
        make(warehouse).run(history, through=DAYS[20])
        after = written(warehouse)
        assert len(after.pop("paper_runs")) == len(before.pop("paper_runs")) + 1
        assert after == before


class TestTamperIsRefused:
    def refused(self, warehouse, history, through=DAYS[25], **kw):
        r2 = make(warehouse, **kw)
        before = written(warehouse)
        with pytest.raises(RunnerRefusal) as excinfo:
            r2.run(history, through=through)
        assert written(warehouse) == before
        assert r2.broker.statements == ()
        return str(excinfo.value)

    def test_tampered_cash_is_refused(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[20])
        warehouse.con.execute(
            "UPDATE paper_sessions SET cash = cash + 1 WHERE session_date = ?", [DAYS[10]]
        )
        message = self.refused(warehouse, history)
        assert "not restored" in message and str(DAYS[10]) in message and "cash" in message

    def test_a_tampered_digest_is_refused(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[20])
        warehouse.con.execute(
            "UPDATE paper_sessions SET state_digest = repeat('0', 64) WHERE session_date = ?",
            [DAYS[12]],
        )
        message = self.refused(warehouse, history)
        assert "state digest" in message and str(DAYS[12]) in message

    def test_a_deleted_session_row_is_refused(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[20])
        warehouse.con.execute("DELETE FROM paper_sessions WHERE session_date = ?", [DAYS[10]])
        message = self.refused(warehouse, history)
        assert "not restored" in message and str(DAYS[10]) in message

    def test_a_revised_bar_is_refused(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[25])
        revised = PriceHistory.from_bars([
            make_bar("AAA", d, D(100 + i) + (D(7) if i == 18 else D(0)), volume=10_000_000)
            for i, d in enumerate(DAYS)
        ])
        message = self.refused(warehouse, revised, through=DAYS[30])
        assert "not restored" in message and str(DAYS[18]) in message

    def test_a_changed_starting_cash_is_refused(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[20])
        message = self.refused(warehouse, history, cash="200000")
        assert "not restored" in message

    def test_rows_without_a_digest_are_refused(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[20])
        warehouse.con.execute("UPDATE paper_sessions SET state_digest = NULL")
        message = self.refused(warehouse, history)
        assert "cannot be verified" in message

    def test_rows_without_parameters_are_refused(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[20])
        warehouse.con.execute("UPDATE paper_sessions SET parameters = NULL")
        assert "cannot be verified" in self.refused(warehouse, history)

    def test_a_tampered_corporate_action_row_is_refused(self, warehouse):
        h = flat(split_at=10)
        make(warehouse, spec=HOLD, corporate_actions=[split()]).run(h, through=DAYS[14])
        warehouse.con.execute("UPDATE paper_corporate_actions SET shares_after = shares_after + 1")
        message = self.refused(warehouse, h, through=DAYS[20], spec=HOLD,
                               corporate_actions=[split()])
        assert f"AAA:{DAYS[10]}:split" in message

    def test_a_deleted_corporate_action_row_is_refused(self, warehouse):
        h = flat(split_at=10)
        make(warehouse, spec=HOLD, corporate_actions=[split()]).run(h, through=DAYS[14])
        warehouse.con.execute("DELETE FROM paper_corporate_actions")
        message = self.refused(warehouse, h, through=DAYS[20], spec=HOLD,
                               corporate_actions=[split()])
        assert f"AAA:{DAYS[10]}:split" in message

    def test_a_refused_restore_leaves_the_runner_usable_on_its_own_book(self, warehouse, history):
        r = make(warehouse)
        r.run(history, through=DAYS[10])
        warehouse.con.execute("UPDATE paper_sessions SET cash = cash + 1")
        live = r.broker
        r._last_persisted_session = None
        with pytest.raises(RunnerRefusal):
            r.run(history, through=DAYS[20])
        assert r.broker is live


class TestStateSurvivesRestart:
    def test_breaker_state_survives_restart(self, warehouse, other):
        risk = RiskConfig(max_daily_loss=D("0.001"), max_portfolio_drawdown=D("0.002"))
        h = flat(closes={i: "50" for i in range(5, 60)})
        make(warehouse, spec=HOLD, risk=risk).run(h, through=DAYS[6])
        r2 = make(warehouse, spec=HOLD, risk=risk)
        r2.run(h, through=DAYS[12])
        single = make(other, spec=HOLD, risk=risk)
        single.run(h, through=DAYS[12])
        assert r2.broker.status()["breakers"]["drawdown_tripped"] is True
        assert r2.broker.status()["peak_equity"] == single.broker.status()["peak_equity"]
        assert r2.broker.statements == single.broker.statements

    def test_pending_orders_survive_restart(self, warehouse, other):
        h = flat()
        r1 = make(warehouse, spec=HOLD)
        r1.run(h, through=DAYS[0])
        assert r1.broker.pending_count == 1
        r2 = make(warehouse, spec=HOLD)
        r2.run(h, through=DAYS[5])
        single = make(other, spec=HOLD)
        single.run(h, through=DAYS[5])
        assert r2.broker.portfolio().quantity_of("AAA") > 0
        assert r2.broker.statements == single.broker.statements
        assert ledger_view(r2) == ledger_view(single)

    def test_a_corporate_action_is_not_double_booked_across_restart(self, warehouse, other):
        h = flat(split_at=10)
        make(warehouse, spec=HOLD, corporate_actions=[split()]).run(h, through=DAYS[12])
        r2 = make(warehouse, spec=HOLD, corporate_actions=[split()])
        r2.run(h, through=DAYS[20])
        single = make(other, spec=HOLD, corporate_actions=[split()])
        single.run(h, through=DAYS[20])
        assert warehouse.con.execute(
            "SELECT count(*) FROM paper_corporate_actions"
        ).fetchall()[0][0] == 1
        assert len(actions_of(r2)) == 1
        assert r2.broker.portfolio().quantity_of("AAA") == single.broker.portfolio().quantity_of("AAA")
        assert r2.broker.statements == single.broker.statements

    def test_a_restart_before_the_split_books_exactly_one(self, warehouse):
        h = flat(split_at=10)
        make(warehouse, spec=HOLD, corporate_actions=[split()]).run(h, through=DAYS[8])
        r2 = make(warehouse, spec=HOLD, corporate_actions=[split()])
        r2.run(h, through=DAYS[12])
        assert warehouse.con.execute(
            "SELECT count(*) FROM paper_corporate_actions"
        ).fetchall()[0][0] == 1
        assert len(actions_of(r2)) == 1

    def test_a_re_approved_strategy_replays_each_session_with_its_own_parameters(
        self, warehouse, other, history
    ):
        first = {"fast_window": 5, "slow_window": 15, "target_weight": "0.05",
                 "symbols": ["AAA"]}
        second = {"fast_window": 3, "slow_window": 10, "target_weight": "0.03",
                  "symbols": ["AAA"]}

        def approve(w, params):
            record_active_strategy(
                w, strategy_id="ma_trend", parameters=params,
                approved_by="austin", reason="test approval",
            )

        approve(other, first)
        single = make(other, spec=None)
        single.run(history, through=DAYS[20])
        approve(other, second)
        single.run(history, through=DAYS[40])

        approve(warehouse, first)
        make(warehouse, spec=None).run(history, through=DAYS[20])
        approve(warehouse, second)
        r2 = make(warehouse, spec=None)
        result = r2.run(history, through=DAYS[40])
        assert result.restored_sessions == 21
        distinct = {r[0] for r in warehouse.con.execute(
            "SELECT DISTINCT parameters FROM paper_sessions"
        ).fetchall()}
        assert len(distinct) == 2
        r3 = make(warehouse, spec=None)
        r3.run(history, through=DAYS[40])
        assert r3.broker.statements == single.broker.statements
        assert sessions(warehouse) == sessions(other)

    def test_an_explicit_resume_after_restart_lifts_the_pause(self, warehouse, other, history):
        r1 = make(warehouse)
        r1.pause("operator halt")
        r1.run(history, through=DAYS[24])
        assert r1.broker.portfolio().quantity_of("AAA") == 0
        r2 = make(warehouse)
        r2.resume()
        r2.run(history, through=DAYS[40])
        assert r2.broker.paused is False
        single = make(other)
        single.pause("operator halt")
        single.run(history, through=DAYS[24])
        single.resume()
        single.run(history, through=DAYS[40])
        assert r2.broker.portfolio().fills
        assert r2.broker.statements == single.broker.statements
        assert sessions(warehouse) == sessions(other)

    def test_a_cli_style_pause_after_restart_is_reapplied(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[20])
        r2 = make(warehouse)
        r2.pause("halt after restart")
        r2.run(history, through=DAYS[25])
        assert r2.broker.paused and r2.broker.pause_reason == "halt after restart"
        assert warehouse.con.execute(
            "SELECT DISTINCT paused_reason FROM paper_sessions WHERE session_date > ?",
            [DAYS[20]],
        ).fetchall() == [("halt after restart",)]


class TestReviewFixes:
    def test_a_pause_persists_across_restart_until_resumed(self, warehouse, history):
        r1 = make(warehouse)
        r1.pause("operator halt")
        r1.run(history, through=DAYS[24])
        r2 = make(warehouse)
        result = r2.run(history, through=DAYS[40])
        assert r2.broker.paused and r2.broker.pause_reason == "operator halt"
        assert r2.broker.portfolio().quantity_of("AAA") == 0
        assert any("paused" in n and "operator halt" in n for n in result.notes)
        assert warehouse.con.execute(
            "SELECT DISTINCT paused_reason FROM paper_sessions WHERE session_date > ?",
            [DAYS[24]],
        ).fetchall() == [("operator halt",)]

    def test_the_book_start_is_recorded_not_the_session_day(self, warehouse, history):
        make(warehouse, start=DAYS[20]).run(history, through=DAYS[30])
        warehouse.con.execute("DELETE FROM paper_books")
        make(warehouse, start=DAYS[20]).run(history, through=DAYS[35])
        assert warehouse.con.execute(
            "SELECT first_session FROM paper_books"
        ).fetchall() == [(DAYS[20],)]

    def test_a_failed_session_forces_a_verified_replay(self, warehouse, other, history, monkeypatch):
        r = make(warehouse)
        r.run(history, through=DAYS[20])
        real = PaperRunner._record_session
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("disk full")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(PaperRunner, "_record_session", flaky)
        with pytest.raises(RuntimeError):
            r.run(history, through=DAYS[30])
        monkeypatch.setattr(PaperRunner, "_record_session", real)
        r.run(history, through=DAYS[40])
        single = make(other)
        single.run(history, through=DAYS[40])
        assert r.broker.portfolio().fills
        assert r.broker.state_digest() == single.broker.state_digest()
        assert sessions(warehouse) == sessions(other)

    def test_a_failed_first_session_starts_the_book_fresh(self, warehouse, other, history, monkeypatch):
        r = make(warehouse)
        real = PaperRunner._record_session

        def boom(self, *args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(PaperRunner, "_record_session", boom)
        with pytest.raises(RuntimeError):
            r.run(history, through=DAYS[20])
        monkeypatch.setattr(PaperRunner, "_record_session", real)
        r.run(history, through=DAYS[40])
        single = make(other)
        single.run(history, through=DAYS[40])
        assert r.broker.state_digest() == single.broker.state_digest()
        assert sessions(warehouse) == sessions(other)


class TestSessionReportFields:
    def test_session_rows_record_pnl_drawdown_and_positions(self, warehouse, history):
        r = make(warehouse)
        r.run(history, through=DAYS[30])
        last = r.broker.statements[-1]
        row = warehouse.con.execute(
            "SELECT realized_pnl, unrealized_pnl, drawdown, positions FROM paper_sessions "
            "WHERE session_date = ?", [DAYS[30]]
        ).fetchall()[0]
        cast = lambda v: warehouse.con.execute(
            "SELECT CAST(? AS DECIMAL(18,6))", [str(v)]).fetchall()[0][0]
        assert row[0] == cast(last.realized_pnl)
        assert row[1] == cast(last.unrealized_pnl)
        assert row[2] == cast(last.drawdown)
        import json
        positions = json.loads(row[3])
        quantity = r.broker.portfolio().quantity_of("AAA")
        assert quantity > 0
        assert positions == [{
            "symbol": "AAA",
            "quantity": quantity,
            "average_cost": str(last.positions["AAA"].average_cost),
            "market_value": str(D(130) * quantity),
        }]

    def test_a_flat_session_records_an_empty_position_list(self, warehouse, history):
        make(warehouse).run(history, through=DAYS[0])
        assert warehouse.con.execute(
            "SELECT positions FROM paper_sessions").fetchall() == [("[]",)]
