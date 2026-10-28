"""The paper runner's book identity, corporate-action wiring and refusals.

Phase 2 step 5. Three things are pinned:

  1. **Books do not collide.** Session progress is keyed by (book_id,
     session_date), and a warehouse written before books existed keeps its
     rows under the default NASDAQCOM book.
  2. **Actions reach the broker and leave an audit row.** One row per action
     per book, written with the session, never twice.
  3. **The runner refuses data it cannot trust** before trading a single
     session: an action that arrived after its day was traded, a split whose
     prices do not look like the split, and a price jump nobody recorded.
"""

from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.types import CorporateAction
from signalforge.data.storage import Warehouse
from signalforge.paper.ledger import EventType
from signalforge.paper import runner as runner_module
from signalforge.paper.runner import (
    ensure_schema,
    SPLIT_PRICE_TOLERANCE,
    UNRECORDED_JUMP_RATIO,
    PaperRunner,
    RunnerRefusal,
)

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 30)
SPEC = ("buy_and_hold", {"symbols": ("AAA",), "target_weight": D("0.05")})
SPLIT_DAY = DAYS[10]


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "runner_ca.duckdb"))
    yield w
    w.close()


def history(closes: dict[int, str] | None = None, opens: dict[int, str] | None = None,
            split_at: int | None = 10, ratio: int = 2) -> PriceHistory:
    """Flat 100, halving to 50 from `split_at` for a 2:1 split unless overridden."""
    bars = []
    for i, d in enumerate(DAYS):
        price = D(100) if split_at is None or i < split_at else D(100) / ratio
        if closes and i in closes:
            price = D(closes[i])
        open_ = D(opens[i]) if opens and i in opens else None
        bars.append(make_bar("AAA", d, price, open_=open_, volume=10_000_000))
    return PriceHistory.from_bars(bars)


def split(day=SPLIT_DAY, ratio="2") -> CorporateAction:
    return CorporateAction.split("AAA", day, D(ratio), "test")


def runner(warehouse, **kw) -> PaperRunner:
    kw.setdefault("override_strategy", SPEC)
    kw.setdefault("override_reason", "test")
    kw.setdefault("start", DAYS[0])
    return PaperRunner(
        warehouse=warehouse,
        research=ResearchConfig(starting_cash=D("100000"), universe=("AAA",)),
        risk=RiskConfig(),
        **kw,
    )


def seed_legacy(warehouse) -> None:
    warehouse.con.execute(
        "CREATE TABLE paper_sessions (session_date DATE PRIMARY KEY, strategy_id VARCHAR "
        "NOT NULL, equity DECIMAL(18,6), cash DECIMAL(18,6), gross_exposure DECIMAL(18,6), "
        "position_count BIGINT, fills BIGINT, completed_at TIMESTAMP)"
    )
    warehouse.con.execute(
        "CREATE TABLE paper_runs (run_id VARCHAR, strategy_id VARCHAR NOT NULL, "
        "approval_source VARCHAR NOT NULL, approval_detail VARCHAR, first_session DATE, "
        "last_session DATE, sessions BIGINT, started_at TIMESTAMP)"
    )
    warehouse.con.execute(
        "INSERT INTO paper_sessions VALUES (DATE '2020-01-02', 'old', 1, 1, 0, 0, 0, NULL)"
    )
    warehouse.con.execute(
        "INSERT INTO paper_runs VALUES ('r', 'old', 'override', 'x', DATE '2020-01-02', "
        "DATE '2020-01-02', 1, NULL)"
    )


def snapshot(warehouse):
    return (
        warehouse.con.execute(
            "SELECT book_id, session_date, strategy_id FROM paper_sessions ORDER BY 2"
        ).fetchall(),
        warehouse.con.execute("SELECT run_id, book_id FROM paper_runs ORDER BY 1").fetchall(),
    )


def count(warehouse, table, where="") -> int:
    return warehouse.con.execute(f"SELECT count(*) FROM {table} {where}").fetchall()[0][0]


class TestBookIdentity:
    def test_the_default_book_is_the_nasdaqcom_series(self, warehouse):
        r = runner(warehouse)
        r.run(history(split_at=None), through=DAYS[5])
        books = warehouse.con.execute(
            "SELECT DISTINCT book_id FROM paper_sessions"
        ).fetchall()
        assert books == [("series:NASDAQCOM",)]
        runs = warehouse.con.execute("SELECT DISTINCT book_id FROM paper_runs").fetchall()
        assert runs == [("series:NASDAQCOM",)]

    def test_a_legacy_warehouse_keeps_its_rows_under_the_default_book(self, warehouse):
        seed_legacy(warehouse)
        r = runner(warehouse)
        assert warehouse.con.execute(
            "SELECT book_id, session_date, strategy_id FROM paper_sessions"
        ).fetchall() == [("series:NASDAQCOM", date(2020, 1, 2), "old")]
        assert warehouse.con.execute("SELECT book_id FROM paper_runs").fetchall() == [
            ("series:NASDAQCOM",)
        ]
        assert r.last_completed_session(history()) == date(2020, 1, 2)

    def test_migrating_twice_changes_nothing(self, warehouse):
        seed_legacy(warehouse)
        ensure_schema(warehouse.con)
        first = snapshot(warehouse)
        assert first[0] == [("series:NASDAQCOM", date(2020, 1, 2), "old")]
        ensure_schema(warehouse.con)
        assert snapshot(warehouse) == first

    def test_a_failed_migration_rolls_back_and_leaves_the_legacy_table_intact(
        self, warehouse, monkeypatch
    ):
        seed_legacy(warehouse)
        monkeypatch.setattr(runner_module, "SCHEMA", runner_module.SCHEMA + "SELECT error('boom');")
        with pytest.raises(Exception):
            ensure_schema(warehouse.con)
        tables = {r[0] for r in warehouse.con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
        assert "paper_sessions_legacy" not in tables
        assert warehouse.con.execute(
            "SELECT session_date, strategy_id FROM paper_sessions"
        ).fetchall() == [(date(2020, 1, 2), "old")]
        assert "book_id" not in {r[0] for r in warehouse.con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'paper_sessions'"
        ).fetchall()}
        monkeypatch.undo()
        ensure_schema(warehouse.con)
        assert snapshot(warehouse)[0] == [("series:NASDAQCOM", date(2020, 1, 2), "old")]

    def test_two_books_do_not_collide(self, warehouse):
        h = history(split_at=None)
        a = runner(warehouse, book_id="series:NASDAQCOM")
        b = runner(warehouse, book_id="ohlcv:AAA")
        a.run(h, through=DAYS[5])
        result = b.run(h, through=DAYS[5])
        assert result.sessions == 6
        assert count(warehouse, "paper_sessions") == 12
        assert count(warehouse, "paper_sessions", "WHERE book_id = 'ohlcv:AAA'") == 6
        assert b.last_completed_session(h) == DAYS[5]

    def test_a_fresh_runner_restores_its_own_book_and_another_book_is_unaffected(
        self, warehouse
    ):
        h = history(split_at=None)
        runner(warehouse, book_id="one").run(h, through=DAYS[5])
        resumed = runner(warehouse, book_id="one").run(h, through=DAYS[8])
        assert (resumed.sessions, resumed.restored_sessions) == (3, 6)
        other = runner(warehouse, book_id="two").run(h, through=DAYS[8])
        assert (other.sessions, other.restored_sessions) == (9, 0)


class TestActionsReachTheBroker:
    def test_an_explicit_split_is_booked_and_audited(self, warehouse):
        r = runner(warehouse, corporate_actions=[split()])
        r.run(history(), through=DAYS[12])
        assert r.broker.portfolio().quantity_of("AAA") > 0
        events = [e for e in r.broker.ledger.events if e.event_type is EventType.CORPORATE_ACTION]
        assert [e.payload["action_id"] for e in events] == [f"AAA:{SPLIT_DAY}:split"]
        row = warehouse.con.execute(
            "SELECT book_id, action_id, session_date, kind, symbol, shares_before, shares_after, "
            "cash_delta, cash_in_lieu, residual, realized_pnl_delta FROM paper_corporate_actions"
        ).fetchall()
        assert len(row) == 1
        book, action_id, day, kind, symbol, before, after, cash, lieu, residual, pnl = row[0]
        assert (book, action_id, day, kind, symbol) == (
            "series:NASDAQCOM", f"AAA:{SPLIT_DAY}:split", SPLIT_DAY, "split", "AAA"
        )
        assert after == before * 2
        assert (cash, lieu, residual, pnl) == (D("0.00"), D("0.00"), D("0"), D("0"))

    def test_a_dividend_is_audited_with_its_cash(self, warehouse):
        div = CorporateAction.dividend("AAA", SPLIT_DAY, D("0.50"), "test")
        r = runner(warehouse, corporate_actions=[div])
        r.run(history(split_at=None), through=DAYS[12])
        shares, cash = warehouse.con.execute(
            "SELECT shares_before, cash_delta FROM paper_corporate_actions"
        ).fetchall()[0]
        assert cash == D(shares) * D("0.50")

    def test_a_double_run_writes_no_new_rows(self, warehouse):
        r = runner(warehouse, corporate_actions=[split()])
        h = history()
        r.run(h, through=DAYS[12])
        assert count(warehouse, "paper_corporate_actions") == 1
        assert r.run(h, through=DAYS[12]).sessions == 0
        assert count(warehouse, "paper_corporate_actions") == 1

    def test_the_audit_key_rejects_a_duplicate_and_rolls_the_session_back(self, warehouse):
        r = runner(warehouse, corporate_actions=[split()])
        r.run(history(), through=DAYS[12])
        sessions_before = warehouse.con.execute(
            "SELECT * FROM paper_sessions ORDER BY session_date"
        ).fetchall()
        with pytest.raises(duckdb.ConstraintException):
            r._record_session(DAYS[12], "buy_and_hold", r.broker.statements[-1], 0)
        assert count(warehouse, "paper_corporate_actions") == 1
        assert warehouse.con.execute(
            "SELECT * FROM paper_sessions ORDER BY session_date"
        ).fetchall() == sessions_before

    def test_an_action_evaluated_while_flat_is_still_audited(self, warehouse):
        r = runner(warehouse, corporate_actions=[split(DAYS[0])])
        r.run(history(split_at=None), through=DAYS[2])
        assert warehouse.con.execute(
            "SELECT shares_before, shares_after FROM paper_corporate_actions"
        ).fetchall() == [(0, 0)]

    def test_the_series_book_passes_no_actions_and_writes_no_audit_rows(self, warehouse):
        r = runner(warehouse)
        r.run(history(split_at=None), through=DAYS[5])
        assert count(warehouse, "paper_corporate_actions") == 0

    def test_a_raw_basis_book_loads_its_actions_from_the_warehouse(self, warehouse):
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": SPLIT_DAY, "action_type": "split",
             "ratio": "2", "source": "tiingo"},
            {"symbol": "AAA", "effective_date": DAYS[11], "action_type": "dividend",
             "cash_amount": "0.10", "source": "other"},
        ])
        r = runner(warehouse, raw_basis=True, corporate_action_source="tiingo")
        r.run(history(), through=DAYS[12])
        assert warehouse.con.execute(
            "SELECT action_id FROM paper_corporate_actions"
        ).fetchall() == [(f"AAA:{SPLIT_DAY}:split",)]


class TestLateArrival:
    def test_an_action_for_a_day_already_traded_is_refused(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        h = history(split_at=None)
        r.run(h, through=DAYS[12])
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": SPLIT_DAY, "action_type": "dividend",
             "cash_amount": "0.10", "source": "tiingo"},
        ])
        before = count(warehouse, "paper_sessions")
        with pytest.raises(RunnerRefusal, match="after the day was traded") as excinfo:
            r.run(h, through=DAYS[14])
        assert f"AAA:{SPLIT_DAY}:dividend" in str(excinfo.value)
        assert count(warehouse, "paper_sessions") == before

    def test_an_action_before_the_books_first_session_is_not_late(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        h = history(split_at=None)
        r.run(h, through=DAYS[9])
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": DAYS[0] - timedelta(days=30),
             "action_type": "dividend", "cash_amount": "0.10", "source": "tiingo"},
        ])
        assert r.run(h, through=DAYS[12]).sessions == 3

    def test_an_action_after_the_last_session_is_not_late(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        h = history(split_at=None)
        r.run(h, through=DAYS[9])
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": DAYS[20], "action_type": "dividend",
             "cash_amount": "0.10", "source": "tiingo"},
        ])
        assert r.run(h, through=DAYS[12]).sessions == 3

    def test_an_audited_action_is_not_late(self, warehouse):
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": SPLIT_DAY, "action_type": "split",
             "ratio": "2", "source": "tiingo"},
        ])
        r = runner(warehouse, raw_basis=True)
        h = history()
        r.run(h, through=DAYS[12])
        assert r.run(h, through=DAYS[14]).sessions == 2


class TestPriceConsistency:
    def test_constants_are_the_specified_thresholds(self):
        assert SPLIT_PRICE_TOLERANCE == D("0.25")
        assert UNRECORDED_JUMP_RATIO == D("1.4")

    def test_a_split_whose_prices_did_not_move_is_refused(self, warehouse):
        r = runner(warehouse, corporate_actions=[split()])
        with pytest.raises(RunnerRefusal) as excinfo:
            r.run(history(split_at=None), through=DAYS[12])
        message = str(excinfo.value)
        assert "AAA" in message and str(SPLIT_DAY) in message
        assert "expected" in message and "observed" in message
        assert count(warehouse, "paper_sessions") == 0

    def test_a_split_within_tolerance_is_accepted(self, warehouse):
        # Implied 100 / 42 = 2.38, 19% off 2.
        r = runner(warehouse, corporate_actions=[split()])
        h = history(opens={10: "42"}, closes={10: "42"})
        assert r.run(h, through=DAYS[12]).sessions == 13

    def test_a_split_beyond_tolerance_is_refused(self, warehouse):
        # Implied 100 / 39 = 2.56, 28% off 2.
        r = runner(warehouse, corporate_actions=[split()])
        h = history(opens={10: "39"}, closes={10: "39"})
        with pytest.raises(RunnerRefusal):
            r.run(h, through=DAYS[12])

    def test_the_open_is_compared_not_the_close(self, warehouse):
        r = runner(warehouse, corporate_actions=[split()])
        h = history(opens={10: "50"}, closes={10: "40"})
        # open fits the split; the close drifting 20% intraday is not a split error.
        assert r.run(h, through=DAYS[12]).sessions == 13

    def test_a_reverse_split_is_checked_against_its_own_ratio(self, warehouse):
        reverse = split(ratio="0.5")
        r = runner(warehouse, corporate_actions=[reverse])
        h = history(split_at=10, ratio=2)  # prices halve, but a 1:2 reverse split doubles them
        with pytest.raises(RunnerRefusal):
            r.run(h, through=DAYS[12])

    def test_a_dividend_is_not_price_checked(self, warehouse):
        div = CorporateAction.dividend("AAA", SPLIT_DAY, D("0.50"), "test")
        r = runner(warehouse, corporate_actions=[div])
        assert r.run(history(split_at=None), through=DAYS[12]).sessions == 13


class TestUnrecordedJumps:
    def test_a_split_shaped_move_with_no_action_is_refused(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        with pytest.raises(RunnerRefusal) as excinfo:
            r.run(history(), through=DAYS[12])
        message = str(excinfo.value)
        assert "AAA" in message and str(SPLIT_DAY) in message
        assert count(warehouse, "paper_sessions") == 0

    def test_a_jump_up_is_refused_too(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        with pytest.raises(RunnerRefusal):
            r.run(history(closes={10: "150"}, split_at=None), through=DAYS[12])

    def test_a_move_inside_the_ratio_is_not_a_jump(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        h = history(closes={10: "139"}, split_at=None)  # 1.39x, under 1.4x
        # Day 11 reverts, 139 -> 100 is 1.39x as well.
        assert r.run(h, through=DAYS[12]).sessions == 13

    def test_a_jump_outside_the_window_being_traded_is_ignored(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        assert r.run(history(), through=DAYS[8]).sessions == 9

    def test_the_bar_before_the_first_session_counts(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        r.run(history(split_at=None), through=DAYS[9])
        with pytest.raises(RunnerRefusal):
            r.run(history(), through=DAYS[10])

    def test_a_recorded_split_excuses_the_jump(self, warehouse):
        r = runner(warehouse, corporate_actions=[split()], raw_basis=True)
        assert r.run(history(), through=DAYS[12]).sessions == 13

    def test_a_recorded_dividend_does_not_excuse_a_jump(self, warehouse):
        div = CorporateAction.dividend("AAA", SPLIT_DAY, D("0.50"), "test")
        r = runner(warehouse, corporate_actions=[div], raw_basis=True)
        with pytest.raises(RunnerRefusal):
            r.run(history(), through=DAYS[12])

    def test_the_series_book_is_not_jump_checked(self, warehouse):
        r = runner(warehouse)
        assert r.run(history(), through=DAYS[12]).sessions == 13


class TestWaysOutOfAnUnrecordedJump:
    def test_an_acknowledged_move_is_not_refused(self, warehouse):
        warehouse.acknowledge_price_move("AAA", SPLIT_DAY, "Class C issue, not tradeable as a split")
        r = runner(warehouse, raw_basis=True)
        assert r.run(history(), through=DAYS[12]).sessions == 13

    def test_an_acknowledgement_for_another_date_does_not_excuse_it(self, warehouse):
        warehouse.acknowledge_price_move("AAA", DAYS[3], "unrelated")
        r = runner(warehouse, raw_basis=True)
        with pytest.raises(RunnerRefusal):
            r.run(history(), through=DAYS[12])

    @pytest.mark.parametrize("reason", ["", "   ", None])
    def test_an_acknowledgement_needs_a_reason(self, warehouse, reason):
        with pytest.raises(ValidationError):
            warehouse.acknowledge_price_move("AAA", SPLIT_DAY, reason)
        assert warehouse.price_move_acknowledgements() == set()

    def test_re_acknowledging_with_the_same_reason_is_idempotent(self, warehouse):
        warehouse.acknowledge_price_move("AAA", SPLIT_DAY, "real crash")
        warehouse.acknowledge_price_move("AAA", SPLIT_DAY, "real crash")
        assert count(warehouse, "price_move_acknowledgements") == 1
        assert warehouse.price_move_acknowledgements() == {("AAA", SPLIT_DAY)}

    def test_re_acknowledging_with_a_different_reason_is_refused(self, warehouse):
        warehouse.acknowledge_price_move("AAA", SPLIT_DAY, "real crash")
        with pytest.raises(ValidationError):
            warehouse.acknowledge_price_move("AAA", SPLIT_DAY, "actually a split")
        assert warehouse.con.execute(
            "SELECT reason FROM price_move_acknowledgements"
        ).fetchall() == [("real crash",)]

    def test_a_manual_split_is_loaded_booked_and_excuses_the_jump(self, warehouse):
        warehouse.record_manual_split("AAA", SPLIT_DAY, D("2"))
        r = runner(warehouse, raw_basis=True, corporate_action_source="tiingo")
        assert r.run(history(), through=DAYS[12]).sessions == 13
        assert warehouse.con.execute(
            "SELECT action_id, kind FROM paper_corporate_actions"
        ).fetchall() == [(f"AAA:{SPLIT_DAY}:split", "split")]

    def test_a_manual_split_agreeing_with_a_vendor_split_collapses_to_one(self, warehouse):
        warehouse.record_manual_split("AAA", SPLIT_DAY, D("2"))
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": SPLIT_DAY, "action_type": "split",
             "ratio": "2", "source": "tiingo"},
        ])
        r = runner(warehouse, raw_basis=True, corporate_action_source="tiingo")
        r.run(history(), through=DAYS[12])
        assert count(warehouse, "paper_corporate_actions") == 1

    def test_a_manual_split_disagreeing_with_a_vendor_split_is_refused(self, warehouse):
        warehouse.record_manual_split("AAA", SPLIT_DAY, D("2"))
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": SPLIT_DAY, "action_type": "split",
             "ratio": "3", "source": "tiingo"},
        ])
        r = runner(warehouse, raw_basis=True, corporate_action_source="tiingo")
        with pytest.raises(ValidationError, match="disagree"):
            r.run(history(), through=DAYS[12])
        assert count(warehouse, "paper_sessions") == 0

    def test_a_manual_split_is_validated(self, warehouse):
        with pytest.raises(ValidationError):
            warehouse.record_manual_split("AAA", SPLIT_DAY, D("0"))
        assert count(warehouse, "corporate_actions") == 0

    def test_a_conflicting_manual_split_is_refused_and_a_repeat_is_idempotent(self, warehouse):
        warehouse.record_manual_split("AAA", SPLIT_DAY, D("2"))
        warehouse.record_manual_split("AAA", SPLIT_DAY, D("2"))
        assert count(warehouse, "corporate_actions") == 1
        with pytest.raises(ValidationError):
            warehouse.record_manual_split("AAA", SPLIT_DAY, D("3"))
        assert warehouse.con.execute(
            "SELECT ratio FROM corporate_actions"
        ).fetchall() == [(D("2.000000"),)]

    def test_the_refusal_lists_every_unacknowledged_jump_and_both_remedies(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        h = history(closes={10: "150", 20: "50"}, split_at=None)
        warehouse.acknowledge_price_move("AAA", DAYS[11], "fine")
        with pytest.raises(RunnerRefusal) as excinfo:
            r.run(h, through=DAYS[25])
        message = str(excinfo.value)
        for d in (DAYS[10], DAYS[20], DAYS[21]):
            assert str(d) in message
        assert str(DAYS[11]) not in message
        assert "1.50" in message and "2.00" in message
        assert "acknowledge_price_move" in message
        assert "record_manual_split" in message


class TestNonTradingDayActions:
    SATURDAY = DAYS[2] + timedelta(days=1)

    def test_an_action_on_a_non_trading_day_inside_the_window_is_refused(self, warehouse):
        div = CorporateAction.dividend("AAA", self.SATURDAY, D("0.50"), "test")
        r = runner(warehouse, corporate_actions=[div])
        with pytest.raises(RunnerRefusal, match="not a trading day") as excinfo:
            r.run(history(split_at=None), through=DAYS[12])
        assert str(self.SATURDAY) in str(excinfo.value)
        assert count(warehouse, "paper_sessions") == 0

    def test_it_is_refused_when_it_falls_in_the_already_traded_range(self, warehouse):
        r = runner(warehouse, raw_basis=True)
        h = history(split_at=None)
        r.run(h, through=DAYS[12])
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": self.SATURDAY, "action_type": "dividend",
             "cash_amount": "0.10", "source": "tiingo"},
        ])
        with pytest.raises(RunnerRefusal, match="not a trading day"):
            r.run(h, through=DAYS[14])

    def test_an_action_for_a_symbol_outside_the_book_is_ignored(self, warehouse):
        div = CorporateAction.dividend("ZZZ", self.SATURDAY, D("0.50"), "test")
        r = runner(warehouse, corporate_actions=[div])
        assert r.run(history(split_at=None), through=DAYS[12]).sessions == 13

    def test_an_action_after_the_window_is_not_refused_yet(self, warehouse):
        saturday = DAYS[20] + timedelta(days=(5 - DAYS[20].weekday()))
        div = CorporateAction.dividend("AAA", saturday, D("0.50"), "test")
        r = runner(warehouse, corporate_actions=[div])
        assert r.run(history(split_at=None), through=DAYS[12]).sessions == 13


class TestRunIdCarriesTheBook:
    def test_the_default_book_run_id_is_unchanged(self, warehouse):
        result = runner(warehouse).run(history(split_at=None), through=DAYS[5])
        assert result.result.run_id == f"paper:buy_and_hold:{DAYS[5].isoformat()}"
        assert warehouse.con.execute("SELECT run_id FROM paper_runs").fetchall() == [
            (f"paper:buy_and_hold:{DAYS[5].isoformat()}",)
        ]

    def test_another_book_puts_its_id_in_the_run_id(self, warehouse):
        result = runner(warehouse, book_id="ohlcv:AAA").run(
            history(split_at=None), through=DAYS[5]
        )
        assert result.result.run_id == f"paper:ohlcv:AAA:buy_and_hold:{DAYS[5].isoformat()}"
