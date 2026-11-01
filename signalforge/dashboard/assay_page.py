"""The Assay tab: does copying company insiders beat the market once hindsight is removed?

Same rules as the operator page and Crucible: static HTML, no script, nothing
loaded from the network, every chart a <figure> with a caption that states
its numbers, every interpolation escaped, and DATA UNAVAILABLE when no trial
has run. Motion is CSS only and stops under prefers-reduced-motion.

The page renders from the trial record (reports/insider_trials/*.json), so
it can be rebuilt from a saved record without re-running the study.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from signalforge.dashboard.charts import DATA_UNAVAILABLE, _n, esc
from signalforge.dashboard.render import CSS, tab_strip

ARMS = ("all", "cluster", "smart_past", "smart_hindsight")
SHORT = {
    "all": "Every insider buy",
    "cluster": "2+ insiders buying",
    "smart_past": "Smart, judged on the past",
    "smart_hindsight": "Smart, judged with hindsight",
}
CLASS = {"all": "c-all", "cluster": "c-clu", "smart_past": "c-past", "smart_hindsight": "c-hind"}

EXTRA_CSS = """
.vault{background:#0d0b08;color:#ece6da;border:1px solid #2a2418;border-radius:14px;padding:14px;margin:16px 0;
--gold:#e8b44c;--gold2:#fff1c7;--ink:#f6f1e7;--mut:#9c927f;--grid:#231e15;--past:#f6f1e7;--clu:#8fb7e8;--all:#7c8b99;--bench:#6f6656}
.vault h2,.vault h3{color:#f6f1e7}.vault .muted,.vault figcaption{color:#9c927f}
.brand{display:grid;gap:2px;margin:14px 0 4px}
.mark{font-weight:900;letter-spacing:.38em;font-size:1.7rem;line-height:1.1;
background:linear-gradient(90deg,#b8862b,#fff1c7,#e8b44c,#b8862b);background-size:250% 100%;
-webkit-background-clip:text;background-clip:text;color:transparent;animation:assay-sheen 6s ease-in-out infinite alternate}
.verdict{font-size:1.15rem;font-weight:650;line-height:1.45;margin:6px 0 10px;max-width:70ch}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:12px 0}
.figs div{border:1px solid #2a2418;border-radius:10px;padding:12px;background:#120f0a;animation:assay-rise .6s ease-out both;animation-delay:var(--d)}
.figs div.hind{border-color:#5a4520;box-shadow:inset 0 0 24px rgba(232,180,76,.10)}
.figs strong{display:block;font-size:1.55rem;font-variant-numeric:tabular-nums}
.figs .hind strong{color:#e8b44c}
.figs span{font-size:13px;color:#9c927f}
.vault svg{max-width:760px}
.cv .g{stroke:#231e15;stroke-width:1}.cv text{fill:#9c927f;font-size:13px;font-family:ui-monospace,Menlo,Consolas,monospace}
.cv .l{fill:none;stroke-width:2.2;stroke-dasharray:1;stroke-dashoffset:1;animation:assay-draw 2.2s cubic-bezier(.3,.6,.2,1) forwards;animation-delay:var(--d)}
.cv .c-hind{stroke:#e8b44c;stroke-width:2.8;filter:drop-shadow(0 0 6px rgba(232,180,76,.55))}
.cv .c-past{stroke:#f6f1e7}.cv .c-clu{stroke:#8fb7e8}.cv .c-all{stroke:#7c8b99}
.cv .bench{stroke:#6f6656;stroke-dasharray:5 4;stroke-dashoffset:0;animation:none;opacity:.9}
.curve-key{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:#9c927f;margin-top:6px}
.curve-key i{display:inline-block;width:16px;height:3px;margin-right:6px;vertical-align:middle;background:currentColor}
.curve-key .c-hind{color:#e8b44c}.curve-key .c-past{color:#f6f1e7}.curve-key .c-clu{color:#8fb7e8}.curve-key .c-all{color:#7c8b99}.curve-key .bench{color:#6f6656}
.coins{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.hist .b{fill:#3b3324;transform-box:fill-box;transform-origin:bottom;animation:assay-grow .7s ease-out both;animation-delay:var(--d)}
.hist .obs{stroke:#e8b44c;stroke-width:2.5;animation:assay-drop .7s 1s cubic-bezier(.3,1.4,.5,1) both}
.hist .obsdot{fill:#e8b44c;animation:assay-drop .7s 1s cubic-bezier(.3,1.4,.5,1) both}
.hist text{fill:#9c927f;font-size:13px;font-family:ui-monospace,Menlo,Consolas,monospace}
.hist .lab{fill:#f6f1e7}
.funnel{display:grid;gap:6px;margin:8px 0}
.funnel .row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 10px;align-items:center;font-size:14px}
.funnel .bar{grid-column:1 / -1;height:10px;border-radius:5px;background:linear-gradient(90deg,#b8862b,#e8b44c);
width:var(--w);transform-origin:left;animation:assay-stretch .8s ease-out both;animation-delay:var(--d)}
.funnel .n{font-variant-numeric:tabular-nums;color:#f6f1e7}
.chip{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.04em;border-radius:4px;padding:1px 6px;border:1px solid var(--line)}
.chip.ok{border-color:var(--ok);color:var(--ok)}.chip.thin{border-color:var(--bad);color:var(--bad)}
.chip.cheat{border-color:#b8862b;color:#b8862b}
@keyframes assay-draw{to{stroke-dashoffset:0}}
@keyframes assay-rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes assay-grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes assay-stretch{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes assay-drop{from{opacity:0;transform:translateY(-30px)}to{opacity:1;transform:none}}
@keyframes assay-sheen{from{background-position:0% 0}to{background-position:100% 0}}
@media (prefers-reduced-motion: reduce){
.cv .l{animation:none;stroke-dashoffset:0}.hist .b,.hist .obs,.hist .obsdot,.figs div,.funnel .bar,.mark{animation:none}
}
"""


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _txt_signed(x, digits: int = 1) -> str:
    if not _ok(x):
        return "n/a"
    spec = {0: ".0f", 1: ".1f", 2: ".2f"}.get(digits, ".1f")
    return "".join(("+" if x >= 0 else "-", format(abs(x) * 100, spec), "%"))


def _txt_share(x) -> str:
    return "n/a" if not _ok(x) else "".join((format(x * 100, ".0f"), "%"))


def _txt_p(x) -> str:
    if not _ok(x):
        return "n/a"
    return "< 0.01" if x < 0.01 else format(x, ".2f")


def _txt_money(x) -> str:
    if not _ok(x):
        return "n/a"
    if x >= 1e6:
        return "".join(("$", format(x / 1e6, ".2f"), "M"))
    return "".join(("$", format(x / 1e3, ".0f"), "k"))


def _arm(rec: dict, name: str) -> dict:
    return rec["arms"][name]["by_horizon"][str(rec["params"]["primary"])]


def verdict_html(rec: dict) -> str:
    past, every = _arm(rec, "smart_past"), _arm(rec, "all")
    pp = rec["nulls"].get("random_insiders_vs_smart_past", {}).get("p_value")
    ps = rec["nulls"].get("random_stock_same_days", {}).get("p_value")
    if past["n"] < 30:
        honest = "Too few honest picks to judge."
    elif _ok(pp) and pp < 0.05:
        honest = "".join((
            "Judged only on what was knowable at the time, smart insiders beat random insiders (p ",
            _txt_p(pp), "). Worth checking on data the study has not seen.",
        ))
    else:
        honest = "".join((
            "Judged only on what was knowable at the time, smart insiders did no better than random insiders (p ",
            _txt_p(pp), "). The edge in the hindsight picks is the hindsight.",
        ))
    if every["n"] < 30:
        group = ""
    elif _ok(ps) and ps < 0.05:
        group = "".join((" Insider buys as a group beat random stocks bought on the same days (p ", _txt_p(ps), ")."))
    else:
        group = "".join((" Insider buys as a group did no better than random stocks bought on the same days (p ",
                         _txt_p(ps), ")."))
    return f'<p class="verdict">{esc(honest)}{esc(group)}</p>'


def figures_html(rec: dict) -> str:
    hind, past, every = _arm(rec, "smart_hindsight"), _arm(rec, "smart_past"), _arm(rec, "all")
    h = rec["params"]["primary"]
    pf_h, pf_p = rec["arms"]["smart_hindsight"]["portfolio"], rec["arms"]["smart_past"]["portfolio"]
    stock = rec["nulls"].get("random_stock_same_days", {})
    figs = [
        ("hind", _txt_signed(hind["mean"]), "".join((
            "per copied trade over ", str(h), " sessions, ", _txt_share(hind["hit_rate"]),
            " winners. Insiders picked with hindsight: the copy-trading pitch's method.",
        ))),
        ("", _txt_signed(past["mean"]), "".join((
            "per copied trade when insiders are judged only on their past (",
            str(past["n"]), " trades, ", _txt_share(past["hit_rate"]), " winners).",
        ))),
        ("", _txt_signed(every["mean"]), "".join((
            "per trade copying every insider buy; random stocks on the same days made ",
            _txt_signed(stock.get("median")), ".",
        ))),
        ("hind", _txt_money(pf_h["equity"][-1] if pf_h["equity"] else None), "".join((
            "a $100k book copying the hindsight picks. The honest picks: ",
            _txt_money(pf_p["equity"][-1] if pf_p["equity"] else None), "; SPY: ",
            _txt_money(pf_p["benchmark"][-1] if pf_p["benchmark"] else None), ".",
        ))),
    ]
    cells = []
    for i, (cls, big, small) in enumerate(figs):
        cells.append(
            f'<div class="{esc(cls)}" style="--d:{_n(i * 0.1)}s"><strong>{esc(big)}</strong><span>{esc(small)}</span></div>'
        )
    cells_html = "".join(cells)
    return f'<div class="figs">{cells_html}</div>'


def _log_ticks(lo: float, hi: float) -> list[float]:
    out = []
    k = math.floor(math.log10(lo))
    while 10 ** k <= hi * 1.0001:
        for m in (1, 2, 5):
            v = m * 10 ** k
            if lo <= v <= hi:
                out.append(v)
        k += 1
    return out


def curves_svg(series, days, label: str) -> str:
    """Equity curves on a log scale, so a 20x line does not flatten the rest."""
    w, h, left, right, top, bottom = 640, 280, 62, 12, 12, 26
    values = [v for _, vals, _ in series for v in vals if v > 0]
    lo, hi = min(values) * 0.95, max(values) * 1.05
    llo, lhi = math.log(lo), math.log(hi)
    n = max(len(vals) for _, vals, _ in series)

    def x(i):
        return left + (w - left - right) * (i / max(1, n - 1))

    def y(v):
        return top + (h - top - bottom) * (1 - (math.log(v) - llo) / (lhi - llo))

    parts = [f'<svg class="cv" viewBox="0 0 {_n(w)} {_n(h)}" role="img" aria-label="{esc(label)}">']
    for v in _log_ticks(lo, hi):
        parts.append(f'<line class="g" x1="{_n(left)}" x2="{_n(w - right)}" y1="{_n(y(v))}" y2="{_n(y(v))}"/>')
        parts.append(f'<text x="{_n(left - 6)}" y="{_n(y(v) + 4)}" text-anchor="end">{esc(_txt_money(v))}</text>')
    if days:
        parts.append(f'<text x="{_n(left)}" y="{_n(h - 6)}">{esc(days[0][:4])}</text>')
        parts.append(f'<text x="{_n(w - right)}" y="{_n(h - 6)}" text-anchor="end">{esc(days[-1][:4])}</text>')
    for k, (_, vals, cls) in enumerate(series):
        points_html = " ".join(",".join((_n(x(i)), _n(y(v)))) for i, v in enumerate(vals) if v > 0)
        parts.append(
            f'<polyline class="l {esc(cls)}" pathLength="1" style="--d:{_n(k * 0.35)}s" points="{points_html}"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _key_html(series) -> str:
    items_html = "".join(f'<span class="{esc(cls)}"><i></i>{esc(name)}</span>' for name, _, cls in series)
    return f'<div class="curve-key">{items_html}</div>'


def trap_html(rec: dict) -> str:
    pf_h, pf_p = rec["arms"]["smart_hindsight"]["portfolio"], rec["arms"]["smart_past"]["portfolio"]
    if not pf_h["equity"] or not pf_p["equity"]:
        return ""
    series = [
        ("SPY, bought and held", pf_p["benchmark"], "bench"),
        ("Smart, judged on the past", pf_p["equity"], "c-past"),
        ("Smart, judged with hindsight (cheating)", pf_h["equity"], "c-hind"),
    ]
    svg_html = curves_svg(series, pf_p["days"], "Hindsight-picked insiders against honestly picked insiders and SPY")
    key_html = _key_html(series)
    cap = "".join((
        "$100k copying each set of insiders, equal weight, 5 bps each way. Hindsight book ends at ",
        _txt_money(pf_h["equity"][-1]), " (", _txt_signed(pf_h["cagr"]), " a year); honest book ",
        _txt_money(pf_p["equity"][-1]), " (", _txt_signed(pf_p["cagr"]), " a year); SPY ",
        _txt_money(pf_p["benchmark"][-1]), ". Log scale.",
    ))
    return (
        '<section class="vault" id="trap"><h2>The hindsight trap</h2>'
        "<p class=\"muted\">The same insiders, the same trades. The only difference is when the "
        "\"smart\" label was decided: before each trade, or after the fact.</p>"
        f'<figure class="chart">{svg_html}{key_html}<figcaption>{esc(cap)}</figcaption></figure></section>'
    )


def hist_svg(null: dict, label: str) -> str:
    counts, edges = null.get("histogram") or [], null.get("edges") or []
    if not counts or len(edges) != len(counts) + 1:
        return ""
    w, h, left, right, top, bottom = 420, 170, 10, 10, 26, 26
    obs = null.get("observed")
    lo_x, hi_x = edges[0], edges[-1]
    if _ok(obs):
        lo_x, hi_x = min(lo_x, obs), max(hi_x, obs)
    span = (hi_x - lo_x) or 1e-9
    peak = max(counts) or 1

    def x(v):
        return left + (w - left - right) * ((v - lo_x) / span)

    def y(c):
        return top + (h - top - bottom) * (1 - c / peak)

    parts = [f'<svg class="hist" viewBox="0 0 {_n(w)} {_n(h)}" role="img" aria-label="{esc(label)}">']
    for i, c in enumerate(counts):
        x0, x1 = x(edges[i]), x(edges[i + 1])
        parts.append(
            f'<rect class="b" style="--d:{_n(i * 0.02)}s" x="{_n(x0 + 0.5)}" y="{_n(y(c))}" '
            f'width="{_n(max(0.5, x1 - x0 - 1))}" height="{_n(y(0) - y(c))}"/>'
        )
    parts.append(f'<text x="{_n(x(edges[0]))}" y="{_n(h - 6)}">{esc(_txt_signed(edges[0], 2))}</text>')
    parts.append(f'<text x="{_n(x(edges[-1]))}" y="{_n(h - 6)}" text-anchor="end">{esc(_txt_signed(edges[-1], 2))}</text>')
    if _ok(obs):
        ox = x(obs)
        anchor = "end" if ox > w * 0.7 else "start"
        dx = -6 if anchor == "end" else 6
        parts.append(f'<line class="obs" x1="{_n(ox)}" x2="{_n(ox)}" y1="{_n(top - 4)}" y2="{_n(y(0))}"/>')
        parts.append(f'<circle class="obsdot" cx="{_n(ox)}" cy="{_n(top - 4)}" r="4"/>')
        parts.append(
            f'<text class="lab" x="{_n(ox + dx)}" y="{_n(top - 10)}" text-anchor="{esc(anchor)}">'
            f'{esc(_txt_signed(obs, 2))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def coins_html(rec: dict) -> str:
    h = rec["params"]["primary"]
    panels = []
    for key, title, what in (
        ("random_insiders_vs_smart_past", "Smart (past) against random insiders",
         "the same number of purchases, drawn from all insider buys filed in the same months"),
        ("random_stock_same_days", "Every insider buy against random stocks",
         "the same entry and exit days, a random warehouse stock instead of the insider's"),
    ):
        null = rec["nulls"].get(key) or {}
        svg_html = hist_svg(null, title)
        if not svg_html:
            continue
        cap = "".join((
            "Gold line: the strategy's average excess return per trade over ", str(h), " sessions, ",
            _txt_signed(null.get("observed"), 2), ". Bars: ", str(null.get("draws")), " random draws of ", what,
            "; 95% of them fall between ", _txt_signed(null.get("lo"), 2), " and ", _txt_signed(null.get("hi"), 2),
            ". p ", _txt_p(null.get("p_value")), ".",
        ))
        panels.append(f'<figure class="chart"><h3>{esc(title)}</h3>{svg_html}<figcaption>{esc(cap)}</figcaption></figure>')
    if not panels:
        return ""
    panels_html = "".join(panels)
    return (
        '<section class="vault" id="coins"><h2>Against a fair coin</h2>'
        '<p class="muted">Beating SPY is not enough: the warehouse only holds companies that survived to '
        "2026, so almost anything bought in it beats SPY. A strategy has found something only if it "
        "beats random picks made under the same conditions.</p>"
        f'<div class="coins">{panels_html}</div></section>'
    )


def table_html(rec: dict) -> str:
    hs = rec["params"]["horizons"]
    head_html = "".join(f"<th>Excess {esc(h)}d</th>" for h in hs)
    rows = []
    for name in ARMS:
        arm = rec["arms"][name]
        s = _arm(rec, name)
        pf = arm["portfolio"]
        cells_html = "".join(
            f'<td data-label="Excess {esc(h)}d">{esc(_txt_signed(arm["by_horizon"][str(h)]["mean"], 2))}</td>' for h in hs
        )
        if name == "smart_hindsight":
            chip_html = '<span class="chip cheat">USES THE FUTURE</span>'
        elif s["evidence"] == "SUFFICIENT":
            chip_html = '<span class="chip ok">SUFFICIENT</span>'
        else:
            chip_html = '<span class="chip thin">INSUFFICIENT EVIDENCE</span>'
        rows.append(
            "<tr>"
            f'<td data-label="Strategy">{esc(SHORT[name])}</td>'
            f'<td data-label="Trades">{esc(s["n"])}</td>{cells_html}'
            f'<td data-label="Winners">{esc(_txt_share(s["hit_rate"]))}</td>'
            f'<td data-label="Book a year">{esc(_txt_signed(pf["cagr"]))}</td>'
            f'<td data-label="Worst drop">{esc(_txt_signed(-pf["max_drawdown"] if _ok(pf["max_drawdown"]) else None))}</td>'
            f'<td data-label="Evidence">{chip_html}</td>'
            "</tr>"
        )
    rows_html = "".join(rows)
    return (
        '<section id="strategies"><h2>Four ways to copy insiders</h2>'
        '<p>Excess is the copied trade\'s return minus SPY\'s over the same days, after 5 bps each way. '
        '<span class="prov prov-backtest">HISTORICAL BACKTEST</span></p>'
        '<div class="table-wrap"><table><thead><tr><th>Strategy</th><th>Trades</th>'
        f"{head_html}<th>Winners</th><th>Book a year</th><th>Worst drop</th><th>Evidence</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table></div></section>"
    )


def funnel_html(rec: dict) -> str:
    steps = [s for s in rec.get("funnel", []) if _ok(s.get("count"))]
    if not steps:
        return ""
    top = max(s["count"] for s in steps) or 1
    rows = []
    for i, s in enumerate(steps):
        width = max(0.6, 100 * math.sqrt(s["count"] / top))
        rows.append(
            f'<div class="row"><span>{esc(s["step"])}</span><span class="n">{esc(format(s["count"], ","))}</span>'
            f'<div class="bar" style="--w:{_n(width)}%;--d:{_n(i * 0.12)}s"></div></div>'
        )
    rows_html = "".join(rows)
    dropped = rec.get("excluded") or {}
    drop_html = "".join(f"<li>{esc(k)}: {esc(format(v, ','))}</li>" for k, v in dropped.items())
    list_html = f'<ul class="chart-list">{drop_html}</ul>' if drop_html else ""
    return (
        '<section class="vault" id="funnel"><h2>From filings to trades</h2>'
        '<p class="muted">Bar length is the square root of the count, so the last steps stay visible.</p>'
        f'<div class="funnel">{rows_html}</div>{list_html}</section>'
    )


def books_html(rec: dict) -> str:
    base = rec["arms"]["all"]["portfolio"]
    if not base["equity"]:
        return ""
    series = [("SPY, bought and held", base["benchmark"], "bench")]
    for name in ARMS:
        pf = rec["arms"][name]["portfolio"]
        if pf["equity"]:
            series.append((SHORT[name], pf["equity"], CLASS[name]))
    svg_html = curves_svg(series, base["days"], "All four insider books against SPY")
    key_html = _key_html(series)
    cap = "; ".join(
        "".join((SHORT[name], " ", _txt_money(rec["arms"][name]["portfolio"]["equity"][-1]), " (Sharpe ",
                 format(rec["arms"][name]["portfolio"]["sharpe"], ".2f") if _ok(rec["arms"][name]["portfolio"]["sharpe"]) else "n/a",
                 ")"))
        for name in ARMS if rec["arms"][name]["portfolio"]["equity"]
    )
    return (
        '<section class="vault" id="books"><h2>Every book, side by side</h2>'
        f'<figure class="chart">{svg_html}{key_html}<figcaption>{esc(cap)}. SIMULATED.</figcaption></figure></section>'
    )


def render_assay_page(rec: dict | None, now: datetime | None = None, note: str | None = None) -> str:
    now = now or datetime.now()
    note_html = f'<p class="empty-state">{esc(note)}</p>' if note else ""
    if not rec:
        body_html = (
            '<section id="assay-empty"><h2>No Assay yet</h2>'
            f'<p class="empty-state">{esc(DATA_UNAVAILABLE)}: no insider trial has been run. Run '
            "<code>python -m signalforge.insider_cli download</code>, then <code>build</code>, then "
            "<code>trial --window validation --source tiingo</code>.</p></section>"
        )
    else:
        p = rec["params"]
        intro_html = (
            '<section class="vault" id="verdict"><h2>The verdict</h2>'
            f"{verdict_html(rec)}{figures_html(rec)}"
            f'<p class="muted">Window {esc(p["window_start"])} to {esc(p["window_end"])}. A copied trade enters at the '
            "close of the first trading day after the filing and is held for a fixed number of sessions.</p></section>"
        )
        body_html = "".join((intro_html, trap_html(rec), coins_html(rec), table_html(rec), funnel_html(rec), books_html(rec)))
    css_html = "".join((CSS, EXTRA_CSS))
    tabs_html = tab_strip("assay.html")
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>SIGNALFORGE Assay</title>"
        f"<style>{css_html}</style></head><body>"
        "<header><h1>SIGNALFORGE operator dashboard</h1>"
        '<p class="sim-banner" role="note"><strong>SIMULATION ONLY.</strong> '
        "Nothing on this page is live trading. Copying an insider here is a historical measurement, not an order.</p>"
        f"{tabs_html}"
        '<div class="brand"><span class="mark">ASSAY</span>'
        '<span class="muted">Smart money or fool\'s gold? Copy-trading tested only on what was knowable at the time.</span></div>'
        f'<p class="muted">Rendered {esc(now.strftime("%Y-%m-%d %H:%M"))} local time from the last insider trial.</p>'
        f"{note_html}</header>"
        f"<main>{body_html}</main>"
        '<footer><p class="muted">Static page. It loads nothing from the network and runs no script. Insider '
        "filings: SEC Insider Transactions Data Sets. The warehouse holds only companies that survived to 2026; "
        "the random-stock comparison measures how much that alone is worth.</p></footer>"
        "</body></html>"
    )


def write_assay_page(path, rec: dict | None, now: datetime | None = None, note: str | None = None) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_assay_page(rec, now, note), encoding="utf-8")
    return out
