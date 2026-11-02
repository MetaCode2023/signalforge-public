"""Operator dashboard: every number from real state, never mistakable for live trading."""

from __future__ import annotations

import ast
import html as htmllib
import re
import threading
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pytest

from signalforge.config.models import DataSourcesConfig, ProviderConfig, load_all
from signalforge.core.interfaces import BacktestResult, EquityPoint, RiskAction, RiskEvent
from signalforge.core.types import Fill, Order, OrderStatus, OrderType, Side
from signalforge.dashboard import __main__ as cli
from signalforge.dashboard import render, serve
from signalforge.dashboard.render import render_dashboard, write_dashboard
from signalforge.data.storage import Warehouse
from tests.conftest import make_bar

D = Decimal
NOW = datetime(2026, 10, 26, 12, 0, 0)
DASHBOARD_DIR = Path(render.__file__).parent
SECTION_IDS = ("overview", "strategies", "data-health", "learning-lab", "risk-center", "trade-ledger")


def mk(name: str) -> str:
    """A distinct hostile payload per data field, so each site is proven separately."""
    return f"<script>alert('{name}')</script>"


# --- the interpolation scanner (shared with test_charts) --------------------

ESCAPERS = {"esc", "_n"}
BUILDERS = {"money", "pct", "na", "badge", "count_link"}


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _is_str_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def interpolation_violations(path: Path) -> list[str]:
    """Every `{...}` in an f-string must be an escaper, a numeric formatter, a
    builder that escapes internally, or a pre-built `*_html` fragment. Anything
    else (a bare variable, an attribute) is an unescaped site. `.format`, `%`
    and `+` string building are banned outright because they bypass this scan."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue):
            value = node.value
            name = _call_name(value)
            ok = (
                (name is not None and (name in ESCAPERS or name in BUILDERS or name.endswith("_html")))
                or (isinstance(value, ast.Name) and value.id.endswith("_html"))
            )
            if not ok:
                bad.append(f"{path.name}:{node.lineno}: {ast.unparse(value)}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "format" and _is_str_const(node.func.value):
                bad.append(f"{path.name}:{node.lineno}: str.format")
        elif isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Mod) and _is_str_const(node.left):
                bad.append(f"{path.name}:{node.lineno}: %-format")
            if isinstance(node.op, ast.Add) and (_is_str_const(node.left) or _is_str_const(node.right)):
                bad.append(f"{path.name}:{node.lineno}: string +")
    return bad


# --- fixtures ----------------------------------------------------------------

def make_curve(equities, start=date(2020, 1, 2)):
    out, peak = [], None
    for i, e in enumerate(equities):
        e = D(str(e))
        peak = e if peak is None or e > peak else peak
        out.append(EquityPoint(
            valuation_date=start + timedelta(days=i), equity=e, cash=e / 2,
            gross_exposure=e / 2, position_count=1, realized_pnl=D(0), unrealized_pnl=D(0),
            drawdown=(peak - e) / peak, peak_equity=peak,
        ))
    return out


def make_run(strategy_id="alpha", run_id="run-1", equities=None, trades=True, risk=True, symbol="AAA"):
    equities = equities or [100000, 101000, 99000, 102000, 103000, 101500, 104000, 103000, 105000, 104500, 106000, 107000]
    curve = make_curve(equities)
    orders, fills, events = [], [], []
    if trades:
        orders = [
            Order("o1", symbol, Side.BUY, 10, OrderType.MARKET, date(2020, 1, 2), strategy_id,
                  "sma cross up", status=OrderStatus.FILLED),
            Order("o2", symbol, Side.SELL, 10, OrderType.MARKET, date(2020, 1, 9), strategy_id,
                  "sma cross down", status=OrderStatus.FILLED),
            Order("o3", "BBB", Side.BUY, 5, OrderType.MARKET, date(2020, 1, 5), strategy_id,
                  "breakout entry", status=OrderStatus.REJECTED),
        ]
        fills = [
            Fill("o1", symbol, Side.BUY, 10, D("100"), D("1"), D("0.5"), date(2020, 1, 3)),
            Fill("o2", symbol, Side.SELL, 10, D("110"), D("1"), D("0.5"), date(2020, 1, 10)),
        ]
    if risk:
        events = [
            RiskEvent(date(2020, 1, 5), "max_positions", RiskAction.REJECT, "BBB", "at 20 positions", strategy_id, 5, 0),
            RiskEvent(date(2020, 1, 6), "max_positions", RiskAction.REJECT, "CCC", "at 20 positions", strategy_id, 5, 0),
            RiskEvent(date(2020, 1, 7), "breaker_daily_loss", RiskAction.REJECT, None, "daily loss limit", strategy_id),
            RiskEvent(date(2020, 1, 8), "martingale_averaging_down", RiskAction.REJECT, "AAA", "adding to a loser", strategy_id, 5, 0),
            RiskEvent(date(2020, 1, 9), "correlated_cluster_exposure", RiskAction.REJECT, "DDD", "cluster full", strategy_id, 5, 0),
            RiskEvent(date(2020, 1, 10), "single_name_exposure", RiskAction.REDUCE, "AAA", "capped", strategy_id, 50, 20),
        ]
    return BacktestResult(
        strategy_id=strategy_id, run_id=run_id, start_date=curve[0].valuation_date,
        end_date=curve[-1].valuation_date, starting_cash=D("100000"), equity_curve=curve,
        fills=fills, orders=orders, risk_events=events, parameters={"fast": 10, "slow": 50},
        data_limitations=["index close series only"], symbols=(symbol,), bar_count=len(curve),
    )


def sufficient_config():
    bundle = load_all()
    research = bundle.research.model_copy(
        update={"min_trades_for_evidence": 1, "min_observations_for_metrics": 5}
    )
    return bundle.model_copy(update={"research": research})


def clean_config():
    """Two providers whose wording cannot trip the fabricated-token scan."""
    providers = {
        "vendor_a": ProviderConfig(
            name="vendor_a", kind="bars", status="rejected", enabled=False,
            licence_note="licence terms apply", terms_url="https://a.test/terms",
            rejection_reason="rejected because the endpoint blocks automation",
        ),
        "vendor_b": ProviderConfig(
            name="vendor_b", kind="series", status="active", enabled=True,
            licence_note="public download", terms_url="https://b.test/terms",
        ),
    }
    return load_all().model_copy(update={"data_sources": DataSourcesConfig(providers=providers)})


@pytest.fixture
def wh():
    w = Warehouse(":memory:")
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 8)]  # Jan 4 and 5 missing
    bars = [
        make_bar(s, d, D("10"), source=src)
        for s, src in (("AAA", "srcone"), ("BBB", "srcone"), ("AAA", "srctwo"))
        for d in days
    ]
    w.upsert_bars(bars, retrieved_at=datetime(2026, 10, 20, 9, 0, 0))
    w.upsert_series([
        {"series_id": "SER1", "observation_date": date(2024, 1, 2) + timedelta(days=i), "source": "fred", "value": D("1.5")}
        for i in range(5)
    ])
    w.upsert_series([
        {"series_id": "SER2", "observation_date": date(2024, m, 2), "source": "fred", "value": D("2")}
        for m in (1, 2, 3)
    ])
    w.insert_flags([
        {"symbol": f"F{i}", "trade_date": date(2024, 1, 2), "source": "srcone", "rule": "gap", "detail": f"detail {i}"}
        for i in range(7)
    ])
    w.insert_quarantine([
        {"symbol": "QQQ1", "trade_date": date(2024, 1, 2), "source": "srcone", "rule": "high_below_low", "detail": "bad"},
        {"symbol": "QQQ2", "trade_date": date(2024, 1, 3), "source": "srcone", "rule": "negative_price", "detail": "bad2"},
    ])
    w.record_run(run_id="r1", provider="fred", started_at=datetime(2026, 10, 20),
                 finished_at=datetime(2026, 10, 20, 0, 1), rows_in=10, rows_accepted=9,
                 rows_quarantined=1, status="ok")
    return w


def section(html: str, sid: str) -> str:
    m = re.search(rf'<section id="{sid}".*?</section>', html, re.DOTALL)
    assert m, f"section {sid} missing"
    return m.group(0)


class _RowCounter(HTMLParser):
    VOID = {"meta", "link", "br", "hr", "img", "input", "path", "rect", "line", "circle", "polygon", "polyline"}

    def __init__(self):
        super().__init__()
        self.stack: list[str | None] = []
        self.rows: dict[str, int] = {}
        self.ids: set[str] = set()
        self.counts: list[tuple[str, str, str]] = []
        self._link: dict | None = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr" and "row" in (a.get("class") or "").split():
            for i in self.stack:
                if i:
                    self.rows[i] = self.rows.get(i, 0) + 1
        if tag in self.VOID:
            return
        if a.get("id"):
            self.ids.add(a["id"])
            self.rows.setdefault(a["id"], 0)
        self.stack.append(a.get("id"))
        if tag == "a" and "count" in (a.get("class") or "").split():
            self._link = a
            self._text = ""

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if tag == "a" and self._link is not None:
            self.counts.append((self._link["href"], self._link["data-count"], self._text.strip()))
            self._link = None
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self._link is not None:
            self._text += data


def analyse(html):
    p = _RowCounter()
    p.feed(html)
    return p


def strip_money(html: str) -> str:
    return re.sub(r'<span class="money">.*?</span>', "", html, flags=re.DOTALL)


# --- structure ---------------------------------------------------------------

def test_fresh_install_renders_all_six_sections_each_stating_no_data():
    html = render_dashboard(now=NOW)
    for sid in SECTION_IDS:
        body = section(html, sid)
        assert "DATA UNAVAILABLE" in body, sid
        assert "checked" in body.lower(), sid
    assert "No backtest or paper simulation has been recorded" in section(html, "overview")


def test_page_is_marked_simulation_only_and_legend_names_all_three_classes():
    html = render_dashboard(now=NOW)
    assert "SIMULATION ONLY" in html
    for label in ("HISTORICAL BACKTEST", "PAPER SIMULATION", "DATA UNAVAILABLE"):
        assert label in html
    for cls in ("prov-backtest", "prov-paper", "prov-na"):
        assert f".{cls}" in html


def test_provenance_classes_render_distinctly():
    html = render_dashboard(backtests=[make_run("bt")], paper_runs=[make_run("pp", run_id="paper-1")], now=NOW)
    assert 'class="prov prov-backtest"' in html
    assert 'class="prov prov-paper"' in html
    assert 'class="prov prov-na"' in html
    strategies = section(html, "strategies")
    bt_row = re.search(r'<tr class="row"[^>]*data-run="bt"[^>]*>.*?</tr>', strategies, re.DOTALL).group(0)
    pp_row = re.search(r'<tr class="row"[^>]*data-run="pp"[^>]*>.*?</tr>', strategies, re.DOTALL).group(0)
    assert "prov-backtest" in bt_row and "prov-paper" not in bt_row
    assert "prov-paper" in pp_row and "prov-backtest" not in pp_row


def test_backtest_only_never_renders_a_paper_badge():
    html = render_dashboard(backtests=[make_run()], now=NOW)
    assert 'class="prov prov-paper"' not in html
    assert 'class="prov prov-backtest"' in html


def test_every_money_figure_is_marked_simulated_and_none_escape_the_marker():
    html = render_dashboard(backtests=[make_run()], paper_runs=[make_run("pp", run_id="p")], now=NOW)
    spans = re.findall(r'<span class="money">.*?</span>', html, re.DOTALL)
    assert len(spans) > 10
    assert all("SIMULATED" in s for s in spans)
    outside = re.sub(r"<style>.*?</style>", "", strip_money(html), flags=re.DOTALL)
    assert not re.search(r"\$\s?\d", outside), re.search(r".{30}\$\s?\d.{10}", outside)


# --- overview ----------------------------------------------------------------

def test_overview_shows_paper_state_from_the_run():
    html = render_dashboard(paper_runs=[make_run("pp", run_id="p")], config=sufficient_config(), now=NOW)
    body = section(html, "overview")
    assert "$107,000.00" in body            # equity
    assert "$53,500.00" in body             # cash = equity / 2 in the fixture
    assert "7.00%" in body                  # total return
    assert "PAPER SIMULATION" in body
    assert "ACTIVE" in body                 # strategy status
    assert "Risk engine" in body
    assert "<svg" in body                   # equity curve


def test_overview_backtest_is_not_presented_as_a_live_portfolio():
    html = render_dashboard(backtests=[make_run("bt")], config=sufficient_config(), now=NOW)
    body = section(html, "overview")
    assert "HISTORICAL BACKTEST" in body
    assert "paper portfolio" in body.lower() and "DATA UNAVAILABLE" in body


def test_run_with_empty_equity_curve_says_so():
    empty = make_run("bt")
    empty.equity_curve = []
    html = render_dashboard(backtests=[empty], now=NOW)
    assert "empty equity curve" in section(html, "overview")


# --- missing values ----------------------------------------------------------

def test_missing_metrics_render_as_not_available_with_a_reason_never_blank_or_zero():
    html = render_dashboard(backtests=[make_run(trades=False, risk=False)], now=NOW)
    assert not re.search(r"<td[^>]*>\s*</td>", html)
    assert not re.search(r"<dd[^>]*>\s*</dd>", html)
    body = section(html, "strategies")
    assert "not available" in body
    assert "no closed trades" in body
    assert "no benchmark curve supplied" in body


def test_empty_ledger_and_risk_sections_state_they_were_checked():
    html = render_dashboard(backtests=[make_run(trades=False, risk=False)], now=NOW)
    ledger = section(html, "trade-ledger")
    assert "0 orders" in ledger and "checked" in ledger.lower()
    risk = section(html, "risk-center")
    assert "0 risk events" in risk and "checked" in risk.lower()


# --- evidence ----------------------------------------------------------------

def test_insufficient_evidence_is_surfaced_prominently_in_every_metric_view():
    html = render_dashboard(backtests=[make_run()], paper_runs=[make_run("pp", run_id="p")], now=NOW)
    for sid in ("overview", "strategies", "risk-center"):
        body = section(html, sid)
        assert re.search(
            r'<strong class="evidence evidence-insufficient"[^>]*>INSUFFICIENT_EVIDENCE</strong>', body
        ), sid
        assert "below the minimum" in body, sid


def test_sufficient_evidence_does_not_claim_insufficient():
    html = render_dashboard(backtests=[make_run()], config=sufficient_config(), now=NOW)
    body = section(html, "strategies")
    assert "INSUFFICIENT_EVIDENCE" not in body
    assert "SUFFICIENT" in body


# --- strategies --------------------------------------------------------------

def test_strategies_section_content():
    html = render_dashboard(backtests=[make_run("bt")], paper_runs=[make_run("pp", run_id="p")], now=NOW)
    body = section(html, "strategies")
    assert "Active paper strategy" in body
    assert "Research candidates" in body
    assert "Promotion" in body and "no promotion decision recorded" in body
    assert "index close series only" in body        # data limitation travels with the run
    assert "fast" in body and "slow" in body        # parameters


def test_benchmark_comparison_uses_supplied_curve():
    bench = make_curve([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111])
    html = render_dashboard(backtests=[make_run()], benchmark_curve=bench, config=sufficient_config(), now=NOW)
    body = section(html, "strategies")
    assert "no benchmark curve supplied" not in body
    assert "Benchmark-relative" in body


def test_no_active_paper_strategy_is_stated_not_omitted():
    body = section(render_dashboard(backtests=[make_run()], now=NOW), "strategies")
    assert "No paper simulation is running" in body


# --- data health -------------------------------------------------------------

def test_data_health_reports_real_warehouse_numbers(wh):
    html = render_dashboard(warehouse=wh, config=load_all(), now=NOW)
    body = section(html, "data-health")
    for source in ("srcone", "srctwo", "fred"):
        assert source in body
    assert "2024-01-02" in body and "2024-01-08" in body
    assert "SER1" in body
    assert "2 quarantined records" in body
    assert "7 quality flags" in body
    assert "high_below_low" in body and "gap" in body
    assert "2026-10-20" in body                      # last retrieved
    assert "STALE" in body                           # latest observation is 2024, limit is 5 days
    assert "r1" in body                              # ingest run id


def test_data_health_missing_observations_are_counted_and_unknowns_explained(wh):
    body = section(render_dashboard(warehouse=wh, config=load_all(), now=NOW), "data-health")
    assert re.search(r'data-label="Missing business days">2<', body)    # Jan 4 and Jan 5 absent
    assert re.search(r'data-label="Missing business days">0<', body)    # SER1 is gap-free
    assert "not available" in body and "irregular" in body              # SER2 is monthly


def test_data_health_without_warehouse_is_unavailable_with_reason():
    body = section(render_dashboard(config=load_all(), now=NOW), "data-health")
    assert "DATA UNAVAILABLE" in body and "no warehouse" in body.lower()


def test_refused_providers_are_listed_with_their_reasons_from_config():
    cfg = load_all()
    body = section(render_dashboard(config=cfg, now=NOW), "data-health")
    refused = [p for p in cfg.data_sources.providers.values() if p.status == "rejected"]
    assert len(refused) >= 4
    for p in refused:
        assert htmllib.escape(p.name) in body
        assert htmllib.escape(p.rejection_reason[:40]) in body
    assert "REFUSED" in body


def test_provider_status_without_config_states_it():
    body = section(render_dashboard(now=NOW), "data-health")
    assert "provider configuration was not supplied" in body.lower()


# --- learning lab ------------------------------------------------------------

def test_learning_lab_no_table_is_an_explicit_state(wh):
    body = section(render_dashboard(warehouse=wh, now=NOW), "learning-lab")
    assert "no experiments recorded yet" in body
    assert "experiments table does not exist" in body


def test_learning_lab_empty_table_is_distinct_from_absent(wh):
    wh.con.execute("CREATE TABLE experiments (experiment_id VARCHAR, status VARCHAR)")
    body = section(render_dashboard(warehouse=wh, now=NOW), "learning-lab")
    assert "no experiments recorded yet" in body
    assert "table exists with 0 rows" in body


def test_learning_lab_renders_rows_and_status_breakdown_from_the_table(wh):
    wh.con.execute("CREATE TABLE experiments (experiment_id VARCHAR, status VARCHAR, note VARCHAR)")
    wh.con.execute("INSERT INTO experiments VALUES ('e1','FAILED','overfit'),('e2','PROMOTED','ok'),('e3','FAILED','noise')")
    html = render_dashboard(warehouse=wh, now=NOW)
    body = section(html, "learning-lab")
    assert "3 experiments" in body
    assert "overfit" in body and "e2" in body
    assert "FAILED: 2" in body and "PROMOTED: 1" in body
    p = analyse(html)
    assert any(c[0] == "#experiments-table" and c[1] == "3" for c in p.counts)


def test_dashboard_does_not_import_the_learning_package():
    for path in DASHBOARD_DIR.glob("*.py"):
        assert "signalforge.learning" not in path.read_text(encoding="utf-8"), path.name


# --- risk center -------------------------------------------------------------

def test_risk_center_groups_events_by_rule_and_names_each_category():
    html = render_dashboard(backtests=[make_run()], config=load_all(), now=NOW)
    body = section(html, "risk-center")
    for rule in ("max_positions", "breaker_daily_loss", "martingale_averaging_down",
                 "correlated_cluster_exposure", "single_name_exposure"):
        assert rule in body
    for label in ("Exposure", "Worst daily loss", "Current drawdown", "Correlation concentration",
                  "Blocked orders", "Martingale violations", "Circuit-breaker history"):
        assert label in body, label
    assert "<svg" in body
    texts = [t for _, _, t in analyse(html).counts]
    assert any(t.startswith("5 blocked") for t in texts)
    assert any(t.startswith("1 martingale") for t in texts)
    assert any(t.startswith("1 correlation") for t in texts)
    assert any(t.startswith("1 circuit-breaker") for t in texts)


# --- trade ledger ------------------------------------------------------------

def test_trade_ledger_lists_orders_fills_reasons_and_realized_pnl():
    html = render_dashboard(backtests=[make_run()], paper_runs=[make_run("pp", run_id="p")], now=NOW)
    body = section(html, "trade-ledger")
    for text in ("o1", "o2", "o3", "sma cross up", "sma cross down", "breakout entry",
                 "2020-01-03", "2020-01-10", "REJECTED", "FILLED"):
        assert text in body
    assert "$98.00" in body                        # (110*10 - 1) - (100*10 + 1)
    assert "realized on exit" in body              # a BUY fill states why it has no P&L
    assert "max_positions" in body                 # risk-rule decisions
    assert "daily bars" in body.lower()            # no intraday timestamp exists; say so
    assert "PAPER SIMULATION" in body and "HISTORICAL BACKTEST" in body


# --- honesty: counts link to rows -------------------------------------------

def test_every_stated_count_links_to_a_table_with_that_many_rows(wh):
    wh.con.execute("CREATE TABLE experiments (experiment_id VARCHAR)")
    wh.con.execute("INSERT INTO experiments VALUES ('a'),('b')")
    html = render_dashboard(
        warehouse=wh, config=load_all(), backtests=[make_run()], paper_runs=[make_run("pp", run_id="p")], now=NOW
    )
    p = analyse(html)
    assert len(p.counts) >= 12
    for href, n, text in p.counts:
        target = href.lstrip("#")
        assert target in p.ids, f"{href} has no target"
        assert p.rows[target] == int(n), f"{text!r} says {n}, {href} holds {p.rows[target]}"
        assert text.startswith(f"{int(n):,}"), text
    texts = [t for _, _, t in p.counts]
    assert "7 quality flags" in texts
    assert "2 quarantined records" in texts


def test_ids_are_unique():
    html = render_dashboard(backtests=[make_run("a", "r1"), make_run("b", "r2")], now=NOW)
    ids = re.findall(r'\sid="([^"]+)"', html)
    assert len(ids) == len(set(ids))


# --- escaping ----------------------------------------------------------------

def _hostile_world():
    run = make_run(strategy_id=mk("strategy_id"), run_id=mk("run_id"), symbol=mk("symbol"))
    run.orders[0] = Order(mk("order_id"), mk("symbol"), Side.BUY, 10, OrderType.MARKET, date(2020, 1, 2),
                          mk("order_strategy"), mk("order_reason"), status=OrderStatus.FILLED)
    run.fills[0] = Fill(mk("order_id"), mk("symbol"), Side.BUY, 10, D("100"), D("1"), D("0.5"), date(2020, 1, 3))
    run.risk_events.append(RiskEvent(date(2020, 1, 11), mk("rule"), RiskAction.REJECT, mk("event_symbol"),
                                     mk("event_reason"), mk("event_strategy"), 1, 0))
    run.risk_events.append(RiskEvent(date(2020, 1, 11), "breaker_" + mk("breaker"), RiskAction.REJECT, None,
                                     mk("breaker_reason")))
    run.parameters = {mk("param_key"): mk("param_value")}
    run.data_limitations = [mk("limitation")]
    run.symbols = (mk("symbols_tuple"),)

    w = Warehouse(":memory:")
    w.upsert_bars([make_bar(mk("bar_symbol"), date(2024, 1, 2), D("10"), source=mk("bar_source"))])
    w.upsert_series([{"series_id": mk("series_id"), "observation_date": date(2024, 1, 2),
                      "source": mk("series_source"), "value": D("1")}])
    w.insert_flags([{"symbol": mk("flag_symbol"), "trade_date": date(2024, 1, 2), "source": mk("flag_source"),
                     "rule": mk("flag_rule"), "detail": mk("flag_detail")}])
    w.insert_quarantine([{"symbol": mk("q_symbol"), "trade_date": date(2024, 1, 2), "source": mk("q_source"),
                          "rule": mk("q_rule"), "detail": mk("q_detail")}])
    w.record_run(run_id=mk("ingest_id"), provider=mk("ingest_provider"), started_at=datetime(2026, 1, 1),
                 finished_at=datetime(2026, 1, 1), rows_in=1, rows_accepted=0, rows_quarantined=1,
                 status=mk("ingest_status"), error=mk("ingest_error"))
    w.con.execute("CREATE TABLE experiments (col_x VARCHAR, status VARCHAR)")
    w.con.execute("INSERT INTO experiments VALUES (?, ?)", [mk("exp_value"), mk("exp_status")])

    providers = {
        "evil": ProviderConfig(
            name=mk("provider_name"), kind="bars", status="rejected", enabled=False,
            licence_note=mk("licence"), terms_url="javascript:" + mk("terms_url"),
            rejection_reason=mk("rejection_reason"), notes=mk("provider_notes"),
        )
    }
    cfg = load_all().model_copy(update={"data_sources": DataSourcesConfig(providers=providers)})
    return run, w, cfg


HOSTILE_FIELDS = [
    "strategy_id", "run_id", "symbol", "order_id", "order_strategy", "order_reason", "rule",
    "event_symbol", "event_reason", "event_strategy", "breaker", "breaker_reason", "param_key",
    "param_value", "limitation", "symbols_tuple", "bar_symbol", "bar_source", "series_id",
    "series_source", "flag_symbol", "flag_source", "flag_rule", "flag_detail", "q_symbol",
    "q_source", "q_rule", "q_detail", "ingest_id", "ingest_provider", "ingest_status",
    "ingest_error", "exp_value", "exp_status", "provider_name", "licence", "terms_url",
    "rejection_reason", "provider_notes",
]


def test_hostile_content_is_escaped_at_every_data_site():
    run, w, cfg = _hostile_world()
    html = render_dashboard(warehouse=w, config=cfg, backtests=[run], paper_runs=[run], now=NOW)
    assert "<script" not in html
    missing = [n for n in HOSTILE_FIELDS if htmllib.escape(mk(n)) not in html]
    assert not missing, f"hostile fields never reached the page (or were mangled): {missing}"


def test_javascript_urls_never_become_links():
    _, _, cfg = _hostile_world()
    html = render_dashboard(config=cfg, now=NOW)
    assert 'href="javascript' not in html.lower()


def test_values_derived_from_data_cannot_break_out_of_attributes():
    run, w, cfg = _hostile_world()
    html = render_dashboard(warehouse=w, config=cfg, backtests=[run], now=NOW)
    for attr in re.findall(r'(?:id|href|data-[a-z-]+|title)="([^"]*)"', html):
        assert "<" not in attr and '"' not in attr


def test_every_dashboard_fstring_interpolation_is_escaped_or_prebuilt():
    violations = []
    for name in ("render.py", "sections.py", "charts.py", "serve.py", "__main__.py", "forecast_page.py", "assay_page.py"):
        violations += interpolation_violations(DASHBOARD_DIR / name)
    assert violations == []


def test_the_interpolation_scanner_actually_catches_unescaped_sites(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        'def f(x, y_html):\n    a = f"<p>{x}</p>"\n    b = f"<p>{esc(x)}{y_html}</p>"\n'
        '    c = "<p>%s</p>" % x\n    d = "<p>{}</p>".format(x)\n    e = "<p>" + x\n',
        encoding="utf-8",
    )
    found = interpolation_violations(bad)
    assert len(found) == 4
    assert not any(":3:" in v for v in found)


# --- no fabricated content ---------------------------------------------------

FABRICATED = re.compile(
    r"lorem|ipsum|placeholder|dummy|fake|\bdemo\b|\bsample\b|john doe|acme|example\.com|\bTODO\b|\bTBD\b|\bxxx+\b|\bfoo\b",
    re.IGNORECASE,
)


def test_generated_pages_contain_no_fabricated_looking_tokens(wh):
    pages = [
        render_dashboard(now=NOW),
        render_dashboard(warehouse=wh, config=clean_config(), backtests=[make_run()],
                         paper_runs=[make_run("pp", run_id="p")], now=NOW),
    ]
    for html in pages:
        assert not FABRICATED.search(html), FABRICATED.search(html)


def test_dashboard_source_contains_no_fabricated_tokens():
    for path in DASHBOARD_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not FABRICATED.search(text), (path.name, FABRICATED.search(text))


def test_a_number_on_the_page_changes_when_the_state_changes():
    a = render_dashboard(paper_runs=[make_run("pp", equities=[100000, 110000, 120000], run_id="p")], now=NOW)
    b = render_dashboard(paper_runs=[make_run("pp", equities=[100000, 90000, 80000], run_id="p")], now=NOW)
    assert "$120,000.00" in a and "$120,000.00" not in b
    assert "$80,000.00" in b


# --- mobile and theme --------------------------------------------------------

def test_css_meets_mobile_and_theme_requirements():
    html = render_dashboard(now=NOW)
    css = re.search(r"<style>(.*?)</style>", html, re.DOTALL).group(1)
    flat = re.sub(r"\s+", "", css)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert '@media(prefers-color-scheme:dark){:root:not([data-theme="light"])' in flat
    assert ':root[data-theme="dark"]' in flat
    assert re.search(r"body\{[^}]*background:var\(--bg\)", flat)
    assert "min-height:44px" in flat
    assert "font-size:16px" in flat
    assert "padding:016px" in flat
    assert "overflow-wrap:anywhere" in flat
    assert "box-sizing:border-box" in flat
    assert "td::before{content:attr(data-label)" in flat
    assert re.search(r"@media\(max-width:\d+px\)\{[^@]*display:block", flat)
    assert "<script" not in html


def test_every_table_cell_carries_a_label_for_the_stacked_card_layout(wh):
    html = render_dashboard(warehouse=wh, config=load_all(), backtests=[make_run()],
                            paper_runs=[make_run("pp", run_id="p")], now=NOW)
    cells = re.findall(r"<td[^>]*>", html)
    assert len(cells) > 50
    assert all('data-label="' in c for c in cells)


def test_page_loads_nothing_from_the_network():
    html = render_dashboard(config=clean_config(), backtests=[make_run()], now=NOW)
    assert not re.search(r'(?:src|href)="https?://', html)
    assert "@import" not in html and "url(" not in html


# --- write / cli / serve -----------------------------------------------------

def test_write_dashboard_creates_parent_dirs_and_utf8_file(tmp_path):
    out = tmp_path / "a" / "b" / "index.html"
    result = write_dashboard(out, now=NOW)
    assert Path(result) == out
    assert out.read_bytes().decode("utf-8").startswith("<!doctype html>")


def test_cli_defaults_bind_loopback_only():
    args = cli.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.serve is False
    assert str(args.db).endswith("signalforge.duckdb")


def test_server_refuses_non_loopback_hosts(tmp_path):
    for host in ("0.0.0.0", "", "::", "192.168.1.5"):
        with pytest.raises(ValueError):
            serve.make_server(tmp_path, host=host, port=0)


def test_server_binds_loopback_and_serves_the_file(tmp_path):
    (tmp_path / "index.html").write_text("<p>hello</p>", encoding="utf-8")
    server = serve.make_server(tmp_path, host="127.0.0.1", port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        t = threading.Thread(target=server.handle_request)
        t.start()
        port = server.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5).read()
        t.join(5)
        assert body == b"<p>hello</p>"
    finally:
        server.server_close()


def test_cli_writes_dashboard_from_a_real_warehouse_file(tmp_path):
    db = tmp_path / "w.duckdb"
    real = Warehouse(db)
    real.upsert_bars([make_bar("ZZZ", date(2024, 1, 2), D("5"), source="srcone"),
                      make_bar("ZZZ", date(2024, 1, 3), D("5"), source="srcone")])
    real.close()
    out = tmp_path / "build" / "index.html"
    assert cli.main(["--db", str(db), "--out", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert "ZZZ" in html and "srcone" in html and "2024-01-03" in html
    assert "REFUSED" in html                         # providers came from config/data_sources.yaml


def test_cli_with_missing_database_renders_unavailable_and_does_not_create_it(tmp_path):
    db = tmp_path / "nope.duckdb"
    out = tmp_path / "index.html"
    assert cli.main(["--db", str(db), "--out", str(out)]) == 0
    assert not db.exists()
    assert "no warehouse" in section(out.read_text(encoding="utf-8"), "data-health").lower()
