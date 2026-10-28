"""Dashboard renderer: program state in, one static HTML page out.

Nothing here invents a value. The page is built from the warehouse, the run
results handed in, and the loaded configuration; whatever is absent is
rendered as an explicit "DATA UNAVAILABLE" with the reason it is absent.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

import duckdb

from signalforge.backtesting.metrics import compute_metrics
from signalforge.dashboard.charts import esc
from signalforge.dashboard.sections import (
    Health,
    RunView,
    data_health_section,
    ledger_section,
    learning_section,
    overview_section,
    risk_section,
    strategies_section,
)
from signalforge.data.storage import Warehouse

COUNT_SQL = {
    "bars": "SELECT count(*) FROM bars",
    "series": "SELECT count(*) FROM series",
    "raw_records": "SELECT count(*) FROM raw_records",
    "data_revisions": "SELECT count(*) FROM data_revisions",
}

CSS = """
:root{--bg:#f6f7f9;--card:#ffffff;--fg:#14171c;--muted:#5a6270;--line:#d5d9e0;--accent:#1f5fbf;
--bad:#b3261e;--ok:#1b7a3e;--warn:#8a5a00;--sim-bg:#fff3d6;--sim-fg:#6b4500;color-scheme:light dark}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0e1116;--card:#171b22;--fg:#e8ebf0;
--muted:#9aa3b2;--line:#2c333d;--accent:#7aa7ff;--bad:#ff8a80;--ok:#6fd695;--warn:#f0b455;
--sim-bg:#3a2d10;--sim-fg:#ffd98a}}
:root[data-theme="dark"]{--bg:#0e1116;--card:#171b22;--fg:#e8ebf0;--muted:#9aa3b2;--line:#2c333d;
--accent:#7aa7ff;--bad:#ff8a80;--ok:#6fd695;--warn:#f0b455;--sim-bg:#3a2d10;--sim-fg:#ffd98a}
*{box-sizing:border-box}
html{font-size:16px;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
overflow-wrap:anywhere}
header,main,footer{max-width:1100px;margin:0 auto;padding:0 16px}
h1{font-size:1.5rem;margin:16px 0 8px}
h2{font-size:1.25rem;margin:0 0 12px}
h3{font-size:1.05rem;margin:20px 0 8px}
h4,h5{margin:12px 0 6px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0;
max-width:100%}
a{color:var(--accent)}
nav{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
nav a,a.count,summary{display:inline-flex;align-items:center;min-height:44px;padding:0 8px}
summary{display:flex;cursor:pointer}
input,select,button,textarea{font-size:16px;min-height:44px}
.muted{color:var(--muted)}
.sim-banner{border:2px solid var(--warn);background:var(--sim-bg);color:var(--sim-fg);border-radius:8px;
padding:8px 12px;margin:8px 0}
.prov,.key{display:inline-block;padding:2px 8px;border-radius:6px;font-size:13px;font-weight:700;
letter-spacing:.02em;border:2px solid var(--line)}
.prov-backtest{border:2px solid var(--accent);color:var(--accent)}
.prov-paper{border:2px dashed var(--warn);color:var(--warn)}
.prov-na{border:2px dotted var(--muted);color:var(--muted);background:repeating-linear-gradient(135deg,transparent 0 6px,var(--line) 6px 7px)}
.legend{list-style:none;padding:0;margin:8px 0}
.legend li{margin:6px 0}
.money{white-space:nowrap}
.sim{font-size:11px;font-weight:700;letter-spacing:.04em;background:var(--sim-bg);color:var(--sim-fg);
border-radius:4px;padding:1px 4px;text-decoration:none}
.na{color:var(--muted);font-style:italic}
.empty-state{border:2px dotted var(--muted);border-radius:8px;padding:12px;margin:8px 0}
.evidence-box{border-radius:8px;padding:8px 12px;margin:8px 0;border:2px solid var(--line)}
.evidence-insufficient{border-color:var(--bad);color:var(--bad)}
strong.evidence-insufficient{font-size:1.1rem;letter-spacing:.03em}
.evidence-ok{border-color:var(--ok)}
.refused{color:var(--bad)}
.stale{color:var(--bad)}
.run-card{border:1px solid var(--line);border-radius:10px;padding:12px;margin:12px 0}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:8px 0}
.kv div{border:1px solid var(--line);border-radius:8px;padding:8px}
.kv dt{font-size:13px;color:var(--muted)}
.kv dd{margin:0;font-weight:600}
.table-wrap{max-width:100%;overflow-x:auto}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:13px;color:var(--muted)}
svg{display:block;max-width:100%;height:auto}
figure.chart{margin:12px 0}
figcaption{font-size:14px;color:var(--muted)}
.chart-list{margin:4px 0;padding-left:20px}
.eq-line{stroke:var(--accent);stroke-width:2.5;fill:none}
.dd-shade{fill:var(--bad);opacity:.28}
.uw-area{fill:var(--bad);opacity:.55}
.bar{fill:var(--accent)}
.axis{stroke:var(--muted);stroke-width:1}
.tick,.label{fill:var(--fg);font-size:15px}
@media (max-width:720px){
table,thead,tbody,tr,td{display:block}
thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
tr{border:1px solid var(--line);border-radius:8px;margin:0 0 12px;padding:4px 0}
td{border:0;padding:6px 12px}
td::before{content:attr(data-label);display:block;font-size:13px;color:var(--muted);text-transform:uppercase}
section{padding:12px}
}
"""

NAV = (
    ("overview", "Overview"),
    ("strategies", "Strategies"),
    ("data-health", "Data Health"),
    ("learning-lab", "Learning Lab"),
    ("risk-center", "Risk Center"),
    ("trade-ledger", "Trade Ledger"),
)


def open_readonly(path) -> Warehouse:
    """Open an existing warehouse file without creating or altering anything."""
    wh = Warehouse.__new__(Warehouse)
    wh.con = duckdb.connect(str(path), read_only=True)
    return wh


def load_health(warehouse, reason: str) -> Health:
    if warehouse is None:
        return Health(False, reason, {}, [], [], [], [])
    con = warehouse.con
    try:
        counts = {name: con.execute(sql).fetchone()[0] for name, sql in COUNT_SQL.items()}
        coverage = warehouse.coverage_report()
        quarantine = [
            {"symbol": s, "trade_date": d.isoformat() if d else None, "source": src, "rule": rule, "detail": detail}
            for s, d, src, rule, detail in con.execute(
                "SELECT symbol, trade_date, source, rule, detail FROM quarantine "
                "ORDER BY trade_date, symbol, rule"
            ).fetchall()
        ]
        flags = [
            {"symbol": s, "trade_date": d.isoformat(), "source": src, "rule": rule, "detail": detail}
            for s, d, src, rule, detail in con.execute(
                "SELECT symbol, trade_date, source, rule, detail FROM quality_flags "
                "ORDER BY trade_date, symbol, rule"
            ).fetchall()
        ]
        runs = [
            {"run_id": a, "provider": b, "started_at": c, "finished_at": d, "rows_in": e,
             "rows_accepted": f, "rows_quarantined": g, "status": h, "error": i}
            for a, b, c, d, e, f, g, h, i in con.execute(
                "SELECT run_id, provider, started_at, finished_at, rows_in, rows_accepted, "
                "rows_quarantined, status, error FROM ingest_runs ORDER BY started_at"
            ).fetchall()
        ]
    except duckdb.Error as exc:
        return Health(False, "".join(("the warehouse could not be read: ", str(exc))), {}, [], [], [], [])
    return Health(True, "", counts, coverage, quarantine, flags, runs)


def load_experiments(warehouse):
    if warehouse is None:
        return None
    con = warehouse.con
    try:
        found = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = 'experiments'"
        ).fetchone()[0]
        if not found:
            return None
        cursor = con.execute("SELECT * FROM experiments")
        return {"columns": [d[0] for d in cursor.description], "rows": cursor.fetchall()}
    except duckdb.Error:
        return None


def render_dashboard(
    *,
    warehouse=None,
    backtests: Sequence = (),
    paper_runs: Sequence = (),
    config=None,
    benchmark_curve=None,
    now: datetime | None = None,
    warehouse_note: str | None = None,
    regime_labels=None,
    selection_bias=None,
) -> str:
    now = now or datetime.now()
    research = config.research if config is not None else None
    runs: list[RunView] = []
    for kind, results in (("paper", paper_runs), ("backtest", backtests)):
        for result in results:
            metrics = compute_metrics(
                result,
                benchmark_curve=benchmark_curve,
                config=research,
                regime_labels=regime_labels,
            )
            runs.append(RunView(len(runs), kind, result, metrics))
    reason = warehouse_note or "no warehouse was supplied to this dashboard"
    health = load_health(warehouse, reason)
    sections_html = "".join((
        overview_section(runs),
        strategies_section(runs, bool(benchmark_curve)),
        data_health_section(health, config, now.date()),
        learning_section(load_experiments(warehouse), selection_bias),
        risk_section(runs, config),
        ledger_section(runs),
    ))
    nav_html = "".join(f'<a href="#{esc(sid)}">{esc(label)}</a>' for sid, label in NAV)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    css_html = CSS
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>SIGNALFORGE operator dashboard</title>"
        f"<style>{css_html}</style></head><body>"
        "<header><h1>SIGNALFORGE operator dashboard</h1>"
        '<p class="sim-banner" role="note"><strong>SIMULATION ONLY.</strong> '
        "Nothing on this page is live trading. No order is sent to a broker and no money moves. "
        "Every monetary figure is marked SIMULATED.</p>"
        f"<p class=\"muted\">Rendered {esc(stamp)} local time from the warehouse, the supplied runs and the loaded configuration.</p>"
        '<ul class="legend">'
        '<li><span class="key prov-backtest">HISTORICAL BACKTEST</span> a strategy replayed over past data. It is not a record of trading.</li>'
        '<li><span class="key prov-paper">PAPER SIMULATION</span> a forward simulation against a virtual book. Still not real money.</li>'
        '<li><span class="key prov-na">DATA UNAVAILABLE</span> nothing exists to show; the reason is stated beside it.</li>'
        "</ul>"
        f"<nav>{nav_html}</nav></header>"
        f"<main>{sections_html}</main>"
        '<footer><p class="muted">Static page. It loads nothing from the network and runs no script.</p></footer>'
        "</body></html>"
    )


def write_dashboard(path, **kwargs) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_dashboard(**kwargs), encoding="utf-8")
    return out
