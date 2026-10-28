"""report_cli: smoke, push, dry-run. Fake session only; tmp_path warehouses; sentinel credentials."""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from signalforge import report_cli
from signalforge.data.storage import Warehouse
from signalforge.learning.registry import ExperimentRegistry
from signalforge.reports import events as ev

NOW = datetime(2026, 10, 28, 15, 30, 5, tzinfo=timezone.utc)
CLIENT_ID = "sentinel-client-id-AAA"
CLIENT_SECRET = "sentinel-client-secret-BBB"
INGEST = "sentinel-ingest-secret-CCC"
SENTINELS = (CLIENT_ID, CLIENT_SECRET, INGEST)
SERIES_DESC = "NASDAQCOM daily close from fred, 13805 observations from 1971-02-05 to 2026-10-27"
OHLCV_DESC = "211958 OHLCV bars for 21 symbols from tiingo, 2010-01-04 to 2026-10-27, adjusted price basis"
FP = "ab12cd34ef56" + "0" * 52
SPLITS = SimpleNamespace(
    train_start=date(2020, 1, 1), train_end=date(2021, 12, 31),
    validation_start=date(2022, 1, 1), validation_end=date(2022, 12, 31),
    test_start=None, test_end=None,
)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ev, "utcnow", lambda: NOW)


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses, raises=None):
        self.responses = list(responses) or [FakeResponse(202, {"ok": True})]
        self.calls = []
        self.raises = raises

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.raises:
            raise self.raises
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def types(self):
        return [json.loads(c[1]["data"])["event_type"] for c in self.calls]


@pytest.fixture
def env(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"SIGNALFORGE_ACCESS_CLIENT_ID={CLIENT_ID}\nSIGNALFORGE_ACCESS_CLIENT_SECRET={CLIENT_SECRET}\n"
        f"SIGNALFORGE_INGEST_SECRET={INGEST}\n", encoding="utf-8")
    return path


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "w.duckdb"
    wh = Warehouse(path)
    wh.con.execute(
        "INSERT INTO bars (symbol, trade_date, open, high, low, close, volume, source) "
        "VALUES ('AAPL', DATE '2026-10-27', 1, 1, 1, 1, 1, 'tiingo')")
    ExperimentRegistry(wh).record(
        strategy_name="ma_trend", strategy_version="1", parameters={"fast": 50, "slow": 200},
        dataset_fingerprint=FP, dataset_description=SERIES_DESC, window="train", splits=SPLITS,
        transaction_cost_assumptions={}, engine_version="0.1.0", random_seed=1, hypothesis="h",
        metrics={"total_return": "0.1", "sharpe": "0.5"}, max_drawdown=0.05, trade_count=9,
        evidence_level="SUFFICIENT", git_commit="c" * 40)
    wh.close()
    return path


def run(argv, env, session=None):
    return report_cli.main(argv, session=session, now=NOW, env_path=env)


def test_smoke_sends_one_test_heartbeat_and_prints_outcome(env, capsys):
    session = FakeSession()
    assert run(["smoke"], env, session) == 0
    assert len(session.calls) == 1
    sent = json.loads(session.calls[0][1]["data"])
    assert sent["event_type"] == "heartbeat" and sent["run_id"] == "e2e-test-2026-10-28"
    assert "is_test" not in sent
    out = capsys.readouterr().out
    assert "202" in out and "heartbeat" in out


def test_smoke_failure_exits_nonzero(env):
    assert run(["smoke"], env, FakeSession(FakeResponse(401, {"error": "unauthorized"}))) == 1


def test_push_sends_in_order_and_reports_totals(env, db, capsys):
    session = FakeSession()
    assert run(["push", "--db", str(db)], env, session) == 0
    assert session.types() == ["data_health", "backtest", "heartbeat"]
    run_ids = {json.loads(c[1]["data"])["run_id"] for c in session.calls}
    assert run_ids == {"research-20261028"}
    out = capsys.readouterr().out
    assert "data_health" in out and "backtest" in out and "3" in out


def test_duplicate_event_counts_as_success_other_failure_does_not(env, db):
    dup = FakeResponse(409, {"error": "duplicate_event"})
    assert run(["push", "--db", str(db)], env, FakeSession(dup)) == 0
    bad = FakeResponse(503, {"error": "unavailable"})
    assert run(["push", "--db", str(db)], env, FakeSession(FakeResponse(202, {}), bad)) == 1


def test_push_keeps_sending_after_a_failure_and_reports_all(env, db, capsys):
    session = FakeSession(FakeResponse(500, {"error": "boom"}), FakeResponse(202, {}))
    assert run(["push", "--db", str(db)], env, session) == 1
    assert len(session.calls) == 3


def test_push_validates_everything_before_sending_anything(env, db, monkeypatch, capsys):
    real = ev.build_all

    def with_bad_event(wh, now):
        events, warnings = real(wh, now)
        bad = json.loads(json.dumps(events[0]))
        bad["payload"]["unexpected"] = 1
        return events + [bad], warnings

    monkeypatch.setattr(ev, "build_all", with_bad_event)
    session = FakeSession()
    assert run(["push", "--db", str(db)], env, session) == 2
    assert session.calls == []
    assert "invalid" in capsys.readouterr().err.lower()


def test_dry_run_needs_no_credentials_and_no_network(tmp_path, db, capsys):
    session = FakeSession(raises=RuntimeError("network must not be touched"))
    assert run(["push", "--db", str(db), "--dry-run"], tmp_path / "absent.env", session) == 0
    assert session.calls == []
    out = capsys.readouterr().out
    lines = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    assert [e["event_type"] for e in lines] == ["data_health", "backtest", "heartbeat"]
    for e in lines:
        ev.validate_event(e)


def test_dry_run_invalid_event_exits_2(tmp_path, db, monkeypatch):
    monkeypatch.setattr(ev, "build_all", lambda wh, now: ([{"bogus": 1}], []))
    assert run(["push", "--db", str(db), "--dry-run"], tmp_path / "absent.env") == 2


def test_missing_credential_fails_closed_before_sending(tmp_path, db, capsys):
    path = tmp_path / ".env"
    path.write_text(f"SIGNALFORGE_ACCESS_CLIENT_ID={CLIENT_ID}\n", encoding="utf-8")
    session = FakeSession()
    assert run(["push", "--db", str(db)], path, session) == 2
    assert run(["smoke"], path, session) == 2
    assert session.calls == []
    captured = capsys.readouterr()
    assert "SIGNALFORGE_ACCESS_CLIENT_SECRET" in captured.err
    assert not any(s in captured.out + captured.err for s in SENTINELS)


def test_credentials_never_printed_even_when_the_transport_fails(env, db, capsys):
    session = FakeSession(raises=RuntimeError(f"{INGEST} {CLIENT_SECRET} {CLIENT_ID}"))
    assert run(["push", "--db", str(db)], env, session) == 1
    assert run(["smoke"], env, session) == 1
    captured = capsys.readouterr()
    assert not any(s in captured.out + captured.err for s in SENTINELS)


def test_missing_database_is_a_clean_error(tmp_path, env, capsys):
    assert run(["push", "--db", str(tmp_path / "nope.duckdb")], env, FakeSession()) == 2
    assert "nope.duckdb" in capsys.readouterr().err


def test_dry_run_shape_with_both_dataset_kinds(tmp_path, capsys):
    path = tmp_path / "both.duckdb"
    wh = Warehouse(path)
    wh.con.execute(
        "INSERT INTO bars (symbol, trade_date, open, high, low, close, volume, source) "
        "VALUES ('AAPL', DATE '2026-10-27', 1, 1, 1, 1, 1, 'tiingo')")
    reg = ExperimentRegistry(wh)
    for desc, fp, name in ((SERIES_DESC, FP, "ma_trend"), (OHLCV_DESC, "89767552ae61" + "0" * 52, "buy_and_hold")):
        reg.record(
            strategy_name=name, strategy_version="1", parameters={} if name == "buy_and_hold" else {"fast": 50},
            dataset_fingerprint=fp, dataset_description=desc, window="validation", splits=SPLITS,
            transaction_cost_assumptions={}, engine_version="0.1.0", random_seed=1, hypothesis="h",
            metrics={"total_return": "0.1", "sharpe": "0.5"}, max_drawdown=0.05, trade_count=9,
            evidence_level="SUFFICIENT", git_commit="c" * 40)
    wh.close()
    assert run(["push", "--db", str(path), "--dry-run"], tmp_path / "absent.env") == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert [e["event_type"] for e in lines] == ["data_health", "backtest", "backtest", "heartbeat"]
    ids = sorted(e["payload"]["strategy_id"] for e in lines if e["event_type"] == "backtest")
    assert ids == ["nasdaqcom.ma_trend-50", "ohlcv21-tiingo.buy_and_hold"]
    assert "risk_checks_ok" not in lines[-1]["payload"]
