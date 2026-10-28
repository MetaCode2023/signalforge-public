"""paper_cli on real OHLCV: raw-basis books, book-scoped approval, freshness gate.

The default NASDAQCOM series path must be untouched; an OHLCV book is its own
book with its own approval, runs on raw prices with corporate actions booked,
and refuses to start on a stale or holey calendar.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge import paper_cli
from signalforge.config.models import load_all
from signalforge.core.types import Bar
from signalforge.data.storage import Warehouse
from signalforge.paper.runner import record_active_strategy
from signalforge.pipeline import SURVIVORSHIP_NOTE

from tests.conftest import business_days

D = Decimal
DAYS = business_days(date(2020, 1, 1), 40)
SERIES_DAYS = business_days(date(2020, 1, 1), 300)

DEFAULT_GOLDEN = (
    'strategy        ma_trend\napproval        override\nbook start      2020-01-01\n'
    'sessions run    300\n'
    'window          2020-01-01 .. 2021-02-23\n'
    'NOTE: WARM-UP SHORT: only 1 trading days are visible at 2020-01-01 and ma_trend '
    'needs 200, so it will not signal until enough history has accrued.\n'
    'NOTE: UNAPPROVED STRATEGY running on an explicit override: golden\n\n'
    'SIMULATED BOOK (no real capital, no real orders)\n'
    '  equity          101,580.60\n  cash            95,196.60\n  positions       1\n'
    '  pending orders  0\n  drawdown        0.00%\n  paused          False\n'
    '  ledger intact   True\n\n'
    '==============================================================\n'
    '  SIMULATED END-OF-DAY STATEMENT  2021-02-23\n'
    '  SIGNALFORGE PAPER TRADING. No real capital. No real orders.\n'
    '==============================================================\n'
    '  Simulated equity                101,580.60\n'
    '  Simulated cash                   95,196.60\n'
    '  Gross exposure                    6,384.00\n'
    '  Realized P&L (cumulative)             0.00\n'
    '  Unrealized P&L                    1,580.60\n'
    '  Fees paid (cumulative)                1.00\n'
    '  Slippage (cumulative)                 2.40\n'
    '  Drawdown from peak                  0.00%\n'
    '--------------------------------------------------------------\n'
    '  OPEN POSITIONS (simulated)\n'
    '    NASDAQCOM        16 sh  avg cost     300.2125\n'
    '--------------------------------------------------------------\n'
    '  FILLS TODAY (simulated): none. Checked, no trades settled.\n'
    '==============================================================\n'
    'LIMITATION: PROXY STUDY, NOT A TRADEABLE RESULT. This run uses the NASDAQCOM index '
    'level as if it were a tradeable instrument. It is not: an index cannot be bought. '
    'A real implementation would trade an ETF and would pay its tracking error, expense '
    'ratio, spread and creation/redemption friction, none of which are modelled here.\n'
    'LIMITATION: Close-only series: open, high and low are all set to the published close, '
    'so there is no intraday range. Gap modelling degrades to close-to-close and limit '
    'orders cannot be evaluated against a real high or low.\n'
    'LIMITATION: Dividends are not reflected: NASDAQCOM is a price index, so total return '
    'is understated for buy-and-hold and for any strategy holding it.\n'
)


def bars_for(symbol, drop_at=None, source="tiingo", days=DAYS, last=None, skip=()):
    out = []
    for i, day in enumerate(days if last is None else days[:last]):
        if i in skip:
            continue
        raw = D(100) + D(i) if drop_at is None or i < drop_at else (D(100) + D(i)) / 2
        adj = raw if drop_at is None else (D(100) + D(i)) / 2
        out.append(Bar(symbol=symbol, trade_date=day, open=raw, high=raw, low=raw, close=raw,
                       volume=1_000_000, adj_close=adj, source=source))
    return out


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "paper.duckdb"
    w = Warehouse(str(path))
    w.upsert_bars(bars_for("AAA") + bars_for("BBB") + bars_for("SPY"))
    w.close()
    return path


def run(db, *extra):
    return paper_cli.main(["--db", str(db), *extra])


OVERRIDE_NO_START = ("--override-strategy", "buy_and_hold", "--override-reason", "test run")
OVERRIDE = (*OVERRIDE_NO_START, "--start", DAYS[0].isoformat())


class TestParser:
    def test_symbols_and_universe_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            paper_cli.parse_args(["--symbols", "AAA", "--universe"])

    def test_series_and_symbols_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            paper_cli.parse_args(["--series", "NASDAQCOM", "--symbols", "AAA"])

    def test_the_defaults_keep_the_series_path(self):
        args = paper_cli.parse_args([])
        assert args.series == "NASDAQCOM" and args.symbols is None and not args.universe
        assert args.source == "tiingo" and args.benchmark == "SPY"

    def test_symbols_are_parsed_upper_case(self):
        assert paper_cli.parse_args(["--symbols", "aaa, bbb"]).symbols == ["AAA", "BBB"]

    def test_there_is_no_holdout_flag(self):
        with pytest.raises(SystemExit):
            paper_cli.parse_args(["--run-holdout"])


class TestDefaultPathIsUntouched:
    def test_the_series_path_output_is_byte_identical(self, tmp_path, capsys):
        path = tmp_path / "series.duckdb"
        w = Warehouse(str(path))
        w.upsert_series([
            {"series_id": "NASDAQCOM", "observation_date": d, "value": D(100) + D(i),
             "source": "fred"}
            for i, d in enumerate(SERIES_DAYS)
        ])
        w.close()
        rc = run(path, "--override-strategy", "ma_trend", "--override-reason", "golden",
                 "--start", SERIES_DAYS[0].isoformat())
        assert rc == 0
        assert capsys.readouterr().out == DEFAULT_GOLDEN


class TestBookStart:
    def test_start_is_parsed(self):
        assert paper_cli.parse_args(["--start", "2020-02-12"]).start == "2020-02-12"
        assert paper_cli.parse_args([]).start is None

    def test_an_invalid_start_exits_2(self, db, capsys):
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START, "--start", "not-a-date") == 2
        assert "--start" in capsys.readouterr().out

    def test_a_new_book_without_start_is_refused_with_exit_3(self, db, capsys):
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START) == 3
        out = capsys.readouterr().out
        assert "REFUSED TO RUN" in out and "--start YYYY-MM-DD" in out
        assert "WARNING" not in out and "sessions run" not in out
        w = Warehouse(str(db))
        try:
            assert w.con.execute("SELECT count(*) FROM paper_sessions").fetchall()[0][0] == 0
        finally:
            w.close()

    def test_the_output_shows_the_book_start(self, db, capsys):
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START, "--start", str(DAYS[30])) == 0
        out = capsys.readouterr().out
        assert f"book start      {DAYS[30]}\n" in out
        assert "restored" not in out

    def test_a_second_days_run_continues_the_book(self, db, capsys):
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START, "--start", str(DAYS[30]),
                   "--through", str(DAYS[35])) == 0
        assert "sessions run    6" in capsys.readouterr().out
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START) == 0
        out = capsys.readouterr().out
        assert "sessions run    4" in out
        assert "restored        6 persisted sessions, replayed and verified" in out
        assert f"book start      {DAYS[30]}" in out
        w = Warehouse(str(db))
        try:
            assert w.con.execute("SELECT count(*) FROM paper_sessions").fetchall()[0][0] == 10
        finally:
            w.close()

    def test_a_pause_sticks_across_runs_until_resume(self, db, capsys):
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START, "--start", str(DAYS[30]),
                   "--through", str(DAYS[32]), "--pause", "bad bars") == 0
        capsys.readouterr()
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START, "--through", str(DAYS[34])) == 0
        assert "paused          True" in capsys.readouterr().out
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START, "--resume") == 0
        assert "paused          False" in capsys.readouterr().out

    def test_pause_and_resume_together_exit_2(self, db, capsys):
        assert run(db, "--symbols", "AAA", *OVERRIDE, "--pause", "x", "--resume") == 2

    def test_a_different_start_on_an_existing_book_exits_3(self, db, capsys):
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START, "--start", str(DAYS[30]),
                   "--through", str(DAYS[35])) == 0
        capsys.readouterr()
        assert run(db, "--symbols", "AAA", *OVERRIDE_NO_START, "--start", str(DAYS[31])) == 3
        out = capsys.readouterr().out
        assert "REFUSED TO RUN" in out and str(DAYS[30]) in out

    def test_the_freshness_window_starts_at_the_book_start(self, tmp_path, capsys):
        path = tmp_path / "window.duckdb"
        w = Warehouse(str(path))
        w.upsert_bars(bars_for("AAA") + bars_for("BBB", skip=(15,)) + bars_for("SPY"))
        w.close()
        assert run(path, "--symbols", "AAA,BBB", *OVERRIDE_NO_START, "--start", str(DAYS[10])) == 3
        capsys.readouterr()
        assert run(path, "--symbols", "AAA,BBB", *OVERRIDE_NO_START, "--start", str(DAYS[20])) == 0
        assert "WARNING" not in capsys.readouterr().out


class TestOhlcvBook:
    def test_it_runs_a_symbols_book_with_its_own_book_id(self, db, capsys):
        assert run(db, "--symbols", "BBB,AAA", *OVERRIDE) == 0
        w = Warehouse(str(db))
        try:
            books = {r[0] for r in w.con.execute("SELECT book_id FROM paper_sessions").fetchall()}
        finally:
            w.close()
        assert books == {"ohlcv:AAA,BBB"}
        assert "sessions run    40" in capsys.readouterr().out

    def test_the_universe_book_is_named_universe(self, db, monkeypatch):
        real = load_all()
        patched = real.model_copy(update={
            "research": real.research.model_copy(update={"universe": ("AAA", "BBB")})
        })
        monkeypatch.setattr(paper_cli, "load_all", lambda: patched)
        assert run(db, "--universe", *OVERRIDE) == 0
        w = Warehouse(str(db))
        try:
            books = {r[0] for r in w.con.execute("SELECT book_id FROM paper_sessions").fetchall()}
        finally:
            w.close()
        assert books == {"ohlcv:universe"}

    def test_the_output_carries_the_survivorship_note(self, db, capsys):
        run(db, "--symbols", "AAA", *OVERRIDE)
        assert SURVIVORSHIP_NOTE in capsys.readouterr().out

    def test_the_output_counts_booked_actions_and_explains_adjustments(self, tmp_path, capsys):
        path = tmp_path / "split.duckdb"
        w = Warehouse(str(path))
        w.upsert_bars(bars_for("AAA", drop_at=20) + bars_for("SPY"))
        w.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": DAYS[20], "action_type": "split",
             "ratio": D("2"), "source": "tiingo"},
        ])
        w.close()
        assert run(path, "--symbols", "AAA", *OVERRIDE) == 0
        out = capsys.readouterr().out
        assert "corporate actions booked  split 1, adjustment 0, dividend 0" in out
        assert "spinoff/special factors" in out and "value-preserving parent shares" in out

    def test_an_unrecorded_jump_is_refused_with_exit_3(self, tmp_path, capsys):
        path = tmp_path / "jump.duckdb"
        w = Warehouse(str(path))
        w.upsert_bars(bars_for("AAA", drop_at=20) + bars_for("SPY"))
        w.close()
        assert run(path, "--symbols", "AAA", *OVERRIDE) == 3
        assert "REFUSED TO RUN" in capsys.readouterr().out

    def test_the_book_trades_raw_prices(self, tmp_path):
        path = tmp_path / "raw.duckdb"
        w = Warehouse(str(path))
        w.upsert_bars(bars_for("AAA", drop_at=20) + bars_for("SPY"))
        w.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": DAYS[20], "action_type": "split",
             "ratio": D("2"), "source": "tiingo"},
        ])
        w.close()
        assert run(path, "--symbols", "AAA", *OVERRIDE) == 0
        w = Warehouse(str(path))
        try:
            rows = w.con.execute(
                "SELECT shares_before, shares_after FROM paper_corporate_actions"
            ).fetchall()
        finally:
            w.close()
        assert len(rows) == 1 and rows[0][1] == rows[0][0] * 2


class TestFreshnessGate:
    def test_a_symbol_that_stops_short_is_refused_with_exit_3(self, tmp_path, capsys):
        path = tmp_path / "stale.duckdb"
        w = Warehouse(str(path))
        w.upsert_bars(bars_for("AAA") + bars_for("BBB", last=37) + bars_for("SPY"))
        w.close()
        assert run(path, "--symbols", "AAA,BBB", *OVERRIDE) == 3
        out = capsys.readouterr().out
        assert "BBB" in out and str(DAYS[37]) in out
        assert "sessions run" not in out

    def test_through_after_the_benchmark_calendar_is_refused(self, db, capsys):
        assert run(db, "--symbols", "AAA", "--through", "2030-01-01", *OVERRIDE) == 3
        assert "calendar" in capsys.readouterr().out

    def test_a_missing_benchmark_is_refused(self, tmp_path, capsys):
        path = tmp_path / "nobench.duckdb"
        w = Warehouse(str(path))
        w.upsert_bars(bars_for("AAA"))
        w.close()
        assert run(path, "--symbols", "AAA", *OVERRIDE) == 3
        assert "SPY" in capsys.readouterr().out

    def test_a_refused_run_books_no_session(self, tmp_path):
        path = tmp_path / "stale2.duckdb"
        w = Warehouse(str(path))
        w.upsert_bars(bars_for("AAA") + bars_for("BBB", last=37) + bars_for("SPY"))
        w.close()
        run(path, "--symbols", "AAA,BBB", *OVERRIDE)
        w = Warehouse(str(path))
        try:
            assert w.count("bars") > 0
            assert w.con.execute("SELECT count(*) FROM paper_sessions").fetchall()[0][0] == 0
        finally:
            w.close()


class TestOlderGapsAreReportedNotRefused:
    def test_an_old_hole_prints_a_warning_and_the_run_proceeds(self, tmp_path, capsys):
        path = tmp_path / "oldhole.duckdb"
        days = business_days(date(2020, 1, 1), 80)
        w = Warehouse(str(path))
        w.upsert_bars(
            bars_for("AAA", days=days)
            + bars_for("BBB", days=days, skip=(5,))
            + bars_for("SPY", days=days)
        )
        w.close()
        assert run(path, "--symbols", "AAA,BBB", *OVERRIDE) == 0
        out = capsys.readouterr().out
        assert "WARNING" in out and "older gaps" in out and "BBB" in out
        assert str(days[5]) in out and "reported, not refused" in out

    def test_a_recent_hole_is_still_refused(self, tmp_path, capsys):
        path = tmp_path / "recenthole.duckdb"
        days = business_days(date(2020, 1, 1), 80)
        w = Warehouse(str(path))
        w.upsert_bars(
            bars_for("AAA", days=days)
            + bars_for("BBB", days=days, skip=(70,))
            + bars_for("SPY", days=days)
        )
        w.close()
        assert run(path, "--symbols", "AAA,BBB", *OVERRIDE) == 3


class TestUniverseDrift:
    def patch_universe(self, monkeypatch, universe):
        real = load_all()
        patched = real.model_copy(update={
            "research": real.research.model_copy(update={"universe": universe})
        })
        monkeypatch.setattr(paper_cli, "load_all", lambda: patched)

    def test_a_changed_universe_is_refused_until_it_is_re_approved(self, db, monkeypatch, capsys):
        approve = ("--approve", "buy_and_hold", "--approved-by", "austin", "--reason", "ok")
        self.patch_universe(monkeypatch, ("AAA", "BBB"))
        assert run(db, "--universe", *approve) == 0
        assert run(db, "--universe", "--start", DAYS[0].isoformat()) == 0
        capsys.readouterr()
        self.patch_universe(monkeypatch, ("AAA", "BBB", "SPY"))
        assert run(db, "--universe") == 2
        out = capsys.readouterr().out
        assert "re-approve" in out and "SPY" in out
        assert run(db, "--universe", *approve) == 0
        assert run(db, "--universe") == 0

    def test_the_default_book_is_not_subject_to_the_drift_check(self, tmp_path):
        path = tmp_path / "d.duckdb"
        w = Warehouse(str(path))
        w.upsert_series([
            {"series_id": "NASDAQCOM", "observation_date": d, "value": D(100) + D(i),
             "source": "fred"}
            for i, d in enumerate(DAYS)
        ])
        w.close()
        assert run(path, "--approve", "buy_and_hold", "--approved-by", "a", "--reason", "r") == 0
        assert run(path, "--start", DAYS[0].isoformat()) == 0


class TestBookScopedApproval:
    APPROVE = ("--approved-by", "austin", "--reason", "approved for paper")

    def test_the_nasdaqcom_approval_does_not_authorise_an_ohlcv_book(self, db, capsys):
        assert run(db, "--approve", "buy_and_hold", *self.APPROVE) == 0
        capsys.readouterr()
        assert run(db, "--symbols", "AAA") == 3
        out = capsys.readouterr().out
        assert "REFUSED TO RUN" in out and "no strategy is approved" in out

    def test_an_ohlcv_approval_authorises_that_book(self, db, capsys):
        assert run(db, "--symbols", "AAA", "--approve", "buy_and_hold", *self.APPROVE) == 0
        capsys.readouterr()
        assert run(db, "--symbols", "AAA", "--start", DAYS[0].isoformat()) == 0
        assert "approval        promotion" in capsys.readouterr().out

    def test_an_approval_for_one_symbol_set_does_not_cover_another(self, db, capsys):
        run(db, "--symbols", "AAA", "--approve", "buy_and_hold", *self.APPROVE)
        capsys.readouterr()
        assert run(db, "--symbols", "AAA,BBB") == 3

    def test_the_ohlcv_approval_trades_the_books_symbols_not_nasdaqcom(self, db, capsys):
        run(db, "--symbols", "AAA", "--approve", "buy_and_hold", *self.APPROVE)
        w = Warehouse(str(db))
        try:
            params = w.con.execute(
                "SELECT parameters, book_id FROM active_strategy"
            ).fetchall()
        finally:
            w.close()
        assert '"AAA"' in params[0][0] and "NASDAQCOM" not in params[0][0]
        assert params[0][1] == "ohlcv:AAA"

    def test_an_ohlcv_approval_does_not_authorise_the_default_book(self, db, capsys):
        w = Warehouse(str(db))
        w.upsert_series([
            {"series_id": "NASDAQCOM", "observation_date": d, "value": D(100) + D(i),
             "source": "fred"}
            for i, d in enumerate(DAYS)
        ])
        w.close()
        run(db, "--symbols", "AAA", "--approve", "buy_and_hold", *self.APPROVE)
        capsys.readouterr()
        assert run(db) == 3
        assert "no strategy is approved" in capsys.readouterr().out


class TestRecordActiveStrategyBookScope:
    def test_a_legacy_approval_row_belongs_to_the_default_book(self, tmp_path):
        import duckdb

        path = tmp_path / "legacy.duckdb"
        con = duckdb.connect(str(path))
        con.execute(
            "CREATE TABLE active_strategy (seq BIGINT, strategy_id VARCHAR NOT NULL, "
            "parameters VARCHAR NOT NULL, approved_by VARCHAR NOT NULL, reason VARCHAR NOT NULL, "
            "approved_at TIMESTAMP)"
        )
        con.execute(
            "INSERT INTO active_strategy VALUES (1, 'buy_and_hold', "
            "'{\"symbols\": [\"NASDAQCOM\"], \"target_weight\": \"0.05\"}', 'a', 'r', now())"
        )
        con.close()
        w = Warehouse(str(path))
        w.upsert_series([
            {"series_id": "NASDAQCOM", "observation_date": d, "value": D(100) + D(i),
             "source": "fred"}
            for i, d in enumerate(DAYS)
        ])
        w.close()
        assert paper_cli.main(["--db", str(path), "--start", DAYS[0].isoformat()]) == 0
