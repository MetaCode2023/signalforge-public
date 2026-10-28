"""The six dashboard sections, each built from real program state.

Rules every function here obeys:
  * Text that came from data reaches the page only through `esc`.
  * A missing value is `na(reason)`, never 0, never blank, never omitted.
  * A section with nothing to show says what it checked and what it found.
  * Every monetary figure goes through `money`, which stamps it SIMULATED.
  * A stated count is a `count_link` to a table holding exactly that many rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from signalforge.backtesting.metrics import EvidenceLevel, PerformanceMetrics, drawdown_series
from signalforge.core.interfaces import BacktestResult, RiskAction, RiskEvent
from signalforge.core.types import Side
from signalforge.dashboard.charts import (
    drawdown_chart,
    equity_chart,
    esc,
    pct,
    risk_event_chart,
)

# A phone will not read a 12 MB page. Tables are capped at this many rows and
# say so, with the true total stated rather than hidden.
MAX_TABLE_ROWS = 250

KIND_LABELS = {
    "backtest": ("prov-backtest", "HISTORICAL BACKTEST"),
    "paper": ("prov-paper", "PAPER SIMULATION"),
    "na": ("prov-na", "DATA UNAVAILABLE"),
}

EXPERIMENT_STATUS_COLUMNS = ("status", "outcome", "decision", "promotion_status", "promotion_decision")


@dataclass
class RunView:
    index: int
    kind: str
    result: BacktestResult
    metrics: PerformanceMetrics


@dataclass
class Health:
    available: bool
    reason: str
    table_counts: dict
    coverage: list
    quarantine: list
    flags: list
    ingest_runs: list


# --- primitives ---------------------------------------------------------------

def badge(kind: str) -> str:
    cls, label = KIND_LABELS[kind]
    return f'<span class="prov {esc(cls)}">{esc(label)}</span>'


def na(reason: str) -> str:
    return f'<span class="na">not available: {esc(reason)}</span>'


def money(value) -> str:
    v = Decimal(value)
    sign = "-" if v < 0 else ""
    amount = format(abs(v), ",.2f")
    return "".join((
        '<span class="money"><abbr class="sim" title="Simulated, not real money">SIMULATED</abbr> ',
        esc(sign), "$", esc(amount), "</span>",
    ))


def capped_count_link(
    total: int, rendered: int, singular: str, plural: str, target_id: str
) -> str:
    """A count link whose clickable number matches the rows at the other end.

    When a table is capped, saying "4,812 events" and landing the reader on
    250 rows is worse than offering no link at all, so the link carries the
    rendered count and the label states the real total alongside it.
    """
    noun = singular if total == 1 else plural
    if rendered >= total:
        return count_link(total, singular, plural, target_id)
    return (
        f'<a class="count" href="#{esc(target_id)}" data-count="{esc(rendered)}">'
        f"{esc(format(rendered, ','))} of {esc(format(total, ','))} {esc(noun)}</a>"
    )


def count_link(n: int, singular: str, plural: str, target_id: str) -> str:
    """A count that links to the rows it counts.

    Tables cap at MAX_TABLE_ROWS, so a raw count above the cap would land the
    reader on fewer rows than the number they clicked. The link therefore
    carries the count they will actually see and states the real total next to
    it, which keeps the contract true on a page with thousands of events.
    """
    if n > MAX_TABLE_ROWS:
        return capped_count_link(n, MAX_TABLE_ROWS, singular, plural, target_id)
    noun = singular if n == 1 else plural
    return (
        f'<a class="count" href="#{esc(target_id)}" data-count="{esc(n)}">'
        f'{esc(format(n, ","))} {esc(noun)}</a>'
    )


def empty_state_html(title: str, checked: str, result: str) -> str:
    return (
        '<div class="empty-state">'
        '<span class="prov prov-na">DATA UNAVAILABLE</span> '
        f"<strong>{esc(title)}</strong>"
        f"<p>Checked: {esc(checked)}. Result: {esc(result)}.</p>"
        "</div>"
    )


def cell_html(label: str, value_html: str) -> str:
    return f'<td data-label="{esc(label)}">{value_html}</td>'


def row_html(cells_html: Sequence[str], attrs_html: str = "") -> str:
    body_html = "".join(cells_html)
    return f'<tr class="row"{attrs_html}>{body_html}</tr>'


def table_html(
    table_id: str,
    headers: Sequence[str],
    rows_html: Sequence[str],
    empty_html: str,
    max_rows: int = MAX_TABLE_ROWS,
) -> str:
    """Render a table, capped, with the true total stated when it is capped.

    A 6,799-symbol table and fifteen runs' worth of risk events rendered four
    times each pushed the page to 12 MB, which no phone will read. Capping is
    only honest if the reader is told the total and the clickable count
    matches the rows actually present, so both numbers are published as data
    attributes and stated in a visible notice.
    """
    if not rows_html:
        return f'<div class="table-wrap" id="{esc(table_id)}">{empty_html}</div>'
    total = len(rows_html)
    shown = rows_html[:max_rows] if total > max_rows else rows_html
    head_html = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body_html = "".join(shown)
    notice_html = ""
    if total > len(shown):
        notice_html = (
            '<p class="truncated">Showing the first '
            f"{esc(format(len(shown), ','))} of {esc(format(total, ','))} rows. "
            "The remainder are in the warehouse, not omitted from it; this page "
            "caps the table so it stays readable on a phone.</p>"
        )
    return (
        f'<div class="table-wrap" id="{esc(table_id)}" '
        f'data-rendered-rows="{esc(len(shown))}" data-total-rows="{esc(total)}">'
        f"{notice_html}<table>"
        f"<thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table></div>"
    )


def kv_html(pairs_html: Sequence[tuple[str, str]]) -> str:
    body_html = "".join(f"<div><dt>{esc(k)}</dt><dd>{v_html}</dd></div>" for k, v_html in pairs_html)
    return f'<dl class="kv">{body_html}</dl>'


def section_html(section_id: str, title: str, body_html: str) -> str:
    return f'<section id="{esc(section_id)}"><h2>{esc(title)}</h2>{body_html}</section>'


def text_cell_html(label: str, value) -> str:
    return cell_html(label, esc(value))


def optional_cell_html(label: str, value, reason: str) -> str:
    return cell_html(label, na(reason) if value is None else esc(value))


def text(*parts) -> str:
    return "".join(str(p) for p in parts)


def rid(*parts) -> str:
    return "-".join(str(p) for p in parts)


def when(ts) -> str:
    if ts is None:
        return na("no time recorded")
    return esc(ts.strftime("%Y-%m-%d %H:%M UTC"))


# --- metric formatting --------------------------------------------------------

def metric_pct(value, reason: str) -> str:
    return na(reason) if value is None else pct(value)


def metric_num(value, reason: str) -> str:
    return na(reason) if value is None else esc(format(value, ".2f"))


def benchmark_reason(bench_supplied: bool) -> str:
    return (
        "no overlapping dates between the run and the supplied benchmark curve"
        if bench_supplied
        else "no benchmark curve supplied"
    )


def evidence_html(m: PerformanceMetrics) -> str:
    if m.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE:
        return (
            '<div class="evidence-box evidence-insufficient" role="alert">'
            '<strong class="evidence evidence-insufficient">INSUFFICIENT_EVIDENCE</strong> '
            f"<span>{esc(m.evidence_note)}</span></div>"
        )
    return (
        '<div class="evidence-box evidence-ok">'
        '<strong class="evidence evidence-ok">SUFFICIENT</strong> '
        "<span>evidence thresholds from the research config are met</span></div>"
    )


def run_title_html(run: RunView) -> str:
    r = run.result
    return (
        f"{badge(run.kind)} <strong>{esc(r.strategy_id)}</strong> "
        f'<span class="muted">run {esc(r.run_id)}</span>'
    )


# --- derived run facts --------------------------------------------------------

def equity_points(r: BacktestResult) -> list[tuple[date, Decimal]]:
    return [(p.valuation_date, p.equity) for p in r.equity_curve]


def worst_day(r: BacktestResult) -> tuple[Decimal, date] | None:
    worst = None
    for prev, cur in zip(r.equity_curve, r.equity_curve[1:]):
        if prev.equity > 0:
            change = cur.equity / prev.equity - 1
            if worst is None or change < worst[0]:
                worst = (change, cur.valuation_date)
    return worst


def realized_pnl_by_fill(fills) -> dict[int, Decimal | None]:
    """Average-cost realized P&L per sell fill, fees on both sides included.

    Keyed by the fill's position in the input list. Buys are absent (an entry
    realizes nothing). A sell larger than the recorded holding maps to None.
    """
    held: dict[str, list[Decimal]] = {}
    out: dict[int, Decimal | None] = {}
    for idx in sorted(range(len(fills)), key=lambda i: fills[i].fill_date):
        f = fills[idx]
        pos = held.setdefault(f.symbol, [Decimal(0), Decimal(0)])
        if f.side is Side.BUY:
            pos[0] += f.quantity
            pos[1] += f.price * f.quantity + f.fees
            continue
        if f.quantity > pos[0]:
            out[idx] = None
            continue
        basis = pos[1] * f.quantity / pos[0]
        pos[0] -= f.quantity
        pos[1] -= basis
        out[idx] = f.price * f.quantity - f.fees - basis
    return out


def rule_category(rule: str) -> str:
    if rule.startswith("breaker_"):
        return "circuit breaker"
    if rule.startswith("martingale") or rule == "pyramiding_blocked":
        return "martingale"
    if rule.startswith("correlated"):
        return "correlation"
    return "limit"


def risk_status_html(r: BacktestResult) -> str:
    if not r.risk_events:
        return na("this run logged no risk events, so the risk engine's behaviour cannot be judged from it")
    breakers = sum(1 for e in r.risk_events if rule_category(e.rule) == "circuit breaker")
    blocked = r.rejected_order_count
    if breakers:
        return esc(text("CIRCUIT BREAKER TRIPPED ", breakers, " times; ", blocked, " orders blocked"))
    return esc(text(len(r.risk_events), " risk events, ", blocked, " orders blocked, no circuit-breaker trips"))


# --- overview -----------------------------------------------------------------

def overview_card_html(run: RunView) -> str:
    r, m = run.result, run.metrics
    head_html = f'<h4>{run_title_html(run)}</h4>{evidence_html(m)}'
    if not r.equity_curve:
        return (
            f'<article class="run-card">{head_html}'
            f"<p>{na('this run has an empty equity curve, so no portfolio state exists')}</p></article>"
        )
    last = r.equity_curve[-1]
    dds = drawdown_series(r.equity_curve)
    if run.kind == "paper":
        status_html = esc(text("ACTIVE (paper simulation, last valued ", last.valuation_date.isoformat(), ")"))
        equity_label, cash_label = "Virtual equity", "Virtual cash"
    else:
        status_html = esc("RESEARCH CANDIDATE (historical backtest, not trading)")
        equity_label, cash_label = "Final equity (backtest end state)", "Cash at end of backtest"
    pairs = [
        (equity_label, money(last.equity)),
        (cash_label, money(last.cash)),
        ("Open positions", esc(last.position_count)),
        ("Total return", metric_pct(m.total_return, "fewer than 2 equity points, or starting equity not positive")),
        ("Max drawdown", pct(m.max_drawdown)),
        ("Current drawdown", pct(dds[-1])),
        ("Strategy status", status_html),
        ("Risk engine status", risk_status_html(r)),
    ]
    chart_html = equity_chart(
        equity_points(r), title=text("Equity curve, ", r.strategy_id), fmt_value=money, uid=rid("ov-eq", run.index)
    )
    return f'<article class="run-card">{head_html}{kv_html(pairs)}{chart_html}</article>'


def overview_section(runs: Sequence[RunView]) -> str:
    if not runs:
        body_html = empty_state_html(
            "No backtest or paper simulation has been recorded",
            "the runs supplied to this dashboard (backtests and paper simulations)",
            "none were supplied, so there is no portfolio equity, cash, position or drawdown to show",
        )
        return section_html("overview", "Overview", body_html)
    paper = [overview_card_html(x) for x in runs if x.kind == "paper"]
    back = [overview_card_html(x) for x in runs if x.kind == "backtest"]
    if paper:
        paper_html = "".join(paper)
    else:
        paper_html = empty_state_html(
            "Virtual paper portfolio: no paper simulation is running",
            "the paper simulation runs supplied to this dashboard",
            "none were supplied; the figures below are historical backtests, not a portfolio",
        )
    if back:
        back_html = "".join(back)
    else:
        back_html = empty_state_html(
            "Historical backtests",
            "the backtest runs supplied to this dashboard",
            "none were supplied",
        )
    body_html = (
        f"<h3>Virtual paper portfolio</h3>{paper_html}"
        f"<h3>Historical backtests</h3>{back_html}"
    )
    return section_html("overview", "Overview", body_html)


# --- strategies ---------------------------------------------------------------

def strategy_row_html(run: RunView, bench_supplied: bool) -> str:
    r, m = run.result, run.metrics
    attrs_html = f' data-run="{esc(r.strategy_id)}"'
    final_html = money(r.final_equity) if r.equity_curve else na("empty equity curve")
    cells = [
        cell_html("Strategy", f"{badge(run.kind)} <strong>{esc(r.strategy_id)}</strong> "
                              f'<span class="muted">run {esc(r.run_id)}</span>'),
        text_cell_html("Period", text(r.start_date.isoformat(), " to ", r.end_date.isoformat())),
        cell_html("Final equity", final_html),
        cell_html("Total return", metric_pct(m.total_return, "fewer than 2 equity points, or starting equity not positive")),
        cell_html("Max drawdown", pct(m.max_drawdown)),
        cell_html("Sharpe", metric_num(m.sharpe, "return variance is zero or there are fewer than 3 equity points")),
        text_cell_html("Closed trades", m.trade_count),
        cell_html("Benchmark-relative return", metric_pct(m.benchmark_relative_return, benchmark_reason(bench_supplied))),
        cell_html("Evidence", evidence_html(m)),
        cell_html("Promotion", na("no promotion decision recorded")),
    ]
    return row_html(cells, attrs_html)


def run_detail_html(run: RunView, bench_supplied: bool) -> str:
    r, m = run.result, run.metrics
    no_trades = "no closed trades"
    bench = benchmark_reason(bench_supplied)
    pairs = [
        ("Observations", esc(m.observations)),
        ("CAGR", metric_pct(m.cagr, "fewer than 2 equity points on different dates, or starting equity not positive")),
        ("Annualised volatility", metric_pct(m.annualised_volatility, "fewer than 3 equity points")),
        ("Sortino", metric_num(m.sortino, "no downside variance or fewer than 3 equity points")),
        ("Calmar", metric_num(m.calmar, "CAGR not computable, or the run never drew down")),
        ("Max drawdown duration", esc(text(m.max_drawdown_duration_days, " days, ", m.max_drawdown_duration_sessions, " sessions"))),
        ("Fills", esc(m.fill_count)),
        ("Win rate", metric_pct(m.win_rate, no_trades)),
        ("Profit factor", metric_num(m.profit_factor, "no closed trade with a loss")),
        ("Average trade return", metric_pct(m.avg_trade_return, no_trades)),
        ("Turnover", metric_num(m.turnover, "no equity history")),
        ("Exposure", metric_pct(m.exposure, "no equity history")),
        ("Transaction costs", money(m.total_transaction_costs)),
        ("Slippage", money(m.total_slippage)),
        ("Tracking error", metric_pct(m.tracking_error, bench)),
        ("Information ratio", metric_num(m.information_ratio, bench)),
    ]
    params = ", ".join(text(k, "=", v) for k, v in sorted(r.parameters.items(), key=lambda kv: str(kv[0])))
    params_html = esc(params) if params else na("this run recorded no parameters")
    limit_items_html = "".join(f"<li>{esc(x)}</li>" for x in r.data_limitations)
    limits_html = (
        f"<ul>{limit_items_html}</ul>"
        if r.data_limitations
        else na("no data limitations were recorded with this run")
    )
    symbols_html = esc(", ".join(r.symbols)) if r.symbols else na("this run recorded no symbols")
    return (
        f'<details class="run-detail"><summary>{esc(r.strategy_id)}: full metrics, parameters, limitations</summary>'
        f"<p>{run_title_html(run)}</p>{evidence_html(m)}{kv_html(pairs)}"
        f"<h5>Parameters</h5><p>{params_html}</p>"
        f"<h5>Symbols</h5><p>{symbols_html}</p>"
        f"<h5>Data limitations</h5>{limits_html}</details>"
    )


STRATEGY_HEADERS = (
    "Strategy", "Period", "Final equity", "Total return", "Max drawdown", "Sharpe",
    "Closed trades", "Benchmark-relative return", "Evidence", "Promotion",
)


def strategies_section(runs: Sequence[RunView], bench_supplied: bool) -> str:
    paper = [x for x in runs if x.kind == "paper"]
    back = [x for x in runs if x.kind == "backtest"]
    paper_empty_html = empty_state_html(
        "No paper simulation is running",
        "the paper simulation runs supplied to this dashboard",
        text(len(paper), " found, so there is no active paper strategy"),
    )
    back_empty_html = empty_state_html(
        "No research candidates recorded",
        "the backtest runs supplied to this dashboard",
        text(len(back), " found, so there are no research candidates"),
    )
    paper_table_html = table_html("strategies-paper", STRATEGY_HEADERS,
                             [strategy_row_html(x, bench_supplied) for x in paper], paper_empty_html)
    back_table_html = table_html("strategies-backtest", STRATEGY_HEADERS,
                            [strategy_row_html(x, bench_supplied) for x in back], back_empty_html)
    details_html = "".join(run_detail_html(x, bench_supplied) for x in paper + back)
    if not details_html:
        details_html = "<p>No runs to detail. Checked: 0 runs supplied.</p>"
    if bench_supplied:
        bench_html = "<p>Benchmark comparison uses the supplied benchmark curve; figures are in the Benchmark-relative return column.</p>"
    else:
        bench_html = f"<p>Benchmark comparison: {na('no benchmark curve supplied')}</p>"
    regimes_html = "".join(
        regime_details_html(x) for x in paper + back
    )
    if not regimes_html:
        regimes_html = f"<p>{na('no runs supplied, so there is nothing to split by regime')}</p>"
    body_html = (
        f"<h3>Active paper strategy</h3>{paper_table_html}"
        f"<h3>Research candidates</h3>{back_table_html}"
        f"<h3>Benchmark comparison</h3>{bench_html}"
        f"<h3>Performance by market regime</h3>{regimes_html}"
        f"<h3>Promotion status</h3><p>{na('no promotion decision recorded for any strategy in the data supplied')}</p>"
        f"<h3>Run details</h3>{details_html}"
    )
    return section_html("strategies", "Strategies", body_html)


# --- data health --------------------------------------------------------------

def freshness_html(latest: date | None, today: date, stale_after: int | None) -> str:
    if latest is None:
        return na("no observations")
    age = (today - latest).days
    stale_html = ' <strong class="stale">STALE</strong>' if stale_after is not None and age > stale_after else ""
    return f"{esc(age)} days old{stale_html}"


def missing_html(value) -> str:
    if value is None:
        return na("the series is not daily (irregular or sparse), so a business-day gap count does not apply")
    return esc(value)


def providers_html(config) -> str:
    if config is None:
        return empty_state_html(
            "Provider status",
            "the data-source configuration",
            "provider configuration was not supplied to this dashboard",
        )
    rows = []
    for p in config.data_sources.providers.values():
        if p.status == "rejected":
            status_html = '<strong class="refused">REFUSED</strong>'
            reason_html = esc(p.rejection_reason) if p.rejection_reason else na("no rejection reason recorded")
        elif p.status == "pending_key":
            status_html = "PENDING KEY"
            reason_html = esc(text("needs the environment variable ", p.api_key_env)) if p.api_key_env else na("key name not recorded")
        else:
            status_html = "ACTIVE"
            reason_html = na("not applicable to an active provider")
        cells = [
            text_cell_html("Provider", p.name),
            text_cell_html("Kind", p.kind),
            cell_html("Status", status_html),
            text_cell_html("Enabled", "yes" if p.enabled else "no"),
            cell_html("Reason", reason_html),
            cell_html("Licence note", esc(p.licence_note)),
            cell_html("Notes", esc(p.notes) if p.notes else na("no notes recorded")),
            text_cell_html("Terms", p.terms_url),
        ]
        rows.append(row_html(cells))
    refused = sum(1 for p in config.data_sources.providers.values() if p.status == "rejected")
    summary_html = (
        f"<p>{count_link(len(rows), 'provider', 'providers', 'providers-table')} configured, "
        f"{esc(refused)} of them REFUSED.</p>"
    )
    table = table_html(
        "providers-table",
        ("Provider", "Kind", "Status", "Enabled", "Reason", "Licence note", "Notes", "Terms"),
        rows,
        "<p>Checked: the configuration lists 0 providers.</p>",
    )
    return summary_html + table


def coverage_html(health: Health, today: date, stale_after: int | None) -> str:
    bars = [c for c in health.coverage if c["kind"] == "bars"]
    series = [c for c in health.coverage if c["kind"] == "series"]
    by_source: dict[str, list] = {}
    for c in bars:
        by_source.setdefault(c["source"], []).append(c)
    summary_rows, detail_html = [], ""
    for k, source in enumerate(sorted(by_source)):
        items = by_source[source]
        target = rid("cov-bars", k)
        lo, hi = min(c["min_date"] for c in items), max(c["max_date"] for c in items)
        last = max((c["last_retrieved_at"] for c in items if c["last_retrieved_at"] is not None), default=None)
        summary_rows.append(row_html([
            text_cell_html("Source", source),
            cell_html("Symbols", count_link(len(items), "symbol", "symbols", target)),
            text_cell_html("Stored rows (aggregated, not listed individually)", format(sum(c["rows"] for c in items), ",")),
            text_cell_html("First date", lo.isoformat()),
            text_cell_html("Last date", hi.isoformat()),
            cell_html("Freshness", freshness_html(hi, today, stale_after)),
            cell_html("Missing business days", esc(sum(c["missing_business_days"] for c in items))),
            cell_html("Last retrieved", when(last)),
        ]))
        sym_rows = [
            row_html([
                text_cell_html("Symbol", c["symbol"]),
                text_cell_html("Rows", format(c["rows"], ",")),
                text_cell_html("First date", c["min_date"].isoformat()),
                text_cell_html("Last date", c["max_date"].isoformat()),
                cell_html("Missing business days", esc(c["missing_business_days"])),
                cell_html("Last retrieved", when(c["last_retrieved_at"])),
            ])
            for c in items
        ]
        sym_table_html = table_html(
            target, ("Symbol", "Rows", "First date", "Last date", "Missing business days", "Last retrieved"),
            sym_rows, "<p>Checked: 0 symbols.</p>",
        )
        detail_html += (
            f"<details><summary>Symbols in {esc(source)} "
            f"({esc(format(len(items), ','))})</summary>{sym_table_html}</details>"
        )
    bars_empty_html = "<p>Checked the bars table: 0 sources hold bars.</p>"
    bars_table_html = table_html(
        "coverage-bars",
        ("Source", "Symbols", "Stored rows (aggregated, not listed individually)", "First date", "Last date",
         "Freshness", "Missing business days", "Last retrieved"),
        summary_rows, bars_empty_html,
    )
    series_rows = [
        row_html([
            text_cell_html("Series", c["symbol"]),
            text_cell_html("Source", c["source"]),
            text_cell_html("Rows", format(c["rows"], ",")),
            text_cell_html("First date", c["min_date"].isoformat()),
            text_cell_html("Last date", c["max_date"].isoformat()),
            cell_html("Freshness", freshness_html(c["max_date"], today, None if c["missing_business_days"] is None else stale_after)),
            cell_html("Missing business days", missing_html(c["missing_business_days"])),
            cell_html("Last retrieved", when(c["last_retrieved_at"])),
        ])
        for c in series
    ]
    series_table_html = table_html(
        "cov-series",
        ("Series", "Source", "Rows", "First date", "Last date", "Freshness", "Missing business days", "Last retrieved"),
        series_rows, "<p>Checked the series table: 0 series.</p>",
    )
    if stale_after is None:
        limit_html = f"<p>Stale limit: {na('config not supplied, so no staleness threshold applies')}</p>"
    else:
        limit_html = (
            f"<p>STALE means the latest observation is older than the risk limit of {esc(stale_after)} days. "
            "The limit is not applied to non-daily series, which update on their own cadence.</p>"
        )
    return (
        f"<h3>Bar coverage by source</h3>{bars_table_html}{detail_html}"
        f"<h3>Series coverage</h3><p>{count_link(len(series), 'series', 'series', 'cov-series')} stored.</p>{series_table_html}"
        f"{limit_html}"
    )


def validation_html(health: Health) -> str:
    q_rows = [
        row_html([
            optional_cell_html("Symbol", q["symbol"], "no symbol recorded"),
            optional_cell_html("Date", q["trade_date"], "no date recorded"),
            optional_cell_html("Source", q["source"], "no source recorded"), text_cell_html("Rule", q["rule"]),
            optional_cell_html("Detail", q["detail"], "no detail recorded"),
        ])
        for q in health.quarantine
    ]
    f_rows = [
        row_html([
            text_cell_html("Symbol", f["symbol"]), text_cell_html("Date", f["trade_date"]),
            text_cell_html("Source", f["source"]), text_cell_html("Rule", f["rule"]),
            optional_cell_html("Detail", f["detail"], "no detail recorded"),
        ])
        for f in health.flags
    ]
    i_rows = [
        row_html([
            optional_cell_html("Run", i["run_id"], "no run id recorded"),
            optional_cell_html("Provider", i["provider"], "no provider recorded"),
            cell_html("Started", when(i["started_at"])), cell_html("Finished", when(i["finished_at"])),
            optional_cell_html("Rows in", i["rows_in"], "not recorded"),
            optional_cell_html("Accepted", i["rows_accepted"], "not recorded"),
            optional_cell_html("Quarantined", i["rows_quarantined"], "not recorded"),
            optional_cell_html("Status", i["status"], "no status recorded"),
            optional_cell_html("Error", i["error"], "no error recorded"),
        ])
        for i in health.ingest_runs
    ]
    headers = ("Symbol", "Date", "Source", "Rule", "Detail")
    q_table_html = table_html("quarantine-table", headers, q_rows, "<p>Checked the quarantine table: 0 records.</p>")
    f_table_html = table_html("flags-table", headers, f_rows, "<p>Checked the quality_flags table: 0 flags.</p>")
    i_table_html = table_html(
        "ingest-table",
        ("Run", "Provider", "Started", "Finished", "Rows in", "Accepted", "Quarantined", "Status", "Error"),
        i_rows, "<p>Checked the ingest_runs table: 0 runs.</p>",
    )
    return (
        "<h3>Validation failures</h3>"
        f"<p>{count_link(len(q_rows), 'quarantined record', 'quarantined records', 'quarantine-table')} "
        "were rejected by validation and kept out of the bars.</p>"
        f"{q_table_html}"
        f"<p>{count_link(len(f_rows), 'quality flag', 'quality flags', 'flags-table')} "
        "mark suspect bars that were kept.</p>"
        f"{f_table_html}"
        f"<h3>Ingest runs</h3><p>{count_link(len(i_rows), 'ingest run', 'ingest runs', 'ingest-table')} recorded.</p>"
        f"{i_table_html}"
    )


def data_health_section(health: Health, config, today: date) -> str:
    stale_after = config.risk.stale_data_max_age_days if config is not None else None
    if health.available:
        counts_html = kv_html([
            ("Bar rows stored", esc(format(health.table_counts.get("bars", 0), ","))),
            ("Series rows stored", esc(format(health.table_counts.get("series", 0), ","))),
            ("Raw records archived", esc(format(health.table_counts.get("raw_records", 0), ","))),
            ("Data revisions detected", esc(format(health.table_counts.get("data_revisions", 0), ","))),
        ])
        warehouse_html = counts_html + coverage_html(health, today, stale_after) + validation_html(health)
    else:
        warehouse_html = empty_state_html(
            "Warehouse", "the warehouse database", health.reason
        )
    body_html = f"{warehouse_html}<h3>Provider status</h3>{providers_html(config)}"
    return section_html("data-health", "Data Health", body_html)


# --- learning lab -------------------------------------------------------------

def experiments_table_html(table_id: str, columns, rows) -> str:
    body = [
        row_html([
            cell_html(c, na("NULL in the experiments table") if v is None else esc(v))
            for c, v in zip(columns, r)
        ])
        for r in rows
    ]
    return table_html(table_id, columns, body, "<p>Checked: 0 rows.</p>")


def learning_section(experiments, selection_bias=None) -> str:
    bias_html = (
        selection_bias_html(selection_bias)
        if selection_bias is not None
        else empty_state_html(
            "Selection bias (multiple testing)",
            "the experiment registry for a family-wise correction",
            "no correction was supplied to this dashboard",
        )
    )
    if experiments is None:
        body_html = empty_state_html(
            "no experiments recorded yet",
            "the warehouse for a table named experiments",
            "the experiments table does not exist yet, so no experiment, candidate comparison, "
            "walk-forward result or promotion decision has been recorded",
        ) + bias_html
        return section_html("learning-lab", "Learning Lab", body_html)
    columns, rows = experiments["columns"], experiments["rows"]
    if not rows:
        body_html = empty_state_html(
            "no experiments recorded yet",
            "the experiments table",
            "the table exists with 0 rows",
        ) + bias_html
        return section_html("learning-lab", "Learning Lab", body_html)
    lower = [c.lower() for c in columns]
    status_cols = [i for i, c in enumerate(lower) if c in EXPERIMENT_STATUS_COLUMNS]
    parts = [
        f"<h3>Experiment history</h3><p>{count_link(len(rows), 'experiment', 'experiments', 'experiments-table')} recorded.</p>",
        experiments_table_html("experiments-table", columns, rows),
    ]
    if status_cols:
        failed = [
            r for r in rows
            if any("FAIL" in str(r[i]).upper() or "REJECT" in str(r[i]).upper() for i in status_cols)
        ]
        parts.append(
            "<h3>Failed experiments</h3>"
            f"<p>{count_link(len(failed), 'failed experiment', 'failed experiments', 'experiments-failed')} "
            "(a status, outcome or decision containing FAIL or REJECT).</p>"
        )
        parts.append(experiments_table_html("experiments-failed", columns, failed))
        parts.append("<h3>Outcome and promotion decisions</h3>")
        for i in status_cols:
            tally: dict[str, int] = {}
            for r in rows:
                key = "NULL" if r[i] is None else str(r[i])
                tally[key] = tally.get(key, 0) + 1
            items_html = "".join(f"<li>{esc(k)}: {esc(n)}</li>" for k, n in sorted(tally.items()))
            parts.append(f"<h4>By {esc(columns[i])}</h4><ul>{items_html}</ul>")
    else:
        parts.append(f"<h3>Failed experiments</h3><p>{na('the experiments table has no status, outcome or decision column')}</p>")
        parts.append(f"<h3>Outcome and promotion decisions</h3><p>{na('the experiments table has no status, outcome or decision column')}</p>")
    walk = [columns[i] for i, c in enumerate(lower) if "walk" in c or "oos" in c or "out_of_sample" in c]
    if walk:
        walk_html = esc(", ".join(walk))
        parts.append(f"<h3>Walk-forward performance</h3><p>Columns in the history table: {walk_html}.</p>")
    else:
        parts.append(f"<h3>Walk-forward performance</h3><p>{na('the experiments table has no walk-forward column')}</p>")
    if len(rows) >= 2:
        parts.append(f"<h3>Candidate comparisons</h3><p>{esc(len(rows))} experiments can be compared side by side in the history table above.</p>")
    else:
        parts.append(f"<h3>Candidate comparisons</h3><p>{na('fewer than 2 experiments recorded')}</p>")
    parts.append("<h3>Selection bias</h3>")
    parts.append(bias_html)
    return section_html("learning-lab", "Learning Lab", "".join(parts))


# --- risk center --------------------------------------------------------------

def event_rows(events: Sequence[RiskEvent]) -> list[str]:
    return [
        row_html([
            text_cell_html("Date", e.event_date.isoformat()),
            text_cell_html("Rule", e.rule),
            text_cell_html("Action", e.action.value),
            optional_cell_html("Symbol", e.symbol, "this event is not tied to a symbol"),
            text_cell_html("Reason", e.reason),
            optional_cell_html("Strategy", e.strategy_id, "no strategy recorded on this event"),
            optional_cell_html("Requested", e.requested_quantity, "no quantity recorded"),
            optional_cell_html("Approved", e.approved_quantity, "no quantity recorded"),
        ])
        for e in events
    ]


EVENT_HEADERS = ("Date", "Rule", "Action", "Symbol", "Reason", "Strategy", "Requested", "Approved")


def event_table_html(table_id: str, events: Sequence[RiskEvent]) -> str:
    return table_html(table_id, EVENT_HEADERS, event_rows(events), "<p>Checked: 0 matching risk events.</p>")


def risk_run_html(run: RunView, config) -> str:
    r, m = run.result, run.metrics
    i = run.index
    events = r.risk_events
    head_html = f"<h3>{run_title_html(run)}</h3>{evidence_html(m)}"
    if not events:
        return (
            f'<article class="run-card">{head_html}'
            "<p>0 risk events recorded for this run. Checked: the run's risk_events list. "
            "Absence of events does not show the risk engine was active.</p></article>"
        )
    blocked = [e for e in events if e.action is RiskAction.REJECT]
    martingale = [e for e in events if rule_category(e.rule) == "martingale"]
    correlation = [e for e in events if rule_category(e.rule) == "correlation"]
    breakers = [e for e in events if rule_category(e.rule) == "circuit breaker"]
    by_rule: dict[str, list[RiskEvent]] = {}
    for e in events:
        by_rule.setdefault(e.rule, []).append(e)

    if r.equity_curve:
        last = r.equity_curve[-1]
        dd_now = drawdown_series(r.equity_curve)[-1]
        share = last.gross_exposure / last.equity if last.equity > 0 else None
        exposure_html = money(last.gross_exposure) + (
            f" ({pct(share)} of equity)" if share is not None else f" ({na('equity not positive')})"
        )
        day = worst_day(r)
        limit_day_html = f" (limit {pct(config.risk.max_daily_loss)})" if config is not None else ""
        day_html = (
            f"{pct(day[0])} on {esc(day[1].isoformat())}{limit_day_html}" if day else na("fewer than 2 equity points")
        )
        limit_dd_html = f" (limit {pct(config.risk.max_portfolio_drawdown)})" if config is not None else ""
        dd_html = f"{pct(dd_now)}{limit_dd_html}"
        charts_html = drawdown_chart(
            equity_points(r), title=text("Underwater chart, ", r.strategy_id), uid=rid("rc-dd", i)
        )
    else:
        exposure_html = day_html = dd_html = na("this run has an empty equity curve")
        charts_html = ""
    pairs = [
        ("Exposure", exposure_html),
        ("Worst daily loss", day_html),
        ("Current drawdown", dd_html),
        ("Correlation concentration", count_link(len(correlation), "correlation-concentration event", "correlation-concentration events", rid("risk", i, "corr"))),
        ("Blocked orders", count_link(len(blocked), "blocked order", "blocked orders", rid("risk", i, "blocked"))),
        ("Martingale violations", count_link(len(martingale), "martingale violation", "martingale violations", rid("risk", i, "mart"))),
        ("Circuit-breaker history", count_link(len(breakers), "circuit-breaker event", "circuit-breaker events", rid("risk", i, "breaker"))),
        ("All risk events", count_link(len(events), "risk event", "risk events", rid("risk", i, "all"))),
    ]
    rule_rows, rule_details_html = [], ""
    for j, rule in enumerate(sorted(by_rule)):
        items = by_rule[rule]
        target = rid("risk", i, "rule", j)
        rule_rows.append(row_html([
            text_cell_html("Rule", rule),
            text_cell_html("Category", rule_category(rule)),
            cell_html("Events", count_link(len(items), "event", "events", target)),
            text_cell_html("Rejected", sum(1 for e in items if e.action is RiskAction.REJECT)),
            text_cell_html("Reduced", sum(1 for e in items if e.action is RiskAction.REDUCE)),
        ]))
        events_html = event_table_html(target, items)
        rule_details_html += f"<details><summary>Events for {esc(rule)}</summary>{events_html}</details>"
    rules_table_html = table_html(
        rid("risk", i, "rules"), ("Rule", "Category", "Events", "Rejected", "Reduced"), rule_rows,
        "<p>Checked: 0 rules fired.</p>",
    )
    counts = {rule: len(items) for rule, items in by_rule.items()}
    rule_chart_html = risk_event_chart(counts, title=text("Risk events by rule, ", r.strategy_id), uid=rid("rc-rules", i))
    groups = (
        ("Blocked orders", rid("risk", i, "blocked"), blocked),
        ("Martingale violations", rid("risk", i, "mart"), martingale),
        ("Correlation concentration events", rid("risk", i, "corr"), correlation),
        ("Circuit-breaker history", rid("risk", i, "breaker"), breakers),
        ("All risk events", rid("risk", i, "all"), events),
    )
    group_html = "".join(
        f"<details><summary>{esc(title)}</summary>{event_table_html(tid, evs)}</details>"
        for title, tid, evs in groups
    )
    return (
        f'<article class="run-card">{head_html}{kv_html(pairs)}{charts_html}{rule_chart_html}'
        f"<h4>Events grouped by rule</h4>{rules_table_html}{rule_details_html}{group_html}</article>"
    )


def risk_section(runs: Sequence[RunView], config) -> str:
    if not runs:
        body_html = empty_state_html(
            "Risk center",
            "the runs supplied to this dashboard",
            "no run was supplied, so there are no risk events, exposure or drawdown to show; "
            "0 risk events recorded",
        )
    else:
        body_html = "".join(risk_run_html(x, config) for x in runs)
    return section_html("risk-center", "Risk Center", body_html)


# --- trade ledger -------------------------------------------------------------

def ledger_run_html(run: RunView) -> str:
    r = run.result
    i = run.index
    pnl = realized_pnl_by_fill(r.fills)
    order_rows = [
        row_html([
            text_cell_html("Signal date", o.signal_date.isoformat()),
            text_cell_html("Order", o.order_id),
            text_cell_html("Strategy", o.strategy_id),
            text_cell_html("Symbol", o.symbol),
            text_cell_html("Side", o.side.value),
            text_cell_html("Quantity", o.quantity),
            text_cell_html("Type", o.order_type.value),
            cell_html("Limit price", money(o.limit_price) if o.limit_price is not None else na("market order has no limit price")),
            text_cell_html("Status", o.status.value),
            text_cell_html("Reason", o.reason),
        ])
        for o in r.orders
    ]
    fill_rows = []
    for idx, f in enumerate(r.fills):
        if f.side is Side.BUY:
            pnl_html = na("entry fill: realized on exit")
        elif pnl.get(idx) is None:
            pnl_html = na("sell exceeds the holding recorded by earlier fills")
        else:
            pnl_html = money(pnl[idx])
        fill_rows.append(row_html([
            text_cell_html("Fill date", f.fill_date.isoformat()),
            text_cell_html("Fill", f.fill_id),
            text_cell_html("Order", f.order_id),
            text_cell_html("Symbol", f.symbol),
            text_cell_html("Side", f.side.value),
            text_cell_html("Quantity", f.quantity),
            cell_html("Price", money(f.price)),
            cell_html("Fees", money(f.fees)),
            cell_html("Slippage", money(f.slippage_cost)),
            cell_html("Realized P&L", pnl_html),
        ]))
    n_orders, n_fills, n_events = len(order_rows), len(fill_rows), len(r.risk_events)
    head_html = f"<h3>{run_title_html(run)}</h3>"
    summary_html = (
        f"<p>{count_link(n_orders, 'order', 'orders', rid("ledger", i, "orders"))}, "
        f"{count_link(n_fills, 'fill', 'fills', rid("ledger", i, "fills"))}, "
        f"{count_link(n_events, 'risk decision', 'risk decisions', rid("ledger", i, "risk"))}. "
        "Checked: the run's orders, fills and risk events.</p>"
        "<p class=\"muted\">Dates are the dates of daily bars; this engine records no intraday timestamp.</p>"
    )
    orders_table_html = table_html(
        rid("ledger", i, "orders"),
        ("Signal date", "Order", "Strategy", "Symbol", "Side", "Quantity", "Type", "Limit price", "Status", "Reason"),
        order_rows, "<p>Checked: this run recorded 0 orders.</p>",
    )
    fills_table_html = table_html(
        rid("ledger", i, "fills"),
        ("Fill date", "Fill", "Order", "Symbol", "Side", "Quantity", "Price", "Fees", "Slippage", "Realized P&L"),
        fill_rows, "<p>Checked: this run recorded 0 fills.</p>",
    )
    risk_table_html = event_table_html(rid("ledger", i, "risk"), r.risk_events)
    return (
        f'<article class="run-card">{head_html}{summary_html}'
        f"<h4>Simulated orders and entry/exit reasoning</h4>{orders_table_html}"
        f"<h4>Simulated fills and realized P&amp;L</h4>{fills_table_html}"
        f"<h4>Risk-rule decisions</h4>{risk_table_html}</article>"
    )


def ledger_section(runs: Sequence[RunView]) -> str:
    if not runs:
        body_html = empty_state_html(
            "Trade ledger",
            "the runs supplied to this dashboard",
            "no run was supplied, so 0 orders and 0 fills are recorded",
        )
    else:
        body_html = "".join(ledger_run_html(x) for x in runs)
    return section_html("trade-ledger", "Trade Ledger", body_html)

def regime_details_html(run: RunView) -> str:
    """One collapsible regime breakdown for a single run."""
    inner_html = regime_table_html(f"run-{esc(run.index)}", run.metrics)
    return (
        f"<details><summary>Regimes: {esc(run.result.strategy_id)} "
        f"({esc(run.result.run_id)})</summary>{inner_html}</details>"
    )


# --- per-regime performance ---------------------------------------------------

def regime_table_html(uid: str, metrics) -> str:
    """Performance split by market regime, or an explicit unclassified state.

    An empty `regimes` mapping means no labels were supplied, never "no
    regimes found", so the two cases must read differently. Each regime
    carries its own observation count and evidence flag, because most regimes
    on a short run are thin and that has to be visible rather than implied.
    """
    table_id = f"{esc(uid)}-regimes"
    if not metrics.regimes:
        return empty_state_html(
            "Performance by market regime",
            "the run's metrics for a regime breakdown",
            "not classified: no regime labels were supplied for this run, so no "
            "split is possible. This is not the same as finding no regimes",
        )
    rows_html = []
    for label in sorted(metrics.regimes):
        regime = metrics.regimes[label]
        flag = regime.evidence.value
        evidence_html = (
            f"<strong>{esc(flag)}</strong>" if flag != "SUFFICIENT" else esc(flag)
        )
        rows_html.append(
            row_html(
                [
                    cell_html("Regime", esc(regime.label)),
                    cell_html("Sessions", esc(format(regime.observations, ","))),
                    cell_html(
                        "Total return",
                        metric_pct(regime.total_return, "no return computed"),
                    ),
                    cell_html(
                        "Volatility",
                        metric_pct(
                            regime.annualised_volatility, "fewer than 2 observations"
                        ),
                    ),
                    cell_html(
                        "Sharpe",
                        metric_num(
                            regime.sharpe,
                            "zero variance or fewer than 2 observations",
                        ),
                    ),
                    cell_html(
                        "Max drawdown",
                        metric_pct(regime.max_drawdown, "no drawdown computed"),
                    ),
                    cell_html("Evidence", evidence_html),
                ]
            )
        )
    count_html = count_link(len(rows_html), "regime", "regimes", table_id)
    note_html = (
        "Regimes are classified point-in-time: each session is placed against the "
        "terciles of the volatility observed up to that session, never against the "
        "whole history. A boundary computed over all of history would move every "
        "past label as new data arrived."
    )
    body_html = table_html(
        table_id,
        ["Regime", "Sessions", "Total return", "Volatility", "Sharpe", "Max drawdown", "Evidence"],
        rows_html,
        empty_state_html(
            "Performance by market regime",
            "the regime breakdown",
            "no regimes produced any sessions",
        ),
    )
    return (
        f"<h4>Performance by market regime {count_html}</h4>"
        f'<p class="note">{esc(note_html)}</p>'
        f"{body_html}"
    )


# --- selection bias -----------------------------------------------------------

def selection_bias_html(summary) -> str:
    """The multiple-testing correction, stated plainly.

    Testing N variants and reporting the best one is not a discovery. This
    panel exists so the best number on the page cannot be read without the
    count of how many things were tried to get it.
    """
    if summary.deflated_sharpe is None or summary.best_observed_sharpe is None:
        return empty_state_html(
            "Selection bias (multiple testing)",
            "the experiment registry for recorded Sharpe values",
            "no experiments with a computable Sharpe are recorded, so no "
            "correction can be applied",
        )
    dsr = summary.deflated_sharpe
    survives = bool(dsr.significant)
    verdict_html = (
        "<strong>survives the correction</strong>"
        if survives
        else "<strong>does NOT survive the correction</strong>"
    )
    cls = "ok" if survives else "warn"
    return (
        f'<div class="panel panel-{esc(cls)}" id="selection-bias">'
        "<h4>Selection bias (multiple testing)</h4>"
        "<p>"
        f"{esc(format(summary.n_variants, ','))} distinct variant(s) were tried "
        f"across {esc(format(summary.n_strategies, ','))} strategies in "
        f"{esc(format(summary.n_experiments, ','))} recorded experiments. The best "
        f"observed Sharpe is {esc(format(float(summary.best_observed_sharpe), '.3f'))}"
        f" ({esc(str(summary.best_strategy))})."
        "</p>"
        "<p>"
        "With that many tries, the expected best result under the null "
        f"hypothesis is {esc(format(float(dsr.expected_max_sharpe), '.3f'))}, so the "
        f"observed figure {verdict_html}: deflated Sharpe "
        f"{esc(format(float(dsr.deflated_sharpe), '.4f'))}."
        "</p>"
        '<p class="note">Reading: testing several variants and reporting the best '
        "one is not a discovery. If the observed Sharpe is not above what the "
        "luckiest of that many random tries would produce, it is not evidence of "
        "an edge.</p>"
        "</div>"
    )
