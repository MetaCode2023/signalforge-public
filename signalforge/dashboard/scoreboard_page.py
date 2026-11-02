"""The Scoreboard tab: every idea SignalForge has tested, against one bar.

The question on every row is the same: did this idea beat just buying and
holding, once costs and risk are counted? Verdicts are computed from the
recorded numbers on every render, never typed in, so the page changes when
the evidence does.

Sources, each optional. A missing source renders as DATA UNAVAILABLE or as a
queued row, never as an invented number.

- Classic strategies: the warehouse `experiments` table. One group per
  dataset fingerprint and window, and the bar is the buy-and-hold row
  recorded in that same group, so a strategy is only ever compared with a
  benchmark run on identical data. Validation and test rows only; train rows,
  walk-forward folds and the strategy-only (no drawdown breaker) variants
  stay in the operator page's Learning Lab.
- Crucible: every forecast trial record in reports/forecast_trials/. Calling
  direction is scored against the always-up base rate: a forecaster beats it
  only when the low end of its hit-rate interval clears it. When the trial ran
  with --backtest, the cone rule's Sharpe is scored against buy-and-hold's on
  the same stocks.
- Assay: every insider trial record in reports/insider_trials/. The bar is
  random insiders filing in the same months (random stocks on the same days
  for "every insider buy"). Hindsight picks are listed and never scored,
  because they use the future.

Same rules as the other tabs: static HTML, no script, nothing loaded from the
network, every interpolation escaped, motion in CSS only and stopped under
prefers-reduced-motion.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from signalforge.dashboard.charts import DATA_UNAVAILABLE, _n, esc
from signalforge.dashboard.render import CSS, load_experiments, open_readonly, tab_strip

TIE = 0.005          # Sharpe values this close are a tie, not a win
MIN_CALLS = 30       # matches research.yaml min_trades_for_evidence
WINDOWS = ("test", "validation")
WINDOW_TITLES = {"test": "sealed exam, evaluated once", "validation": "practice years"}

STRATEGIES = {
    "buy_and_hold": ("Buy and hold", "Buy once and never sell"),
    "ma_trend": ("Trend", "Own a stock only while its 50-day average is above its 200-day"),
    "vol_targeted_trend": ("Volatility-sized trend", "The trend rule, sized to a 10% yearly swing"),
    "xs_momentum": ("Momentum", "Own what rose most over the last six months"),
    "zscore_mean_reversion": ("Mean reversion", "Buy after an unusually sharp drop, sell on the bounce"),
    "tsmom": ("Asset-class trend", "Hold each fund only while its own 12-month return is above zero"),
}
FORECASTERS = {
    "random_walk": ("Coin-flip forecaster", "Random walk: no view on direction at all"),
    "block_bootstrap": ("Copycat cone forecaster", "Replays recent history, so it is momentum in disguise"),
    "kronos": ("Kronos, the AI model", "Open-source model trained on price charts"),
}
CONE_RULES = {
    "random_walk": "Cone rule, coin-flip forecaster",
    "block_bootstrap": "Cone rule, copycat forecaster",
    "kronos": "Cone rule, Kronos",
}
ARMS = {
    "smart_past": ("Copy smart insiders", "Insiders whose earlier copied buys mostly won, judged only on the past"),
    "cluster": ("Copy insider clusters", "Two or more insiders buying one company within 30 days"),
}

# verdict key -> (css class, stamp text)
VERDICTS = {
    "BEAT": ("beat", "BEAT THE BAR"),
    "AHEAD_THIN": ("thin", "AHEAD, TOO FEW TRADES"),
    "TIED": ("tied", "TIED"),
    "LOST": ("lost", "LOST"),
    "NO_EDGE": ("lost", "NO EDGE FOUND"),
    "UNCLEAR": ("tied", "TOO CLOSE TO CALL"),
    "THIN": ("thin", "TOO FEW TO JUDGE"),
    "NO_SCORE": ("tied", "NO SCORE RECORDED"),
    "QUEUED": ("queued", "QUEUED"),
    "FUTURE": ("future", "USES THE FUTURE, NOT SCORED"),
}
UNSCORED = {"QUEUED", "FUTURE", "NO_SCORE"}

EXTRA_CSS = """
.forge{background:#0b0e13;color:#e6eaf0;border:1px solid #1d232c;border-radius:14px;padding:16px;margin:16px 0;
animation:sb-rise .6s cubic-bezier(.2,.7,.2,1) both;animation-delay:var(--d,0s)}
.forge h2{color:#f3f5f8;font-family:"Arial Narrow","Roboto Condensed","Helvetica Neue",system-ui,sans-serif;
font-stretch:condensed;text-transform:uppercase;letter-spacing:.04em;font-size:1.35rem;margin:0 0 4px}
.forge p{margin:0 0 8px}.forge .sub{color:#a3adbb;font-size:14px}
.forge.fam-crucible{border-color:#3a2a1d}.forge.fam-crucible h2{color:#ff9d5c}
.forge.fam-assay{background:#0f0d09;border-color:#2a2418}.forge.fam-assay h2{color:#f0c56a}
.forge.fam-queued{border-style:dashed}
.barline{display:flex;align-items:center;gap:10px;margin:6px 0 12px;color:#ff9d5c;
font:600 13px/1.3 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.03em}
.barline::before{content:"";flex:0 0 44px;height:4px;border-radius:2px;background:#ff8a3d}
.forge table{min-width:760px}
.forge th,.forge td{border-bottom:1px solid #1a2029;color:#e6eaf0;vertical-align:middle}
.forge thead th{color:#8c96a5;font:500 12px ui-monospace,Menlo,Consolas,monospace;letter-spacing:.1em;text-transform:uppercase;white-space:nowrap}
.forge tbody th{font-weight:650;color:#f3f5f8}
.forge td.what{color:#b4bdc9;font-size:15px}
.forge td.num{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px;white-space:nowrap}
.gap{position:relative;display:block;width:180px;max-width:100%;height:14px}
.gap .mid{position:absolute;left:50%;top:-4px;bottom:-4px;width:2px;background:#ff8a3d}
.gap b{position:absolute;top:0;bottom:0;animation:sb-grow 1s cubic-bezier(.2,.7,.2,1) both .3s}
.gap b.neg{right:50%;background:#5aa2ef;border-radius:4px 0 0 4px;transform-origin:right center}
.gap b.pos{left:50%;background:#3fd18f;border-radius:0 4px 4px 0;transform-origin:left center}
.gap.none::before{content:"";position:absolute;left:0;right:0;top:6px;height:2px;
background:repeating-linear-gradient(90deg,#2a323d 0 6px,transparent 6px 12px)}
.stamp{display:inline-block;padding:2px 9px;border:1.5px solid;border-radius:6px;white-space:nowrap;
font:800 13px/1.5 "Arial Narrow","Roboto Condensed",system-ui,sans-serif;letter-spacing:.08em}
.stamp.lost{color:#ff8783;border-color:#ff6b66}.stamp.beat{color:#7fe3b2;border-color:#3fd18f}
.stamp.tied{color:#c9d0da;border-color:#6d7a8c}.stamp.thin{color:#f0b455;border-color:#f0b455}
.stamp.queued{color:#ff9d5c;border-color:#ff8a3d}.stamp.future{color:#f0c56a;border-color:#b8862b}
.hero{display:grid;gap:10px}
.hero .kick{font:600 13px ui-monospace,Menlo,Consolas,monospace;letter-spacing:.14em;color:#ff8a3d;margin:0}
.hero .q{font-size:clamp(1.8rem,5vw,3rem);line-height:1;margin:0}
.hero .answer strong{display:block;font-family:"Arial Narrow","Roboto Condensed",system-ui,sans-serif;font-stretch:condensed;
font-size:clamp(3rem,10vw,6rem);line-height:1;margin:4px 0 10px;color:#ff8a3d;animation:sb-glow 3.6s ease-in-out infinite}
.hero .answer p{color:#e3d8cf;font-size:17px;max-width:62ch}
.tiles{list-style:none;padding:0;margin:4px 0 0;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.tiles li{border:1px solid #1d232c;border-radius:10px;padding:12px;background:#10151c}
.tiles b{display:block;font-size:2rem;line-height:1.1;color:#f3f5f8;font-variant-numeric:tabular-nums}
.tiles span{font-size:14px;color:#b4bdc9}
.tiles .hot b{color:#ff8a3d}.tiles .gold b{color:#e8b44c}
.howto li{margin:6px 0;color:#c9d0da}
@keyframes sb-rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@keyframes sb-grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes sb-glow{0%,100%{text-shadow:0 0 0 rgba(255,138,61,0)}50%{text-shadow:0 0 28px rgba(255,138,61,.45)}}
@media (prefers-reduced-motion: reduce){.forge,.gap b,.hero .answer strong{animation:none}}
@media (max-width:720px){.forge table{min-width:0}.forge tbody th{display:block;padding:8px 12px 0}}
"""


@dataclass(frozen=True)
class Row:
    idea: str
    detail: str
    score: str
    gap: float | None
    verdict: str


@dataclass(frozen=True)
class Group:
    family: str
    title: str
    subtitle: str
    bar: str
    rows: tuple[Row, ...]


# --- small helpers -----------------------------------------------------------

def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stamp(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min


def _two(x: float) -> str:
    return format(x, ".2f").replace("-", "−")


def _share(x: float) -> str:
    return "".join((format(x * 100, ".1f"), "%"))


def _signed(x: float) -> str:
    return "".join(("+" if x >= 0 else "−", format(abs(x) * 100, ".2f"), "%"))


def _dollars(x: float) -> str:
    return "".join(("$", format(x, ",.0f")))


def _universe_key(description: str) -> str:
    """The dataset with its size and dates removed: a rebuilt warehouse is the same dataset, newer."""
    return re.sub(r"\d{4}-\d{2}-\d{2}|\b\d+ (?:OHLCV bars|observations)", "", description)


def _sharpe_verdict(score: float | None, bar: float | None, evidence: str = "") -> str:
    if score is None or bar is None:
        return "NO_SCORE"
    if score > bar + TIE:
        return "AHEAD_THIN" if str(evidence).upper().startswith("INSUFFICIENT") else "BEAT"
    if score < bar - TIE:
        return "LOST"
    return "TIED"


# --- classic strategies (warehouse experiments table) ------------------------

def _strategy_label(name: str, params: dict) -> str:
    base = STRATEGIES.get(name, (name, ""))[0]
    if name in ("ma_trend", "vol_targeted_trend") and "fast_window" in params and "slow_window" in params:
        return "".join((base, " ", str(params["fast_window"]), "/", str(params["slow_window"])))
    if name == "xs_momentum" and "lookback_days" in params:
        return "".join((base, " ", str(params["lookback_days"]), "/", str(params.get("skip_days", 0))))
    if name == "zscore_mean_reversion" and "entry_z" in params:
        return "".join((base, " z=", str(params["entry_z"])))
    return base


def _held(params: dict) -> str:
    symbols = params.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    if 0 < len(symbols) <= 3:
        return " ".join(("buy and hold", ", ".join(str(s) for s in symbols)))
    return "buy and hold on the same data"


def classic_groups(experiments: dict | None) -> list[Group]:
    if not experiments or not experiments.get("rows"):
        return []
    columns = list(experiments["columns"])
    by_run: dict[tuple, list[dict]] = {}
    for raw in experiments["rows"]:
        row = dict(zip(columns, raw))
        name = str(row.get("strategy_name") or "")
        description = str(row.get("dataset_description") or "")
        window = row.get("evaluated_window")
        if window not in WINDOWS or name.endswith("_nobreaker") or "limitations:" in description:
            continue
        by_run.setdefault((row.get("dataset_fingerprint"), window), []).append(row)

    newest: dict[tuple, tuple] = {}
    for (_, window), rows in by_run.items():
        if not any(r.get("strategy_name") == "buy_and_hold" for r in rows):
            continue
        when = max(_stamp(r.get("created_at")) for r in rows)
        key = (_universe_key(str(rows[0].get("dataset_description") or "")), window)
        if key not in newest or when > newest[key][0]:
            newest[key] = (when, window, rows)

    ordered = sorted(newest.values(), key=lambda t: t[0], reverse=True)
    ordered.sort(key=lambda t: WINDOWS.index(t[1]))
    groups = []
    for _, window, rows in ordered:
        latest: dict[tuple, dict] = {}
        for r in rows:
            k = (r.get("strategy_name"), json.dumps(_json(r.get("parameters")), sort_keys=True))
            if k not in latest or _stamp(r.get("created_at")) > _stamp(latest[k].get("created_at")):
                latest[k] = r
        benches = [r for r in latest.values() if r.get("strategy_name") == "buy_and_hold"]
        bench = max(benches, key=lambda r: _stamp(r.get("created_at")))
        bar = _num(_json(bench.get("metrics")).get("sharpe"))
        scored = []
        for r in latest.values():
            name = str(r.get("strategy_name"))
            if name == "buy_and_hold":
                continue
            params = _json(r.get("parameters"))
            sharpe = _num(_json(r.get("metrics")).get("sharpe"))
            score = "no score" if sharpe is None else "".join((_two(sharpe), " / ", _two(bar) if bar is not None else "n/a"))
            gap = None if sharpe is None or bar is None else sharpe - bar
            scored.append((sharpe if sharpe is not None else -math.inf, Row(
                _strategy_label(name, params), STRATEGIES.get(name, ("", ""))[1], score, gap,
                _sharpe_verdict(sharpe, bar, str(r.get("evidence_level") or "")),
            )))
        if not scored:
            continue
        scored.sort(key=lambda t: t[0], reverse=True)
        start = bench.get("test_start" if window == "test" else "validation_start")
        end = bench.get("test_end" if window == "test" else "validation_end")
        span = "" if not start or not end else "".join((str(start)[:10], " to ", str(end)[:10], ". "))
        bar_text = "no buy-and-hold Sharpe was recorded" if bar is None else "".join(
            ("bar = ", _two(bar), " Sharpe, ", _held(_json(bench.get("parameters")))))
        family_title = "Allocation," if any(r.get("strategy_name") == "tsmom" for r in rows) else "Classic strategies,"
        groups.append(Group(
            "classic", " ".join((family_title, WINDOW_TITLES[window])),
            "".join((span, str(bench.get("dataset_description") or ""))), bar_text,
            tuple(row for _, row in scored),
        ))
    return groups


# --- Crucible (forecast trial records) ----------------------------------------

def _forecaster(fid: str) -> tuple[str, str]:
    return FORECASTERS.get(fid, (fid, ""))


def crucible_groups(records: list[tuple[str, dict]]) -> list[Group]:
    groups = []
    for _, rec in records:
        fcs = [f for f in rec.get("forecasters") or [] if isinstance(f, dict)]
        if not fcs:
            continue
        first = fcs[0]
        window = first.get("window") or ["", ""]
        symbols = first.get("symbols") or []
        dataset = str((rec.get("provenance") or {}).get("dataset") or "")
        span = "".join((
            str(len(symbols)), " symbols, ", str(window[0]), " to ", str(window[-1]), ", next ",
            str(first.get("horizon", "")), " sessions, ", str(first.get("n_paths", "")), " paths. ", dataset,
        ))
        base = _num((first.get("summary") or {}).get("base_rate_up"))
        rows = []
        for f in fcs:
            s = f.get("summary") or {}
            fid = str(f.get("forecaster") or s.get("forecaster_id") or "")
            hit, n = _num(s.get("hit_rate")), int(_num(s.get("n")) or 0)
            interval = s.get("hit_interval") or [None, None]
            lo, hi = _num(interval[0]), _num(interval[-1])
            if n < MIN_CALLS:
                verdict = "THIN"
            elif hit is None or base is None:
                verdict = "NO_SCORE"
            elif lo is not None and lo > base:
                verdict = "BEAT"
            elif hi is not None and hi < base:
                verdict = "LOST"
            else:
                verdict = "UNCLEAR"
            score = "no score" if hit is None or base is None else "".join((_share(hit), " / ", _share(base)))
            label, detail = _forecaster(fid)
            rows.append(Row(label, detail, score, None if hit is None or base is None else hit - base, verdict))
        bar = "no base rate recorded" if base is None else "".join(
            ("bar = always guess UP, right ", _share(base), " of the time"))
        groups.append(Group("crucible", "Crucible, calling direction", span, bar, tuple(rows)))

        backtest = rec.get("backtest") or {}
        hold = backtest.get("buy_and_hold") or {}
        hold_sharpe = _num(hold.get("sharpe"))
        trade_rows = []
        for key, val in backtest.items():
            if not str(key).startswith("cone_") or not isinstance(val, dict):
                continue
            label, _ = _forecaster(str(key)[5:])
            sharpe = _num(val.get("sharpe"))
            final, hold_final = _num(val.get("final")), _num(hold.get("final"))
            detail = "Own a stock only while most forecast paths point up"
            if final is not None and hold_final is not None:
                detail = "".join((detail, ". Paper $100,000 became ", _dollars(final), "; holding made ", _dollars(hold_final)))
            score = "no score" if sharpe is None else "".join(
                (_two(sharpe), " / ", _two(hold_sharpe) if hold_sharpe is not None else "n/a"))
            trade_rows.append(Row(
                CONE_RULES.get(str(key)[5:], " ".join(("Cone rule,", label))),
                detail, score, None if sharpe is None or hold_sharpe is None else sharpe - hold_sharpe,
                _sharpe_verdict(sharpe, hold_sharpe),
            ))
        if trade_rows:
            bar = "no buy-and-hold Sharpe recorded" if hold_sharpe is None else "".join(
                ("bar = ", _two(hold_sharpe), " Sharpe, buy and hold the same stocks"))
            groups.append(Group("crucible", "Crucible, trading the cone", span, bar, tuple(trade_rows)))
    return groups


# --- Assay (insider trial records) -------------------------------------------

def assay_groups(records: list[tuple[str, dict]]) -> list[Group]:
    groups = []
    for _, rec in records:
        params = rec.get("params") or {}
        primary = str(params.get("primary", ""))
        arms, nulls = rec.get("arms") or {}, rec.get("nulls") or {}

        def horizon(name: str) -> dict:
            return ((arms.get(name) or {}).get("by_horizon") or {}).get(primary) or {}

        rows = []
        for name, (label, detail) in ARMS.items():
            stats, null = horizon(name), nulls.get("".join(("random_insiders_vs_", name))) or {}
            rows.append(_assay_row(label, detail, stats, null, "random insiders"))
        rows.append(_assay_row(
            "Copy every insider buy", "Every qualifying open-market purchase",
            horizon("all"), nulls.get("random_stock_same_days") or {}, "random stocks",
        ))
        hind = _num(horizon("smart_hindsight").get("mean"))
        rows.append(Row(
            "Smart insiders, picked with hindsight", "The copy-trading pitch: winners chosen after the fact",
            "no score" if hind is None else "".join((_signed(hind), " a trade")), None, "FUTURE",
        ))
        span = "".join((
            str(params.get("window_start", "")), " to ", str(params.get("window_end", "")), ", held ",
            primary, " sessions after the filing. ", str(rec.get("scored", "")), " copied trades scored.",
        ))
        groups.append(Group(
            "assay", "Assay, copying insiders", span,
            "bar = random insiders filing in the same months; random stocks for every insider buy",
            tuple(rows),
        ))
    return groups


def _assay_row(label: str, detail: str, stats: dict, null: dict, against: str) -> Row:
    observed, median = _num(null.get("observed", stats.get("mean"))), _num(null.get("median"))
    p, n = _num(null.get("p_value")), int(_num(stats.get("n")) or 0)
    if n < MIN_CALLS:
        verdict = "THIN"
    elif p is None or observed is None:
        verdict = "NO_SCORE"
    elif p < 0.05:
        verdict = "BEAT"
    else:
        verdict = "NO_EDGE"
    if observed is None or median is None:
        score = "no score"
    else:
        score = "".join((_signed(observed), " / ", _signed(median), " ", against))
    gap = None if observed is None or median is None else observed - median
    return Row(label, detail, score, gap, verdict)


# --- what is still waiting ------------------------------------------------------

def queued_group(crucible: list[tuple[str, dict]], assay: list[tuple[str, dict]]) -> Group | None:
    rows = []
    ran = {str(f.get("forecaster")) for _, rec in crucible for f in rec.get("forecasters") or [] if isinstance(f, dict)}
    if "kronos" not in ran:
        rows.append(Row(
            "Kronos, the AI model", "Needs a machine that can load the model: forecast_cli kronos-check, then trial",
            "not run", None, "QUEUED",
        ))
    if not assay:
        for label, detail in ARMS.values():
            rows.append(Row(label, "".join((detail, ". Needs the SEC data: insider_cli download, build, trial")),
                            "not run", None, "QUEUED"))
    if not rows:
        return None
    return Group("queued", "Waiting to run", "Built and tested, but not yet run on real data.",
                 "bar = set by the test that runs them", tuple(rows))


# --- loading ---------------------------------------------------------------------

def load_records(folder: Path, kind: str) -> tuple[list[tuple[str, dict]], list[str]]:
    """Every record of one kind in a folder, by file name. Unreadable files are reported, not skipped silently."""
    found, problems = [], []
    if not folder.is_dir():
        return found, problems
    for path in sorted(folder.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append("".join((path.name, " could not be read: ", type(exc).__name__)))
            continue
        if isinstance(rec, dict) and rec.get("kind") == kind:
            found.append((path.name, rec))
    return found, problems


# --- rendering ---------------------------------------------------------------------

def _gap_html(gap: float | None, scale: float) -> str:
    if gap is None:
        return '<span class="gap none" aria-hidden="true"><i class="mid"></i></span>'
    width = min(abs(gap) / scale, 1.0) * 50 if scale > 0 else 0.0
    side_html = "pos" if gap > 0 else "neg"
    return (
        f'<span class="gap" aria-hidden="true"><b class="{side_html}" style="width:{_n(width)}%"></b>'
        '<i class="mid"></i></span>'
    )


def _row_html(row: Row, scale: float) -> str:
    css, word = VERDICTS[row.verdict]
    gap_html = _gap_html(row.gap, scale)
    return (
        f'<tr><th scope="row">{esc(row.idea)}</th>'
        f'<td class="what" data-label="What it does">{esc(row.detail)}</td>'
        f'<td class="num" data-label="Score vs bar">{esc(row.score)}</td>'
        f'<td data-label="Gap to the bar">{gap_html}</td>'
        f'<td data-label="Verdict"><span class="stamp {esc(css)}">{esc(word)}</span></td></tr>'
    )


def group_html(group: Group, index: int) -> str:
    gaps = [abs(r.gap) for r in group.rows if r.gap is not None]
    scale = max(gaps) if gaps else 0.0
    rows_html = "".join(_row_html(r, scale) for r in group.rows)
    return (
        f'<section class="forge fam-{esc(group.family)}" style="--d:{_n(index * 0.08)}s">'
        f"<h2>{esc(group.title)}</h2><p class=\"sub\">{esc(group.subtitle)}</p>"
        f'<p class="barline">{esc(group.bar)}</p>'
        '<div class="table-wrap"><table><thead><tr><th scope="col">Idea</th><th scope="col">What it does</th>'
        '<th scope="col">Score vs bar</th><th scope="col">Gap to the bar</th><th scope="col">Verdict</th>'
        f"</tr></thead><tbody>{rows_html}</tbody></table></div></section>"
    )


def tally(groups: list[Group]) -> dict[str, int]:
    rows = [r for g in groups for r in g.rows]
    return {
        "scored": sum(1 for r in rows if r.verdict not in UNSCORED),
        "beat": sum(1 for r in rows if r.verdict == "BEAT"),
        "queued": sum(1 for r in rows if r.verdict == "QUEUED"),
    }


def hero_html(groups: list[Group]) -> str:
    t = tally(groups)
    if t["scored"] == 0:
        answer, line = "No results yet.", "Nothing has been scored. Run a research or trial command, then rebuild the dashboard."
    elif t["beat"] == 0:
        answer = "Not yet."
        line = "".join((str(t["scored"]), " results on the board. None cleared the bar once costs and risk were counted."))
    else:
        answer = "".join((str(t["beat"]), " cleared it."))
        line = (
            "Clearing the bar on one window is a lead, not a promotion. The promotion gate also needs walk-forward "
            "evidence and a named human approver, and nothing is promoted by this page."
        )
    return (
        '<section class="forge hero" id="answer"><p class="kick">THE QUESTION ON EVERY ROW</p>'
        '<h2 class="q">Can any idea beat just buying and holding?</h2>'
        f'<div class="answer"><strong>{esc(answer)}</strong><p>{esc(line)}</p></div>'
        f'<ul class="tiles"><li><b>{esc(t["scored"])}</b><span>results scored</span></li>'
        f'<li class="hot"><b>{esc(t["beat"])}</b><span>cleared the bar</span></li>'
        f'<li class="gold"><b>{esc(t["queued"])}</b><span>queued for real data</span></li></ul></section>'
    )


HOWTO_SECTION = (
    '<section class="forge howto" id="how-to-read"><h2>How to read this</h2><ul>'
    "<li>Each table has its own bar, because each test uses different stocks and years. A strategy is only "
    "compared with a benchmark run on the same data.</li>"
    "<li>Strategy scores are Sharpe ratios: return per unit of bumpiness, after slippage and commission. Higher is "
    "better. Within 0.005 of the bar is a tie.</li>"
    "<li>A forecaster beats the bar only when the low end of its 95% interval clears the always-up rate. An "
    "insider strategy beats it only when fewer than 5% of random draws do as well.</li>"
    "<li>Every verdict is worked out from the recorded numbers each time this page is built. Nothing here is "
    "typed in by hand, and nothing here places a trade.</li></ul></section>"
)


def render_scoreboard_page(
    *,
    experiments: dict | None = None,
    crucible: list[tuple[str, dict]] = (),
    assay: list[tuple[str, dict]] = (),
    notes: list[str] = (),
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
    crucible, assay = list(crucible), list(assay)
    groups = classic_groups(experiments) + crucible_groups(crucible) + assay_groups(assay)
    waiting = queued_group(crucible, assay)
    if waiting is not None:
        groups.append(waiting)
    classic_missing_html = ""
    if not any(g.family == "classic" for g in groups):
        classic_missing_html = (
            '<section id="classic-empty"><h2>Classic strategies</h2>'
            f'<p class="empty-state">{esc(DATA_UNAVAILABLE)}: no validation or test results with a buy-and-hold '
            "benchmark were found in the warehouse. Run <code>python -m signalforge.research_cli</code>, then "
            "rebuild the dashboard.</p></section>"
        )
    notes_html = "".join(f'<p class="empty-state">{esc(n)}</p>' for n in notes)
    groups_html = "".join(group_html(g, i) for i, g in enumerate(groups))
    hero_section_html = hero_html(groups)
    css_html = "".join((CSS, EXTRA_CSS))
    howto_html = HOWTO_SECTION
    tabs_html = tab_strip("scoreboard.html")
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>SIGNALFORGE Scoreboard</title>"
        f"<style>{css_html}</style></head><body>"
        "<header><h1>SIGNALFORGE operator dashboard</h1>"
        '<p class="sim-banner" role="note"><strong>SIMULATION ONLY.</strong> '
        "Every score here comes from replaying history with paper money. No order is sent to a broker.</p>"
        f"{tabs_html}"
        f'<p class="muted">Rendered {esc(now.strftime("%Y-%m-%d %H:%M"))} local time from the warehouse experiment '
        "registry and the trial records in reports/.</p>"
        f"{notes_html}</header>"
        f"<main>{hero_section_html}{classic_missing_html}{groups_html}{howto_html}</main>"
        '<footer><p class="muted">Static page. It loads nothing from the network and runs no script. Every name '
        "tested survived to 2026, which flatters every row, the bar included.</p></footer>"
        "</body></html>"
    )


def write_scoreboard_page(path, **kwargs) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_scoreboard_page(**kwargs), encoding="utf-8")
    return out


def build_scoreboard(out_path, *, warehouse=None, reports_dir=Path("reports"), notes=()) -> Path:
    """Gather every source and write the page. Used by the dashboard build and after each trial."""
    reports = Path(reports_dir)
    crucible, bad_c = load_records(reports / "forecast_trials", "forecast_trial")
    assay, bad_a = load_records(reports / "insider_trials", "insider_assay")
    return write_scoreboard_page(
        out_path, experiments=load_experiments(warehouse), crucible=crucible, assay=assay,
        notes=[*notes, *bad_c, *bad_a],
    )


def refresh_scoreboard(out_path, db_path, reports_dir=Path("reports")) -> Path | None:
    """Rewrite the scoreboard after a trial. A trial's own result never depends on this succeeding."""
    warehouse, notes = None, []
    try:
        if Path(db_path).exists():
            warehouse = open_readonly(db_path)
        else:
            notes.append(" ".join(("No warehouse at", str(db_path), "so classic strategies are not shown.")))
    except Exception as exc:  # a locked or unreadable warehouse only hides the classic rows
        notes.append(" ".join(("The warehouse could not be opened:", type(exc).__name__)))
    try:
        return build_scoreboard(out_path, warehouse=warehouse, reports_dir=reports_dir, notes=notes)
    except Exception as exc:  # never let a page refresh fail the trial that produced the result
        print("scoreboard not refreshed:", type(exc).__name__, exc)
        return None
    finally:
        if warehouse is not None:
            warehouse.con.close()
