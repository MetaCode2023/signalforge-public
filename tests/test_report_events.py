"""Report event builders and the strict local validator (contract mirror)."""

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from signalforge.data.storage import Warehouse
from signalforge.learning.registry import ExperimentRegistry
from signalforge.reports import events as ev

NOW = datetime(2026, 10, 28, 15, 30, 5, tzinfo=timezone.utc)
FP = "ab12cd34ef56" + "0" * 52
SPLITS = SimpleNamespace(
    train_start=date(2020, 1, 1), train_end=date(2021, 12, 31),
    validation_start=date(2022, 1, 1), validation_end=date(2022, 12, 31),
    test_start=date(2023, 1, 1), test_end=date(2023, 12, 31),
)
SHA = "a" * 40


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ev, "utcnow", lambda: NOW)


def heartbeat():
    return ev.build_heartbeat(NOW, "research-20261028")


def paper():
    return {
        "schema_version": 1, "paper_only": True, "event_id": str(uuid.uuid4()),
        "event_type": "paper_session", "run_id": "research-20261028",
        "occurred_at": "2026-10-28T15:30:05Z",
        "payload": {
            "strategy_id": "buy_and_hold", "as_of": "2026-10-27T00:00:00Z", "equity": 100000.0,
            "cash": 500.0, "realized_pnl": 0.0, "unrealized_pnl": 12.5,
            "positions": [{"symbol": "AAPL", "qty": 10, "avg_cost": 150.0, "market_value": 1600.0}],
        },
    }


def backtest_payload():
    return {
        "strategy_id": "ma_trend-50-200", "dataset_version": "ab12cd34ef56",
        "commit_sha": SHA, "return_pct": 12.34, "max_drawdown_pct": 8.0, "trades": 14,
        "period_start": "2020-01-01T00:00:00Z", "period_end": "2021-12-31T00:00:00Z", "status": "candidate",
    }


def backtest(**over):
    e = paper()
    e["event_type"] = "backtest"
    e["payload"] = {**backtest_payload(), **over}
    return e


def invalid(event):
    with pytest.raises(ev.EventInvalid):
        ev.validate_event(event)


def test_valid_heartbeat_paper_backtest_pass():
    ev.validate_event(heartbeat())
    ev.validate_event(paper())
    ev.validate_event(backtest())


def test_heartbeat_shape_and_run_id():
    h = heartbeat()
    assert h["schema_version"] == 1 and h["paper_only"] is True
    assert h["event_type"] == "heartbeat"
    assert h["occurred_at"] == "2026-10-28T15:30:05Z"
    assert h["payload"]["status"] == "idle" and "risk_checks_ok" not in h["payload"]
    assert ev.research_run_id(NOW) == "research-20261028"
    assert ev.smoke_run_id(NOW) == "e2e-test-2026-10-28"
    uuid.UUID(h["event_id"])


def test_unknown_keys_and_is_test_rejected():
    for bad in ("is_test", "extra"):
        e = heartbeat()
        e[bad] = True
        invalid(e)
    e = heartbeat()
    e["payload"]["is_test"] = True
    invalid(e)
    invalid(backtest(surprise=1))


@pytest.mark.parametrize("run_id", ["Bad", "-x", "a b", "x" * 65, ""])
def test_bad_run_ids_rejected(run_id):
    e = heartbeat()
    e["run_id"] = run_id
    invalid(e)


@pytest.mark.parametrize("ts", [
    "2026-10-28 15:30:05Z", "2026-10-28T15:30:05", "2026-10-28T15:30:05+00:00", "2099-01-01T00:00:00Z",
])
def test_bad_or_future_occurred_at_rejected(ts):
    e = heartbeat()
    e["occurred_at"] = ts
    invalid(e)


def test_milliseconds_allowed_and_bad_event_id_rejected():
    e = heartbeat()
    e["occurred_at"] = "2026-10-28T15:30:05.123Z"
    ev.validate_event(e)
    e["event_id"] = "nope"
    invalid(e)


@pytest.mark.parametrize("key,value", [
    ("schema_version", 2), ("schema_version", "1"), ("schema_version", True), ("paper_only", False),
])
def test_schema_version_must_be_number_one_and_paper_only_true(key, value):
    e = heartbeat()
    e[key] = value
    invalid(e)


@pytest.mark.parametrize("payload", [
    {}, {"status": "bogus"}, {"status": "idle", "uptime_s": -1},
    {"status": "idle", "engine_version": "Bad Slug"},
    {"status": "idle", "active_strategies": ["a", "a"]},
    {"status": "idle", "active_strategies": [f"s{i}" for i in range(51)]},
    {"status": "idle", "risk_checks_ok": "yes"},
])
def test_heartbeat_payload_rules(payload):
    e = heartbeat()
    e["payload"] = payload
    invalid(e)


def test_paper_session_rules():
    e = paper(); e["payload"]["cash"] = 100000.02; invalid(e)
    e = paper(); e["payload"]["cash"] = 100000.01; ev.validate_event(e)
    e = paper(); e["payload"]["equity"] = -1; invalid(e)
    e = paper(); del e["payload"]["realized_pnl"]; invalid(e)
    e = paper(); e["payload"]["positions"][0]["symbol"] = "aapl"; invalid(e)
    e = paper(); e["payload"]["positions"][0]["qty"] = 1.5; invalid(e)
    e = paper(); e["payload"]["positions"][0]["extra"] = 1; invalid(e)
    e = paper(); e["payload"]["max_drawdown_pct"] = 101; invalid(e)
    e = paper(); e["payload"]["positions"] = [e["payload"]["positions"][0]] * 101; invalid(e)
    e = paper(); e["payload"]["positions"][0]["symbol"] = "BRK.B"; ev.validate_event(e)


def test_backtest_rules():
    invalid(backtest(commit_sha="ABCDEF1"))
    invalid(backtest(commit_sha="abc12"))
    invalid(backtest(dataset_version="AB12"))
    invalid(backtest(return_pct=-1001))
    invalid(backtest(return_pct=100001))
    invalid(backtest(period_start="2022-01-01", period_end="2021-01-01"))
    invalid(backtest(status="weird"))
    invalid(backtest(status="rejected"))
    invalid(backtest(status="rejected", rejection_reason="because"))
    invalid(backtest(rejection_reason="overfit"))
    invalid(backtest(trades=-1))
    ev.validate_event(backtest(
        status="rejected", rejection_reason="overfit", sharpe=0.5, baseline_strategy_id="buy_and_hold"))


def test_data_health_rules():
    e = backtest()
    e["event_type"] = "data_health"
    e["payload"] = {"dataset_version": "tiingo-20261027", "rows": 10,
                    "last_observation_at": "2026-10-27T00:00:00Z", "stale": False}
    ev.validate_event(e)
    e["payload"]["stale"] = "no"
    invalid(e)


def test_unsupported_event_types_refused():
    e = heartbeat()
    e["event_type"] = "risk_event"
    invalid(e)
    e["event_type"] = "mystery"
    invalid(e)


@pytest.mark.parametrize("value", [
    "aB3dE5gH7jK9mN2pQ4sT6vW8yZ1bC3dF",
    "f" * 64,
    "ghp_" + "x" * 30,
    "sk-" + "y" * 30,
    "eyJhbGciOiJIUzI1NiJ9abc",
    "Bearer abcdefghijklmnop",
])
def test_secret_looking_strings_rejected(value):
    e = backtest()
    e["payload"]["baseline_strategy_id"] = value
    invalid(e)
    e = heartbeat()
    e["payload"]["status"] = value
    invalid(e)


def test_full_64_hex_fingerprint_is_rejected_but_12_is_fine():
    invalid(backtest(dataset_version=FP))
    ev.validate_event(backtest(dataset_version=FP[:12]))


def test_oversize_body_rejected():
    e = paper()
    e["payload"]["positions"] = [
        {"symbol": "AAPL", "qty": 10, "avg_cost": 150.0, "market_value": 1600.0 + i * 0.0000001}
        for i in range(100)
    ]
    ev.validate_event(e)
    e["payload"]["strategy_id"] = "s" + "-a" * 40000
    invalid(e)


def test_serialize_is_canonical_utf8_with_floats():
    e = paper()
    e["payload"]["equity"] = Decimal("100000.250000")
    body = ev.serialize(e)
    assert isinstance(body, bytes)
    text = body.decode("utf-8")
    assert json.loads(text)["payload"]["equity"] == 100000.25
    assert text == json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_event_ids_are_deterministic_uuid5():
    a = ev.event_id_for("backtest", "k")
    assert a == ev.event_id_for("backtest", "k")
    assert a != ev.event_id_for("data_health", "k")
    assert uuid.UUID(a).version == 5


# ------------------------------------------------------------ warehouse-based

def make_wh(tmp_path, name="w.duckdb"):
    return Warehouse(tmp_path / name)


def add_bar(wh, symbol, day, source="tiingo"):
    wh.con.execute(
        "INSERT INTO bars (symbol, trade_date, open, high, low, close, volume, source) "
        "VALUES (?, ?, 1, 1, 1, 1, 1, ?)", [symbol, day, source])


SERIES_DESC = "NASDAQCOM daily close from fred, 13805 observations from 1971-02-05 to 2026-10-27"
OHLCV_DESC = "211958 OHLCV bars for 21 symbols from tiingo, 2010-01-04 to 2026-10-27, adjusted price basis"


def record(wh, name="ma_trend", window="train", params=None, fp=FP, metrics=None, commit=SHA, seed=1,
           desc=SERIES_DESC, version="1", costs=None):
    reg = ExperimentRegistry(wh)
    exp = reg.record(
        strategy_name=name, strategy_version=version, parameters={"fast": 50, "slow": 200} if params is None else params,
        dataset_fingerprint=fp, dataset_description=desc, window=window, splits=SPLITS,
        transaction_cost_assumptions=costs or {}, engine_version="0.1.0", random_seed=seed, hypothesis="h",
        metrics=metrics or {"total_return": "0.1234", "sharpe": "0.8", "cagr": "0.05"},
        max_drawdown=0.08, trade_count=14, evidence_level="SUFFICIENT", git_commit=commit,
    )
    return reg, exp


def test_data_health_from_warehouse(tmp_path):
    wh = make_wh(tmp_path)
    add_bar(wh, "AAPL", date(2026, 10, 26))
    add_bar(wh, "AAPL", date(2026, 10, 27))
    wh.con.execute("INSERT INTO series VALUES ('S', DATE '2026-10-01', 1, 'fred', NULL)")
    wh.con.execute("INSERT INTO corporate_actions VALUES ('AAPL', DATE '2026-10-01', 'SPLIT', 2, NULL, 'x')")
    event, warns = ev.build_data_health(wh, NOW, "research-20261028")
    assert event["payload"] == {"dataset_version": "tiingo-20261027", "rows": 4,
                                "last_observation_at": "2026-10-27T00:00:00Z", "stale": False}
    ev.validate_event(event)
    assert event["event_id"] == ev.event_id_for("data_health", "tiingo-20261027")


def test_data_health_stale_after_four_days(tmp_path):
    wh = make_wh(tmp_path)
    add_bar(wh, "AAPL", date(2026, 10, 24))
    assert ev.build_data_health(wh, NOW, "r")[0]["payload"]["stale"] is False
    wh2 = make_wh(tmp_path, "w2.duckdb")
    add_bar(wh2, "AAPL", date(2026, 10, 23))
    assert ev.build_data_health(wh2, NOW, "r")[0]["payload"]["stale"] is True


def test_data_health_empty_warehouse_gives_warning_not_event(tmp_path):
    event, warns = ev.build_data_health(make_wh(tmp_path), NOW, "r")
    assert event is None and warns


def test_backtest_events_map_fields(tmp_path):
    wh = make_wh(tmp_path)
    record(wh, window="train")
    events, warns = ev.build_backtest_events(wh, NOW, "research-20261028")
    assert len(events) == 1
    e = events[0]
    ev.validate_event(e)
    p = e["payload"]
    assert p["strategy_id"] == "nasdaqcom.ma_trend-50-200"
    assert p["dataset_version"] == FP[:12]
    assert p["commit_sha"] == SHA
    assert p["return_pct"] == pytest.approx(12.34)
    assert p["max_drawdown_pct"] == pytest.approx(8.0)
    assert p["trades"] == 14 and p["sharpe"] == pytest.approx(0.8)
    assert (p["period_start"], p["period_end"]) == ("2020-01-01T00:00:00Z", "2021-12-31T00:00:00Z")
    assert p["status"] == "candidate" and p["baseline_strategy_id"] == "nasdaqcom.buy_and_hold"
    assert "rejection_reason" not in p


def test_backtest_never_emits_test_window(tmp_path):
    wh = make_wh(tmp_path)
    record(wh, window="test")
    record(wh, window="validation")
    events, _ = ev.build_backtest_events(wh, NOW, "r")
    assert [e["payload"]["period_start"] for e in events] == ["2022-01-01T00:00:00Z"]
    wh2 = make_wh(tmp_path, "w2.duckdb")
    record(wh2, window="test")
    assert ev.build_backtest_events(wh2, NOW, "r")[0] == []


def test_param_variants_each_emit_and_buy_and_hold_has_no_baseline(tmp_path):
    wh = make_wh(tmp_path)
    record(wh)
    record(wh, params={"fast": 60, "slow": 200}, metrics={"total_return": "0.5", "sharpe": "1.0", "cagr": "0"})
    record(wh, name="buy_and_hold", params={})
    events, _ = ev.build_backtest_events(wh, NOW, "r")
    by = {e["payload"]["strategy_id"]: e["payload"] for e in events}
    assert len(events) == 3
    assert by["nasdaqcom.ma_trend-60-200"]["return_pct"] == pytest.approx(50.0)
    assert by["nasdaqcom.ma_trend-50-200"]["return_pct"] == pytest.approx(12.34)
    assert "baseline_strategy_id" not in by["nasdaqcom.buy_and_hold"]
    assert len({e["event_id"] for e in events}) == 3


def test_version_or_cost_change_gives_new_event_id(tmp_path):
    wh = make_wh(tmp_path)
    _, first = record(wh)
    wh.con.execute("UPDATE experiments SET created_at = TIMESTAMP '2026-10-28 08:00:00' WHERE experiment_id = ?", [first])
    base = ev.build_backtest_events(wh, NOW, "r")[0]
    assert len(base) == 1
    _, second = record(wh, version="2")
    wh.con.execute("UPDATE experiments SET created_at = TIMESTAMP '2026-10-28 09:00:00' WHERE experiment_id = ?", [second])
    after_version = ev.build_backtest_events(wh, NOW, "r")[0]
    _, third = record(wh, costs={"commission_bps": 5})
    wh.con.execute("UPDATE experiments SET created_at = TIMESTAMP '2026-10-28 10:00:00' WHERE experiment_id = ?", [third])
    after_costs = ev.build_backtest_events(wh, NOW, "r")[0]
    ids = [base[0]["event_id"], after_version[0]["event_id"], after_costs[0]["event_id"]]
    assert len(after_version) == len(after_costs) == 1
    assert len(set(ids)) == 3


def test_same_strategy_id_sends_only_the_newest_run(tmp_path):
    wh = make_wh(tmp_path)
    _, old = record(wh, version="1", metrics={"total_return": "0.0137", "sharpe": "0.1", "cagr": "0"})
    wh.con.execute("UPDATE experiments SET created_at = TIMESTAMP '2026-10-27 22:36:00' WHERE experiment_id = ?", [old])
    _, new = record(wh, version="2", metrics={"total_return": "0.0200", "sharpe": "0.2", "cagr": "0"})
    wh.con.execute("UPDATE experiments SET created_at = TIMESTAMP '2026-10-27 22:59:00' WHERE experiment_id = ?", [new])
    events, _ = ev.build_backtest_events(wh, NOW, "r")
    assert [e["payload"]["return_pct"] for e in events] == [pytest.approx(2.0)]
    assert events[0]["event_id"] == ev.event_id_for("backtest", f"{new}|research")


def test_only_newest_fingerprint_per_kind_and_window(tmp_path):
    wh = make_wh(tmp_path)
    old_fp, new_fp = "db1856704111" + "0" * 52, "89767552ae61" + "0" * 52
    _, old = record(wh, name="buy_and_hold", params={}, fp=old_fp, desc=OHLCV_DESC)
    wh.con.execute("UPDATE experiments SET created_at = TIMESTAMP '2026-10-27 22:31:00' WHERE experiment_id = ?", [old])
    _, new = record(wh, name="buy_and_hold", params={}, fp=new_fp, desc=OHLCV_DESC)
    wh.con.execute("UPDATE experiments SET created_at = TIMESTAMP '2026-10-28 09:00:00' WHERE experiment_id = ?", [new])
    record(wh, name="buy_and_hold", params={}, fp=FP, desc=SERIES_DESC)
    events, _ = ev.build_backtest_events(wh, NOW, "r")
    got = {(e["payload"]["strategy_id"], e["payload"]["dataset_version"]) for e in events}
    assert got == {("ohlcv21-tiingo.buy_and_hold", "89767552ae61"), ("nasdaqcom.buy_and_hold", FP[:12])}


def test_newest_is_per_window(tmp_path):
    wh = make_wh(tmp_path)
    old_fp, new_fp = "db1856704111" + "0" * 52, "89767552ae61" + "0" * 52
    _, a = record(wh, name="buy_and_hold", params={}, fp=old_fp, desc=OHLCV_DESC, window="validation")
    wh.con.execute("UPDATE experiments SET created_at = TIMESTAMP '2026-10-27 22:31:00' WHERE experiment_id = ?", [a])
    record(wh, name="buy_and_hold", params={}, fp=new_fp, desc=OHLCV_DESC, window="train")
    events, _ = ev.build_backtest_events(wh, NOW, "r")
    assert sorted(e["payload"]["dataset_version"] for e in events) == ["89767552ae61", "db1856704111"]


def test_unrecognised_description_is_skipped_with_warning(tmp_path):
    wh = make_wh(tmp_path)
    record(wh, desc="mystery data")
    events, warns = ev.build_backtest_events(wh, NOW, "r")
    assert events == [] and warns


def test_long_params_shorten_deterministically_within_slug_bound(tmp_path):
    wh = make_wh(tmp_path)
    params = {"vol_target": "0.15", "vol_lookback": 60, "trend_fast": 50, "trend_slow": 200,
              "max_leverage": "1.5", "rebalance_days": 21, "min_weight": "0.01"}
    record(wh, name="vol_targeted_trend", params=params, desc=OHLCV_DESC)
    a, _ = ev.build_backtest_events(wh, NOW, "r")
    b, _ = ev.build_backtest_events(wh, NOW, "r")
    s = a[0]["payload"]["strategy_id"]
    assert len(s) <= 64 and s == b[0]["payload"]["strategy_id"]
    assert s.startswith("ohlcv21-tiingo.vol_targeted_trend")
    ev.validate_event(a[0])
    wh2 = make_wh(tmp_path, "w2.duckdb")
    record(wh2, name="vol_targeted_trend", params={**params, "vol_lookback": 61}, desc=OHLCV_DESC)
    assert ev.build_backtest_events(wh2, NOW, "r")[0][0]["payload"]["strategy_id"] != s


def test_backtest_rejected_status_and_reason_mapping(tmp_path):
    wh = make_wh(tmp_path)
    reg, exp = record(wh)
    reg.record_decision(exp, "REJECTED", "Overfit to the training period", None)
    reg2, exp2 = record(wh, name="other", params={})
    reg2.record_decision(exp2, "REJECTED", "something unusual", None)
    reg3, exp3 = record(wh, name="thin", params={})
    reg3.record_decision(exp3, "REJECTED", "insufficient evidence", None)
    events, _ = ev.build_backtest_events(wh, NOW, "r")
    by = {e["payload"]["strategy_id"]: e["payload"] for e in events}
    assert by["nasdaqcom.ma_trend-50-200"]["status"] == "rejected"
    assert by["nasdaqcom.ma_trend-50-200"]["rejection_reason"] == "overfit"
    assert by["nasdaqcom.other"]["rejection_reason"] == "underperforms_baseline"
    assert by["nasdaqcom.thin"]["rejection_reason"] == "insufficient_trades"
    for e in events:
        ev.validate_event(e)
        assert "unusual" not in json.dumps(e)


@pytest.mark.parametrize("commit", ["git-unavailable", "ABCDEF1234", "abc12", None])
def test_row_without_valid_commit_is_skipped_never_given_head(tmp_path, commit):
    wh = make_wh(tmp_path)
    record(wh)
    wh.con.execute("UPDATE experiments SET git_commit = ?", [commit])
    events, warns = ev.build_backtest_events(wh, NOW, "r")
    assert events == [] and warns


def test_backtest_event_ids_stable_across_builds_and_use_experiment_id(tmp_path):
    wh = make_wh(tmp_path)
    _, exp = record(wh)
    a, _ = ev.build_backtest_events(wh, NOW, "r")
    b, _ = ev.build_backtest_events(wh, datetime(2026, 10, 29, tzinfo=timezone.utc), "r2")
    assert a[0]["event_id"] == b[0]["event_id"]
    assert a[0]["event_id"] == ev.event_id_for("backtest", f"{exp}|research")


def test_paper_sessions_absent_table_is_empty(tmp_path):
    assert ev.build_paper_session_events(make_wh(tmp_path), NOW, "r") == ([], [])


def test_paper_sessions_skipped_with_warning_since_pnl_not_persisted(tmp_path):
    wh = make_wh(tmp_path)
    wh.con.execute(
        "CREATE TABLE paper_sessions (book_id VARCHAR, session_date DATE, strategy_id VARCHAR, "
        "equity DECIMAL(18,6), cash DECIMAL(18,6), gross_exposure DECIMAL(18,6), "
        "position_count BIGINT, fills BIGINT, completed_at TIMESTAMP)")
    wh.con.execute(
        "INSERT INTO paper_sessions VALUES ('b', DATE '2026-10-27', 'buy_and_hold', 1000, 10, 990, 1, 1, NULL)")
    events, warns = ev.build_paper_session_events(wh, NOW, "r")
    assert events == [] and any("pnl" in w.lower() for w in warns)


def test_heartbeat_lists_approved_strategies_and_build_all_order(tmp_path):
    wh = make_wh(tmp_path)
    add_bar(wh, "AAPL", date(2026, 10, 27))
    record(wh)
    wh.con.execute(
        "CREATE TABLE active_strategy (seq BIGINT, strategy_id VARCHAR, parameters VARCHAR, "
        "approved_by VARCHAR, reason VARCHAR, approved_at TIMESTAMP, book_id VARCHAR)")
    wh.con.execute("INSERT INTO active_strategy VALUES (1, 'Buy_And_Hold', '{}', 'a', 'r', NULL, 'b')")
    events, warns = ev.build_all(wh, NOW)
    assert [e["event_type"] for e in events] == ["data_health", "backtest", "heartbeat"]
    assert events[-1]["payload"]["active_strategies"] == ["buy_and_hold"]
    assert all(e["run_id"] == "research-20261028" for e in events)
    for e in events:
        ev.validate_event(e)


@pytest.mark.parametrize("field", ["period_start", "period_end"])
def test_a_bare_date_is_refused_because_the_worker_rejects_it(field):
    with pytest.raises(ev.EventInvalid):
        ev.validate_event(backtest(**{field: "2020-06-01"}))
