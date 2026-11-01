"""The Kronos Trial tab: forecast cones, what happened next, and the scores.

Same rules as the operator page: static HTML, no script, nothing loaded from
the network, every chart a <figure> whose caption states its numbers, and
DATA UNAVAILABLE with a reason wherever there is nothing to show.

The motion is CSS only. Each sampled path is drawn with `pathLength="1"` and
a dash offset animated from 1 to 0, staggered per path, so the cone fans out
from today's close; the median follows, then the band. Forecaster and replay
switching uses radio inputs and sibling selectors, and showing a panel
restarts its animation. `prefers-reduced-motion` turns all of it off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

import numpy as np

from signalforge.dashboard.charts import DATA_UNAVAILABLE, _n, esc
from signalforge.dashboard.render import CSS, tab_strip
from signalforge.forecasting.cone import read_cone
from signalforge.forecasting.paths import ContextWindow, PathForecast
from signalforge.forecasting.scoring import ForecastOutcome

FAN_W, FAN_H = 640, 320
PAD_L, PAD_R, PAD_T, PAD_B = 6, 70, 14, 26
LABELS = {"kronos": "Kronos", "block_bootstrap": "Block bootstrap", "random_walk": "Random walk"}


@dataclass(frozen=True)
class LivePanel:
    forecast: PathForecast
    window: ContextWindow


@dataclass(frozen=True)
class ReplayPanel:
    forecast: PathForecast
    before: Sequence[float]
    realized: Sequence[float]
    outcome: ForecastOutcome


@dataclass(frozen=True)
class ForecastPageData:
    record: dict | None = None
    live: Sequence[LivePanel] = ()
    replays: Sequence[ReplayPanel] = ()
    backtest: dict = field(default_factory=dict)
    """label -> {"equity": [...], "final": float, "sharpe": float | None}"""
    note: str | None = None


EXTRA_CSS = """
:root{--up:#c4470f;--down:#2f6497}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--up:#f2803f;--down:#6ea3db}}
:root[data-theme="dark"]{--up:#f2803f;--down:#6ea3db}
.scope{background:#0b0e13;color:#e6eaf0;border:1px solid #1d232c;border-radius:14px;padding:14px;margin:16px 0;
--sc-grid:#1a2029;--sc-muted:#8c96a5;--sc-up:#ff8a3d;--sc-down:#5aa2ef;--sc-ink:#f3f5f8;--sc-ok:#3fd18f;--sc-bad:#ff5d57}
.scope h2,.scope h3{color:#f3f5f8}
.scope .muted{color:#8c96a5}
.scope figcaption{color:#8c96a5}
.switch{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.switch label{border:1px solid #2a323d;border-radius:999px;padding:0 12px;min-height:36px;display:inline-flex;align-items:center;
cursor:pointer;color:#c9d0da;font-size:14px}
.radio{position:absolute;opacity:0;pointer-events:none}
.pane{display:none}
.scope-grid{display:grid;grid-template-columns:minmax(0,1fr) 230px;gap:14px;align-items:start}
@media (max-width:760px){.scope-grid{grid-template-columns:minmax(0,1fr)}}
.fan .grid{stroke:var(--sc-grid);stroke-width:1}
.fan .tick{fill:var(--sc-muted);font-size:13px;font-family:ui-monospace,Menlo,Consolas,monospace}
.fan .future{fill:#121820}
.fan .hist{stroke:#cfd6df;stroke-width:1.6;fill:none}
.fan .wick{stroke:#8c96a5;stroke-width:1}
.fan .body-up{fill:#0b0e13;stroke:#cfd6df;stroke-width:1}
.fan .body-dn{fill:#cfd6df;stroke:#cfd6df;stroke-width:1}
.fan .p{fill:none;stroke-width:1.1;stroke-opacity:.38;stroke-dasharray:1;stroke-dashoffset:1;
animation:draw 1.5s cubic-bezier(.2,.7,.2,1) forwards;animation-delay:var(--d)}
.fan .p.up{stroke:var(--sc-up)}
.fan .p.dn{stroke:var(--sc-down)}
.fan .band{fill:none;stroke:#aab3c0;stroke-width:1.3;stroke-dasharray:5 4;opacity:0;animation:fade .7s 1.9s forwards}
.fan .med{fill:none;stroke:var(--sc-ink);stroke-width:2.6;stroke-dasharray:1;stroke-dashoffset:1;
animation:draw 1s ease-out 1.5s forwards;filter:drop-shadow(0 0 4px rgba(255,255,255,.35))}
.fan .real{fill:none;stroke-width:3;stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.1s ease-in-out 2.3s forwards}
.fan .real.hit{stroke:var(--sc-ok);filter:drop-shadow(0 0 6px rgba(63,209,143,.6))}
.fan .real.miss{stroke:var(--sc-bad);filter:drop-shadow(0 0 6px rgba(255,93,87,.6))}
.fan .now{stroke:#f3f5f8;stroke-width:1.2;animation:pulse 2.6s ease-in-out infinite}
.fan .nowdot{fill:#f3f5f8;animation:glow 2.6s ease-in-out infinite}
.fan .tag{fill:#0b0e13;stroke:#3a4350}
.fan .tag.mid{stroke:#f3f5f8}
.fan .tagtext{fill:#cfd6df;font-size:13px;font-family:ui-monospace,Menlo,Consolas,monospace}
.read{display:grid;gap:10px}
.regime{font-size:1.35rem;font-weight:800;letter-spacing:.06em;display:flex;align-items:center;gap:8px}
.regime i{width:11px;height:11px;border-radius:50%;background:var(--sc-down);box-shadow:0 0 12px var(--sc-down)}
.regime.trending i{background:var(--sc-up);box-shadow:0 0 12px var(--sc-up)}
.cells{display:grid;grid-template-columns:repeat(20,1fr);gap:2px;max-width:220px}
.cells b{aspect-ratio:1;background:var(--sc-down);transform:scale(0);animation:pop .25s ease-out forwards;animation-delay:var(--d)}
.cells b.up{background:var(--sc-up)}
.read dl{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:0;font-size:14px}
.read dt{color:#8c96a5}
.read dd{margin:0;text-align:right;font-family:ui-monospace,Menlo,Consolas,monospace}
.read .ok{color:var(--sc-ok)}
.read .bad{color:var(--sc-bad)}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px;margin:12px 0}
.figs div{border:1px solid var(--line);border-radius:10px;padding:12px;animation:rise .6s ease-out both;animation-delay:var(--d)}
.figs strong{display:block;font-size:1.6rem;font-variant-numeric:tabular-nums}
.figs span{font-size:14px;color:var(--muted)}
.bars,.eq{max-width:640px}
.bars .b{fill:var(--up);transform-box:fill-box;transform-origin:bottom;animation:grow .8s cubic-bezier(.2,.7,.2,1) both;animation-delay:var(--d)}
.bars .ref{stroke:var(--fg);stroke-width:1.5;stroke-dasharray:6 4}
.bars .coin{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3}
.bars .wh{stroke:var(--fg);stroke-width:1.4}
.bars text{fill:var(--muted);font-size:14px}
.bars .val{fill:var(--fg)}
.eq .l{fill:none;stroke-width:2.2;stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.8s ease-out forwards}
.eq .bh{stroke:var(--fg)}.eq .c0{stroke:var(--up)}.eq .c1{stroke:var(--down)}.eq .c2{stroke:var(--accent)}
.eq text{fill:var(--muted);font-size:14px}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes fade{to{opacity:1}}
@keyframes pop{to{transform:scale(1)}}
@keyframes grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes pulse{0%,100%{stroke-opacity:.45}50%{stroke-opacity:1}}
@keyframes glow{0%,100%{filter:drop-shadow(0 0 2px #fff)}50%{filter:drop-shadow(0 0 9px #fff)}}
@media (prefers-reduced-motion: reduce){
.fan .p,.fan .med,.fan .real,.eq .l{animation:none;stroke-dashoffset:0}
.fan .band{animation:none;opacity:1}.cells b{animation:none;transform:none}
.bars .b,.figs div,.fan .now,.fan .nowdot{animation:none}
}
"""


def _txt_pct(x, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return "".join((format(x * 100, ".0f" if digits == 0 else ".1f"), "%"))


def _txt_signed(x) -> str:
    return "".join(("+" if x >= 0 else "-", format(abs(x) * 100, ".1f"), "%"))


def _txt_num(v: float, decimals: int) -> str:
    return format(v, ",.0f" if decimals == 0 else (",.1f" if decimals == 1 else ",.2f"))


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    raw = (hi - lo) / n
    mag = 10 ** np.floor(np.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = np.ceil(lo / step) * step
    return [float(v) for v in np.arange(start, hi + step * 1e-9, step)]


def _path_d(x0: float, y0: float, points) -> str:
    """SVG path data from numbers only, so nothing here needs escaping."""
    head = "".join(("M", _n(x0), ",", _n(y0)))
    return "".join([head] + ["".join(("L", _n(x), ",", _n(y))) for x, y in points])


def fan_svg(
    forecast: PathForecast,
    *,
    candles: np.ndarray | None = None,
    closes: Sequence[float] | None = None,
    realized: Sequence[float] | None = None,
    hit: bool | None = None,
    title: str,
) -> str:
    """One cone. `candles` is [n, 4] OHLC; otherwise `closes` draws a line."""
    paths = forecast.paths
    steps = forecast.horizon
    last = forecast.last_close
    if candles is not None:
        hist_lo, hist_hi, n_hist = float(candles[:, 2].min()), float(candles[:, 1].max()), len(candles)
    else:
        arr = np.asarray(closes, dtype=float)
        hist_lo, hist_hi, n_hist = float(arr.min()), float(arr.max()), len(arr)
    lo = min(float(np.quantile(paths, 0.02)), hist_lo)
    hi = max(float(np.quantile(paths, 0.98)), hist_hi)
    if realized is not None:
        lo, hi = min(lo, min(realized)), max(hi, max(realized))
    pad = (hi - lo) * 0.05
    lo, hi = lo - pad, hi + pad
    x_now = PAD_L + (FAN_W - PAD_L - PAD_R) * 0.55
    x_end = FAN_W - PAD_R
    decimals = 0 if hi - lo > 20 else 1

    def xh(i):
        return PAD_L + (x_now - PAD_L) * (i / max(1, n_hist - 1))

    def xf(s):
        return x_now + (x_end - x_now) * (s / steps)

    def y(v):
        return PAD_T + (FAN_H - PAD_T - PAD_B) * (1 - (v - lo) / (hi - lo))

    def future(series):
        return _path_d(x_now, y(last), [(xf(s + 1), y(v)) for s, v in enumerate(series)])

    q10, q50, q90 = (np.quantile(paths, q, axis=0) for q in (0.1, 0.5, 0.9))
    tag_ys = []
    for v in sorted((float(q90[-1]), float(q50[-1]), float(q10[-1])), reverse=True):
        ty = y(v)
        if tag_ys and ty - tag_ys[-1] < 18:
            ty = tag_ys[-1] + 18
        tag_ys.append(ty)

    out = [
        f'<svg class="fan" viewBox="0 0 {_n(FAN_W)} {_n(FAN_H)}" role="img">',
        f"<title>{esc(title)}</title>",
        f'<rect class="future" x="{_n(x_now)}" y="{_n(PAD_T)}" width="{_n(x_end - x_now)}" height="{_n(FAN_H - PAD_T - PAD_B)}"/>',
    ]
    for v in _ticks(lo, hi):
        out.append(f'<line class="grid" x1="{_n(PAD_L)}" x2="{_n(x_end)}" y1="{_n(y(v))}" y2="{_n(y(v))}"/>')
        if all(abs(y(v) - ty) > 14 for ty in tag_ys):
            out.append(f'<text class="tick" x="{_n(x_end + 6)}" y="{_n(y(v) + 4)}">{esc(_txt_num(v, decimals))}</text>')
    if candles is not None:
        w = max(1.0, (x_now - PAD_L) / n_hist * 0.6)
        for i, (o, h, low, c) in enumerate(candles):
            x = xh(i)
            out.append(f'<line class="wick" x1="{_n(x)}" x2="{_n(x)}" y1="{_n(y(h))}" y2="{_n(y(low))}"/>')
            top, bot = y(max(o, c)), y(min(o, c))
            body_html = "body-up" if c >= o else "body-dn"
            out.append(
                f'<rect class="{body_html}" x="{_n(x - w / 2)}" y="{_n(top)}" '
                f'width="{_n(w)}" height="{_n(max(1.0, bot - top))}"/>'
            )
    else:
        points_html = " ".join(",".join((_n(xh(i)), _n(y(v)))) for i, v in enumerate(closes))
        out.append(f'<polyline class="hist" points="{points_html}"/>')
    order = np.argsort(paths[:, -1])
    for rank, k in enumerate(order):
        row = paths[k]
        side_html = "up" if row[-1] > last else "dn"
        # Stagger from the middle of the cone outwards, so it blooms.
        delay = abs(rank - len(order) / 2) / len(order) * 0.9
        d_html = future(row)
        out.append(
            f'<path class="p {side_html}" pathLength="1" style="--d:{esc(format(delay, ".3f"))}s" d="{d_html}"/>'
        )
    for series in (q10, q90):
        band_html = future(series)
        out.append(f'<path class="band" d="{band_html}"/>')
    med_html = future(q50)
    out.append(f'<path class="med" pathLength="1" d="{med_html}"/>')
    if realized is not None:
        real_class_html = "real hit" if hit else "real miss"
        real_html = future(realized)
        out.append(f'<path class="{real_class_html}" pathLength="1" d="{real_html}"/>')
    out.append(f'<line class="now" x1="{_n(x_now)}" x2="{_n(x_now)}" y1="{_n(PAD_T)}" y2="{_n(FAN_H - PAD_B)}"/>')
    out.append(f'<circle class="nowdot" cx="{_n(x_now)}" cy="{_n(y(last))}" r="3.5"/>')
    out.append(f'<text class="tick" x="{_n(x_now)}" y="{_n(FAN_H - 8)}" text-anchor="middle">NOW</text>')
    out.append(
        f'<text class="tick" x="{_n(x_end)}" y="{_n(FAN_H - 8)}" text-anchor="end">'
        f'{esc("".join(("+", str(steps))))}</text>'
    )
    tag_values = sorted(
        [(float(q90[-1]), "tag"), (float(q50[-1]), "tag mid"), (float(q10[-1]), "tag")], key=lambda t: -t[0]
    )
    for (v, tag_html), ty in zip(tag_values, tag_ys):
        out.append(f'<rect class="{tag_html}" x="{_n(x_end + 2)}" y="{_n(ty - 9)}" width="{_n(PAD_R - 4)}" height="18" rx="3"/>')
        out.append(f'<text class="tagtext" x="{_n(x_end + 7)}" y="{_n(ty + 4.5)}">{esc(_txt_num(v, decimals))}</text>')
    out.append("</svg>")
    return "".join(out)


def _cells_html(up: int, n: int) -> str:
    cells = []
    for i in range(n):
        up_html = "up" if i < up else ""
        cells.append(f'<b class="{up_html}" style="--d:{esc(format(0.9 + i * 0.008, ".3f"))}s"></b>')
    return "".join(['<div class="cells" aria-hidden="true">'] + cells + ["</div>"])


def _reading_html(forecast: PathForecast, rows: list[tuple[str, str, str]]) -> str:
    r = read_cone(forecast)
    regime_class_html = "regime trending" if r.regime.value == "TRENDING" else "regime"
    dl_html = "".join(f'<dt>{esc(k)}</dt><dd class="{esc(c)}">{esc(v)}</dd>' for k, v, c in rows)
    cells_html = _cells_html(r.paths_up, r.n_paths)
    return (
        f'<div class="read"><div class="{regime_class_html}"><i></i>{esc(r.regime.value)}</div>'
        f'<div class="muted">{esc(r.paths_up)}/{esc(r.n_paths)} paths up &middot; {esc(r.direction.value)}</div>'
        f"{cells_html}<dl>{dl_html}</dl></div>"
    )


def _switch_html(group: str, labels: list[str], panes_html: list[str]) -> str:
    """CSS-only tabs: radios first, then labels, then panes, all siblings."""
    g_html = esc(group)
    radios_html = "".join(
        f'<input class="radio" type="radio" name="{g_html}" id="{g_html}-{esc(i)}"{checked_html}>'
        for i, checked_html in ((i, " checked" if i == 0 else "") for i in range(len(labels)))
    )
    label_html = "".join(f'<label for="{g_html}-{esc(i)}">{esc(text)}</label>' for i, text in enumerate(labels))
    body_html = "".join(f'<div class="pane" data-g="{g_html}-{esc(i)}">{pane_html}</div>' for i, pane_html in enumerate(panes_html))
    css_html = "".join(
        f"#{g_html}-{esc(i)}:checked~.switch label[for='{g_html}-{esc(i)}']{{background:#f3f5f8;color:#0b0e13;border-color:#f3f5f8}}"
        f"#{g_html}-{esc(i)}:checked~.pane[data-g='{g_html}-{esc(i)}']{{display:block}}"
        f"#{g_html}-{esc(i)}:focus-visible~.switch label[for='{g_html}-{esc(i)}']{{outline:2px solid var(--sc-up);outline-offset:2px}}"
        for i in range(len(labels))
    )
    return f'<style>{css_html}</style>{radios_html}<div class="switch">{label_html}</div>{body_html}'


def _label(fid: str) -> str:
    return LABELS.get(fid, fid)


def live_section(live: Sequence[LivePanel]) -> str:
    if not live:
        return (
            '<section class="scope" id="live-cone"><h2>Live cone</h2>'
            f'<p class="empty-state">{esc(DATA_UNAVAILABLE)}: no forward forecast was produced with this trial.</p></section>'
        )
    panes, labels = [], []
    for panel in live:
        fc, win = panel.forecast, panel.window
        r = read_cone(fc)
        svg_html = fan_svg(
            fc, candles=win.ohlcv[-90:, :4],
            title=" ".join((fc.symbol, str(fc.n_paths), "sampled paths from", fc.as_of.isoformat())),
        )
        rows = [
            ("Last close", _txt_num(r.last_close, 2), ""),
            (" ".join(("Median", "".join(("+", str(fc.horizon))))),
             " ".join((_txt_num(r.median, 2), "".join(("(", _txt_signed(r.median_return), ")")))), ""),
            ("10-90 band", " ".join((_txt_num(r.p10, 0), "to", _txt_num(r.p90, 0))), ""),
            ("Spread", _txt_pct(r.spread_pct), ""),
        ]
        cap_html = (
            f"{esc(fc.symbol)}, {esc(fc.n_paths)} paths from the {esc(fc.as_of.isoformat())} close, next "
            f"{esc(fc.horizon)} sessions, {esc(_label(fc.forecaster_id))}. Median {esc(_txt_num(r.median, 2))}, "
            f"10th percentile {esc(_txt_num(r.p10, 2))}, 90th {esc(_txt_num(r.p90, 2))}; {esc(r.paths_up)} of "
            f"{esc(r.n_paths)} paths end above today. A forward forecast: nothing here is scored, and a cone is not a trade."
        )
        reading_html = _reading_html(fc, rows)
        panes.append(
            f'<div class="scope-grid"><figure class="chart" style="margin:0">{svg_html}'
            f"<figcaption>{cap_html}</figcaption></figure>{reading_html}</div>"
        )
        labels.append(_label(fc.forecaster_id))
    first = live[0].forecast
    switch_html = _switch_html("fc", labels, panes)
    return (
        f'<section class="scope" id="live-cone"><h2>{esc(first.symbol)}, the next {esc(first.horizon)} sessions</h2>'
        f'<p class="muted">From the {esc(first.as_of.isoformat())} close. Switch forecaster to see how much of the cone any of them earns.</p>'
        f"{switch_html}</section>"
    )


def replay_section(replays: Sequence[ReplayPanel]) -> str:
    if not replays:
        return ""
    panes, labels = [], []
    for rp in replays:
        fc, o = rp.forecast, rp.outcome
        r = o.reading
        svg_html = fan_svg(
            fc, closes=rp.before, realized=rp.realized, hit=o.hit,
            title=" ".join((fc.symbol, "cone from", fc.as_of.isoformat(), "and what happened")),
        )
        called = (
            " ".join(("TRENDING", r.direction.value)) if r.regime.value == "TRENDING"
            else " ".join(("CHOPPY, leaning", r.direction.value))
        )
        rows = [
            ("Called", called, ""),
            ("Median call", _txt_signed(r.median_return), ""),
            ("Happened", _txt_signed(o.realized_return), ""),
            ("Direction", "Right" if o.hit else "Wrong", "ok" if o.hit else "bad"),
            ("In 10-90 band", "Yes" if o.covered else "No", "ok" if o.covered else "bad"),
            ("Percentile", "".join((str(round(o.pit * 100)), "th")), ""),
        ]
        cap_html = (
            f"{esc(fc.symbol)} from {esc(fc.as_of.isoformat())}: the cone called {esc(called)} with a median of "
            f"{esc(_txt_signed(r.median_return))}; the next {esc(fc.horizon)} sessions returned "
            f"{esc(_txt_signed(o.realized_return))}, {esc('inside' if o.covered else 'outside')} the 10-90 band."
        )
        reading_html = _reading_html(fc, rows)
        panes.append(
            f'<div class="scope-grid"><figure class="chart" style="margin:0">{svg_html}'
            f"<figcaption>{cap_html}</figcaption></figure>{reading_html}</div>"
        )
        labels.append(" ".join(("✓" if o.hit else "✗", fc.as_of.isoformat())))
    switch_html = _switch_html("rp", labels, panes)
    return (
        '<section class="scope" id="replay"><h2>Replay: the cone, then what actually happened</h2>'
        f'<p class="muted">{esc(_label(replays[0].forecast.forecaster_id))} on the trial window. Green means the '
        "majority of paths called the direction; red means they did not.</p>"
        f"{switch_html}</section>"
    )


def _summaries(record: dict) -> dict[str, dict]:
    return {f["forecaster"]: f["summary"] for f in record.get("forecasters", [])}


def figures_section(record: dict | None, backtest: dict) -> str:
    if not record:
        return (
            '<section id="the-bar"><h2>The bar</h2>'
            f'<p class="empty-state">{esc(DATA_UNAVAILABLE)}: no forecast trial has been run. Run '
            "<code>python -m signalforge.forecast_cli trial --symbols SPY --window validation</code>.</p></section>"
        )
    s = _summaries(record)
    primary = "kronos" if "kronos" in s else ("block_bootstrap" if "block_bootstrap" in s else next(iter(s)))
    p = s[primary]
    trending = {b["label"]: b for b in p["by_regime"]}.get("TRENDING", {})
    figs = [
        (_txt_pct(p["base_rate_up"]), "of windows ended up. Calling UP every time scores this."),
        (_txt_pct(p["hit_rate"]), " ".join((_label(primary), "directional hit rate."))),
        (_txt_pct(trending.get("hit_rate")),
         "".join(("hit rate on its TRENDING calls (n=", str(trending.get("n", 0)), ")."))),
        (_txt_pct(p["coverage"]), "of outcomes inside the 10-90 band. Target 80%."),
    ]
    if "buy_and_hold" in backtest:
        cone = next((k for k in backtest if k != "buy_and_hold"), None)
        if cone:
            figs.append((
                "".join(("$", _txt_num(backtest[cone]["final"] / 1000, 1), "k")),
                "".join(("TRENDING-only rule vs $", _txt_num(backtest["buy_and_hold"]["final"] / 1000, 1),
                         "k buy-and-hold, SIMULATED.")),
            ))
    figs_html = "".join(
        f'<div style="--d:{esc(format(i * 0.08, ".2f"))}s"><strong>{esc(n)}</strong><span>{esc(t)}</span></div>'
        for i, (n, t) in enumerate(figs)
    )
    verdict = (
        "Kronos is in this trial." if "kronos" in s
        else "Kronos has not been run yet. These are the forecasters with no edge that set the bar."
    )
    window = record.get("forecasters", [{}])[0].get("window", ["?", "?"])
    return (
        f'<section id="the-bar"><h2>The bar</h2><p>{esc(verdict)} '
        f'<span class="prov prov-backtest">HISTORICAL BACKTEST</span> {esc(window[0])} to {esc(window[1])}.</p>'
        f'<div class="figs">{figs_html}</div></section>'
    )


def buckets_svg(summary: dict, label: str) -> str:
    buckets = list(summary["by_agreement"])
    w, h, left, right, top, bottom = 560, 240, 46, 10, 14, 44
    y0, y1 = 0.3, 0.85

    def y(v):
        return top + (h - top - bottom) * (1 - (v - y0) / (y1 - y0))

    bw = (w - left - right) / len(buckets)
    parts = [f'<svg class="bars" viewBox="0 0 {_n(w)} {_n(h)}" role="img" aria-label="{esc(" ".join(("Hit rate by path agreement,", label)))}">']
    for v in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        parts.append(f'<line class="axis" x1="{_n(left)}" x2="{_n(w - right)}" y1="{_n(y(v))}" y2="{_n(y(v))}" stroke-opacity=".3"/>')
        parts.append(f'<text x="{_n(left - 6)}" y="{_n(y(v) + 5)}" text-anchor="end">{esc(_txt_pct(v, 0))}</text>')
    for i, b in enumerate(buckets):
        x, bwid = left + bw * i + bw * 0.2, bw * 0.6
        cx = x + bwid / 2
        if b["n"] >= 30 and b.get("hit_rate") is not None:
            hr = b["hit_rate"]
            parts.append(
                f'<rect class="b" style="--d:{esc(format(i * 0.12, ".2f"))}s" x="{_n(x)}" y="{_n(y(hr))}" '
                f'width="{_n(bwid)}" height="{_n(y(y0) - y(hr))}"/>'
            )
            lo_i, hi_i = b["interval"]
            parts.append(f'<line class="wh" x1="{_n(cx)}" x2="{_n(cx)}" y1="{_n(y(max(lo_i, y0)))}" y2="{_n(y(min(hi_i, y1)))}"/>')
            parts.append(f'<text class="val" x="{_n(cx)}" y="{_n(y(min(hi_i, y1)) - 6)}" text-anchor="middle">{esc(_txt_pct(hr, 0))}</text>')
        elif b["n"]:
            parts.append(f'<text x="{_n(cx)}" y="{_n(y(0.4))}" text-anchor="middle">too few</text>')
        parts.append(f'<text x="{_n(cx)}" y="{_n(h - bottom + 18)}" text-anchor="middle">{esc(b["label"].replace(" agree", ""))}</text>')
        parts.append(f'<text x="{_n(cx)}" y="{_n(h - bottom + 36)}" text-anchor="middle">n={esc(b["n"])}</text>')
    base = summary["base_rate_up"]
    parts.append(f'<line class="ref" x1="{_n(left)}" x2="{_n(w - right)}" y1="{_n(y(base))}" y2="{_n(y(base))}"/>')
    parts.append(f'<line class="coin" x1="{_n(left)}" x2="{_n(w - right)}" y1="{_n(y(0.5))}" y2="{_n(y(0.5))}"/>')
    parts.append("</svg>")
    return "".join(parts)


def evidence_section(record: dict | None) -> str:
    if not record:
        return ""
    rows, charts = [], []
    for fid, sm in _summaries(record).items():
        trending = {b["label"]: b for b in sm["by_regime"]}.get("TRENDING", {})
        rows.append(
            "<tr>"
            f'<td data-label="Forecaster">{esc(_label(fid))}</td>'
            f'<td data-label="Forecasts">{esc(sm["n"])}</td>'
            f'<td data-label="10-90 coverage">{esc(_txt_pct(sm["coverage"]))}</td>'
            f'<td data-label="Mean CRPS">{esc(format(sm["mean_crps"], ".4f"))}</td>'
            f'<td data-label="Hit rate">{esc(_txt_pct(sm["hit_rate"]))}</td>'
            f'<td data-label="Always UP">{esc(_txt_pct(sm["base_rate_up"]))}</td>'
            f'<td data-label="Hit when TRENDING">{esc(_txt_pct(trending.get("hit_rate")))} (n={esc(trending.get("n", 0))})</td>'
            "</tr>"
        )
        if sum(1 for b in sm["by_agreement"] if b["n"] >= 30) > 1:
            parts = [
                "".join((b["label"], ": ", _txt_pct(b.get("hit_rate")), " of ", str(b["n"])))
                for b in sm["by_agreement"]
            ]
            cap = "".join(("; ".join(parts), ". Dashed line: always UP at ",
                           _txt_pct(sm["base_rate_up"]), "; dotted: a coin at 50%."))
            chart_html = buckets_svg(sm, _label(fid))
            charts.append(
                f'<figure class="chart"><h3>{esc(_label(fid))}: hit rate by path agreement</h3>'
                f"{chart_html}<figcaption>{esc(cap)}</figcaption></figure>"
            )
    skill_items = [
        (fid, v) for key, d in record.items() if key.startswith("crps_skill") for fid, v in d.items()
    ]
    skill_html = "".join(
        f"<li>{esc(_label(fid))}: {esc(_txt_signed(v))} CRPS skill over the random walk</li>"
        for fid, v in skill_items
    )
    rows_html, charts_html = "".join(rows), "".join(charts)
    list_html = f'<ul class="chart-list">{skill_html}</ul>' if skill_html else ""
    return (
        '<section id="evidence"><h2>Evidence</h2>'
        "<p>A confidence display means something only if confident calls beat a constant. The dashed line is "
        "what calling UP on every window scores.</p>"
        '<div class="table-wrap"><table><thead><tr><th>Forecaster</th><th>Forecasts</th><th>10-90 coverage</th>'
        "<th>Mean CRPS</th><th>Hit rate</th><th>Always UP</th><th>Hit when TRENDING</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table></div>{list_html}{charts_html}</section>"
    )


def backtest_section(backtest: dict) -> str:
    if not backtest:
        return ""
    w, h, left, right, top, bottom = 560, 220, 64, 10, 12, 26
    series = list(backtest.items())
    vals = [v for _, b in series for v in b["equity"]]
    lo, hi = min(vals) * 0.97, max(vals) * 1.02
    n = max(len(b["equity"]) for _, b in series)

    def x(i):
        return left + (w - left - right) * (i / max(1, n - 1))

    def y(v):
        return top + (h - top - bottom) * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg class="eq" viewBox="0 0 {_n(w)} {_n(h)}" role="img" aria-label="Simulated equity, cone rule against buy-and-hold">']
    for v in _ticks(lo, hi, 4):
        parts.append(f'<line class="axis" x1="{_n(left)}" x2="{_n(w - right)}" y1="{_n(y(v))}" y2="{_n(y(v))}" stroke-opacity=".3"/>')
        parts.append(
            f'<text x="{_n(left - 6)}" y="{_n(y(v) + 5)}" text-anchor="end">'
            f'{esc("".join(("$", _txt_num(v / 1000, 0), "k")))}</text>'
        )
    cap = []
    for j, (label, b) in enumerate(series):
        line_class_html = "l bh" if label == "buy_and_hold" else ("l c0", "l c1", "l c2")[min(j, 2)]
        points_html = " ".join(",".join((_n(x(i)), _n(y(v)))) for i, v in enumerate(b["equity"]))
        parts.append(f'<polyline class="{line_class_html}" pathLength="1" points="{points_html}"/>')
        sharpe = "n/a" if b.get("sharpe") is None else format(b["sharpe"], ".2f")
        cap.append("".join((label.replace("_", " "), ": $", _txt_num(b["final"], 0), " SIMULATED, Sharpe ", sharpe)))
    parts.append("</svg>")
    svg_html = "".join(parts)
    return (
        '<section id="engine"><h2>The cone rule in the real engine</h2>'
        "<p>Long a name only while its cone reads TRENDING UP, re-read every horizon. Same fills, costs, "
        'single-name cap and risk engine as buy-and-hold. <span class="sim">SIMULATED</span></p>'
        f'<figure class="chart">{svg_html}<figcaption>{esc("; ".join(cap))}.</figcaption></figure></section>'
    )


def render_forecast_page(data: ForecastPageData, now: datetime | None = None) -> str:
    now = now or datetime.now()
    note_html = f'<p class="empty-state">{esc(data.note)}</p>' if data.note else ""
    body_html = "".join((
        figures_section(data.record, data.backtest),
        live_section(data.live),
        replay_section(data.replays),
        evidence_section(data.record),
        backtest_section(data.backtest),
    ))
    css_html = "".join((CSS, EXTRA_CSS))
    tabs_html = tab_strip("forecast.html")
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>SIGNALFORGE Kronos Trial</title>"
        f"<style>{css_html}</style></head><body>"
        "<header><h1>SIGNALFORGE operator dashboard</h1>"
        '<p class="sim-banner" role="note"><strong>SIMULATION ONLY.</strong> '
        "Nothing on this page is live trading. A forecast cone is a research statistic, not an order.</p>"
        f"{tabs_html}"
        f'<p class="muted">Rendered {esc(now.strftime("%Y-%m-%d %H:%M"))} local time from the last forecast trial.</p>'
        f"{note_html}</header>"
        f"<main>{body_html}</main>"
        '<footer><p class="muted">Static page. It loads nothing from the network and runs no script.</p></footer>'
        "</body></html>"
    )


def write_forecast_page(path, data: ForecastPageData, now: datetime | None = None) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_forecast_page(data, now), encoding="utf-8")
    return out
