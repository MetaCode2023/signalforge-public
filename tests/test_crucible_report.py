"""The forecast_trial report that feeds the Cloudflare dashboard's Crucible tab.

It must pass the local validator (which mirrors the Worker), carry no price
level, fit the ingest size cap, and, where Node is installed, be accepted by
the Worker's own validator and render on the Worker's Crucible page with no
script. The Worker and Python validators are two copies of one contract, so
the cross-language test is the one that catches drift.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from signalforge.forecasting.baselines import BlockBootstrapForecaster, DriftlessRandomWalk
from signalforge.forecasting.paths import ContextWindow
from signalforge.forecasting.report import backtest_payload, cone_payload, trial_record
from signalforge.forecasting.trial import run_trial
from signalforge.reports import events as ev
from tests.test_forecasting import gbm_history

ROOT = Path(__file__).resolve().parents[1]
API_JS = ROOT / "cloudflare" / "signalforge-research-api" / "index.js"
DASH_JS = ROOT / "cloudflare" / "signalforge-dashboard" / "index.js"
NOW = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def event():
    history, days = gbm_history(n=1400, seed=9)
    fs = (DriftlessRandomWalk(250), BlockBootstrapForecaster(250))
    results = [run_trial(history, ["SYN"], f, start=days[300], end=days[-1], horizon=24, n_paths=100) for f in fs]
    record = trial_record(results, null_id="random_walk")
    win = ContextWindow.from_view(history.view(days[-1]), "SYN", 250)
    fc = fs[1].sample_paths(win, 24, 100, 3)
    backtest = {
        "buy_and_hold": {"equity": list(np.linspace(1e5, 2.6e5, 400)), "final": 2.6e5, "sharpe": 0.86},
        "cone_block_bootstrap": {"equity": list(np.linspace(1e5, 1.1e5, 400)), "final": 1.1e5, "sharpe": None},
    }
    return ev.build_forecast_trial(
        record, NOW, commit_sha="394941c", symbols=["SYN"], window=(days[300], days[-1]),
        horizon=24, n_paths=100, trend_agreement=0.65, cone=cone_payload(fc, 0.65),
        backtest=backtest_payload(backtest, days[300], days[-1]),
    )


def test_report_validates_and_fits_the_size_cap(event):
    ev.validate_event(event)
    assert len(ev.serialize(event)) < ev.MAX_BODY_BYTES
    assert event["event_type"] == "forecast_trial" and event["run_id"] == "crucible-20261101"


def test_the_cone_is_returns_not_prices(event):
    cone = event["payload"]["cone"]
    values = cone["p10"] + cone["p50"] + cone["p90"] + [v for row in cone["paths"] for v in row]
    assert all(-1 <= v <= 10 for v in values)
    assert len(cone["paths"]) == 60 and cone["n_paths"] == 100
    assert "last_close" not in json.dumps(event)


def test_a_smuggled_price_field_is_refused(event):
    bad = json.loads(ev.serialize(event))
    bad["payload"]["cone"]["last_close"] = 755.72
    with pytest.raises(ev.EventInvalid, match="last_close is not allowed"):
        ev.validate_event(bad)


def test_cone_series_must_match_the_horizon(event):
    bad = json.loads(ev.serialize(event))
    bad["payload"]["cone"]["p50"].pop()
    with pytest.raises(ev.EventInvalid, match="p50"):
        ev.validate_event(bad)


def test_equity_is_thinned_and_sharpe_left_out_when_unknown(event):
    bt = event["payload"]["backtest"]
    assert len(bt["hold_equity"]) == 160 and bt["hold_equity"][0] == 100000.0
    assert "rule_sharpe" not in bt and bt["hold_sharpe"] == 0.86


def test_thin_buckets_carry_no_rate(event):
    rw = next(f for f in event["payload"]["forecasters"] if f["id"] == "random_walk")
    empty = [b for b in rw["agreement"] if b["n"] == 0]
    assert empty and all("hit_rate" not in b for b in empty)
    assert all(re.fullmatch(r"\d{1,3}-\d{1,3}", b["label"]) for b in rw["agreement"])


NODE_CHECK = r"""
import { validateEnvelope } from %(api)s;
import { renderCrucible } from %(dash)s;
import fs from "node:fs";
const e = JSON.parse(fs.readFileSync(%(path)s, "utf8"));
const issue = validateEnvelope(e);
const stored = { ...e, received_at: "2026-11-01T03:40:00.000Z", is_test: false };
const api = { fetch: async () => new Response(JSON.stringify({ ok: true, events: [stored] })) };
const r = await renderCrucible({ API: api, SIGNALFORGE_READ_TOKEN: "x".repeat(40) });
const html = await r.text();
console.log(JSON.stringify({ issue, status: r.status, scripts: (html.match(/<script/gi) || []).length,
  paths: (html.match(/class="p (up|dn)"/g) || []).length, csp: r.headers.get("content-security-policy") }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_worker_accepts_the_report_and_renders_it_without_script(event, tmp_path):
    path = tmp_path / "event.json"
    path.write_bytes(ev.serialize(event))
    script = NODE_CHECK % {
        "api": json.dumps(API_JS.as_uri()), "dash": json.dumps(DASH_JS.as_uri()), "path": json.dumps(str(path)),
    }
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["issue"] is None
    assert result["status"] == 200 and result["scripts"] == 0 and result["paths"] == 60
    assert "script-src" not in result["csp"] and "default-src 'none'" in result["csp"]


def test_migration_keeps_rows_and_allows_only_the_new_type():
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.executescript(
        """CREATE TABLE "reports" (event_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
        event_type TEXT NOT NULL, run_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
        received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), body_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL, is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0,1)),
        CHECK (event_type IN ('heartbeat','paper_session','backtest','risk_event','data_health','experiment','validation_failure')));
        INSERT INTO reports(event_id,schema_version,event_type,run_id,occurred_at,body_sha256,payload_json)
        VALUES ('a',1,'heartbeat','r','t','s','{}'),('b',1,'backtest','r','t','s','{}');"""
    )
    sql = (ROOT / "cloudflare" / "signalforge-research-api" / "migrations" / "0002_allow_forecast_trial.sql").read_text()
    con.executescript(sql)
    assert con.execute("SELECT count(*) FROM reports").fetchone()[0] == 2
    con.execute("INSERT INTO reports(event_id,schema_version,event_type,run_id,occurred_at,body_sha256,payload_json) "
                "VALUES ('c',1,'forecast_trial','r','t','s','{}')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO reports(event_id,schema_version,event_type,run_id,occurred_at,body_sha256,payload_json) "
                    "VALUES ('d',1,'nonsense','r','t','s','{}')")
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
    assert names == {"idx_reports_received", "idx_reports_run_time", "idx_reports_type_time"}
