"""Inline SVG charts, generated here with no chart library.

Every chart is a <figure> holding an SVG with a <title> and <desc>, plus a
visible <figcaption> that states the same data as text, so a screen reader or a
reader who cannot make out a small plot gets the numbers. A chart with too
little data is not drawn as an empty frame: it says DATA UNAVAILABLE and why.
"""

from __future__ import annotations

import html as _html
from datetime import date
from decimal import Decimal
from typing import Callable, Mapping, Sequence

DATA_UNAVAILABLE = "DATA UNAVAILABLE"

WIDTH, HEIGHT = 640, 240
LEFT, RIGHT, TOP, BOTTOM = 100, 12, 12, 32
ROW = 34

# A plot this wide cannot show more distinct x positions than it has pixels,
# so coordinates beyond this budget cost bytes and buy nothing. A 14,048-point
# curve drawn in full produced a 14.7 MB page, which is unusable on the phone
# this dashboard is meant to be read on.
MAX_CHART_POINTS = 600


def downsample(
    points: Sequence[tuple[date, Decimal]], budget: int = MAX_CHART_POINTS
) -> list[tuple[date, Decimal]]:
    """Reduce a series to `budget` points while keeping both extremes.

    Each bucket contributes its minimum AND its maximum, so a one-day trough
    is never smoothed away. That matters more here than byte count: dropping
    the bottom of a drawdown would make a risk chart show a shallower loss
    than actually happened.

    Callers must still compute every stated statistic from the FULL series.
    This function is for drawing only.
    """
    count = len(points)
    if count <= 2 or count <= budget:
        return list(points)
    if budget < 4:
        return [points[0], points[-1]]

    interior = list(points[1:-1])
    buckets = max(1, (budget - 2) // 2)
    width = len(interior) / buckets
    out: list[tuple[date, Decimal]] = [points[0]]
    for index in range(buckets):
        start = int(index * width)
        stop = int((index + 1) * width)
        chunk = interior[start:stop] if stop > start else interior[start : start + 1]
        if not chunk:
            continue
        lowest = min(chunk, key=lambda item: item[1])
        highest = max(chunk, key=lambda item: item[1])
        for point in sorted({lowest, highest}, key=lambda item: item[0]):
            out.append(point)
    out.append(points[-1])
    return out


def _downsample_note_html(drawn: int, total: int) -> str:
    if drawn >= total:
        return ""
    return (
        f" Plotted from {esc(drawn)} of {esc(total)} points (downsampled for "
        "legibility; every figure quoted here is computed from all "
        f"{esc(total)}, and the highest and lowest points are preserved)."
    )


def esc(value) -> str:
    return _html.escape(str(value), quote=True)


def _n(value) -> str:
    return format(float(value), ".1f")


def plain_html(value) -> str:
    return esc(format(Decimal(value), ",.2f"))


def pct(value) -> str:
    return esc("".join((format(Decimal(value) * 100, ".2f"), "%")))


def unavailable(title: str, reason: str) -> str:
    return (
        '<figure class="chart chart-na">'
        '<span class="prov prov-na">DATA UNAVAILABLE</span> '
        f"<strong>{esc(title)}</strong>"
        f"<p>{esc(reason)}</p>"
        "</figure>"
    )


def _drawdowns(values: Sequence[Decimal]) -> list[Decimal]:
    out, peak = [], None
    for v in values:
        peak = v if peak is None or v > peak else peak
        out.append((peak - v) / peak if peak > 0 else Decimal(0))
    return out


def _scale(lo: float, hi: float) -> tuple[float, float]:
    if hi == lo:
        pad = abs(hi) * 0.01 or 1.0
        return lo - pad, hi + pad
    return lo, hi


def _xs(count: int) -> list[float]:
    span = WIDTH - LEFT - RIGHT
    return [LEFT + span * i / (count - 1) for i in range(count)]


def _ys(values: Sequence[float], lo: float, hi: float) -> list[float]:
    span = HEIGHT - TOP - BOTTOM
    return [TOP + span * (hi - v) / (hi - lo) for v in values]


def coords_html(xs: Sequence[float], ys: Sequence[float]) -> str:
    return " ".join(f"{_n(x)},{_n(y)}" for x, y in zip(xs, ys))


def _frame(uid: str, title: str, desc_html: str, body_html: str, caption_html: str, height: float = HEIGHT) -> str:
    return (
        f'<figure class="chart" id="{esc(uid)}">'
        f'<svg viewBox="0 0 {_n(WIDTH)} {_n(height)}" role="img" '
        f'aria-labelledby="{esc(uid)}-t {esc(uid)}-d" preserveAspectRatio="xMidYMid meet">'
        f'<title id="{esc(uid)}-t">{esc(title)}</title>'
        f'<desc id="{esc(uid)}-d">{desc_html}</desc>'
        f"{body_html}"
        "</svg>"
        f"<figcaption>{caption_html}</figcaption>"
        "</figure>"
    )


def _axis_html(x1: float, y1: float, x2: float, y2: float) -> str:
    return f'<line class="axis" x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" />'


def _tick_html(x: float, y: float, text_html: str, anchor: str = "start") -> str:
    return f'<text class="tick" x="{_n(x)}" y="{_n(y)}" text-anchor="{esc(anchor)}">{text_html}</text>'


def _date_ticks_html(first: date, last: date) -> str:
    return (
        _tick_html(LEFT, HEIGHT - 8, esc(first.isoformat()))
        + _tick_html(WIDTH - RIGHT, HEIGHT - 8, esc(last.isoformat()), "end")
    )


def equity_chart(
    points: Sequence[tuple[date, Decimal]],
    *,
    title: str,
    fmt_value: Callable[[Decimal], str] | None = None,
    uid: str = "equity-chart",
) -> str:
    if len(points) < 2:
        return unavailable(
            title, f"Checked: {esc(len(points))} equity points recorded; at least 2 are needed to draw a curve."
        )
    fmt = fmt_value or plain_html
    dates = [d for d, _ in points]
    values = [Decimal(v) for _, v in points]
    floats = [float(v) for v in values]
    peaks, running = [], floats[0]
    for f in floats:
        running = max(running, f)
        peaks.append(running)
    lo, hi = _scale(min(floats), max(peaks))
    depth = max(_drawdowns(values))

    # Statistics above come from the full series; only the drawing is reduced.
    peak_by_date = dict(zip(dates, peaks))
    drawn = downsample(points)
    drawn_dates = [d for d, _ in drawn]
    drawn_floats = [float(v) for _, v in drawn]
    drawn_peaks = [peak_by_date[d] for d in drawn_dates]
    xs = _xs(len(drawn))
    ys = _ys(drawn_floats, lo, hi)
    peak_ys = _ys(drawn_peaks, lo, hi)

    shade_html = ""
    if depth > 0:
        outline_html = f"{coords_html(xs, peak_ys)} {coords_html(list(reversed(xs)), list(reversed(ys)))}"
        shade_html = f'<polygon class="dd-shade" points="{outline_html}" />'
    base = HEIGHT - BOTTOM
    body_html = (
        _axis_html(LEFT, TOP, LEFT, base)
        + _axis_html(LEFT, base, WIDTH - RIGHT, base)
        + _tick_html(LEFT - 4, TOP + 10, plain_html(Decimal(str(hi))), "end")
        + _tick_html(LEFT - 4, base, plain_html(Decimal(str(lo))), "end")
        + _date_ticks_html(dates[0], dates[-1])
        + shade_html
        + f'<polyline class="eq-line" fill="none" points="{coords_html(xs, ys)}" />'
    )
    desc_html = (
        f"{esc(title)}: SIMULATED equity, {esc(len(points))} daily points from {esc(dates[0].isoformat())} "
        f"to {esc(dates[-1].isoformat())}, ending at {plain_html(values[-1])}."
    )
    start_html, end_html = fmt(values[0]), fmt(values[-1])
    low_html, high_html = fmt(min(values)), fmt(max(values))
    caption_html = (
        f"{esc(title)}: SIMULATED equity, {esc(len(points))} daily points from {esc(dates[0].isoformat())} "
        f"to {esc(dates[-1].isoformat())}. Start {start_html}, end {end_html}, "
        f"lowest {low_html}, highest {high_html}, deepest drawdown {pct(depth)}."
        + _downsample_note_html(len(drawn), len(points))
    )
    return _frame(uid, title, desc_html, body_html, caption_html)


def drawdown_chart(
    points: Sequence[tuple[date, Decimal]],
    *,
    title: str,
    uid: str = "drawdown-chart",
) -> str:
    if len(points) < 2:
        return unavailable(
            title, f"Checked: {esc(len(points))} equity points recorded; at least 2 are needed to draw drawdown."
        )
    dates = [d for d, _ in points]
    dds = _drawdowns([Decimal(v) for _, v in points])
    worst = max(dds)
    worst_at = dates[dds.index(worst)]
    depth = float(worst) if worst > 0 else 0.01
    # Downsample the drawdown series itself, so the deepest point survives.
    drawn = downsample(list(zip(dates, dds)))
    xs = _xs(len(drawn))
    ys = _ys([float(v) for _, v in drawn], 0.0, depth)
    area_html = f"{_n(xs[0])},{_n(TOP)} {coords_html(xs, ys)} {_n(xs[-1])},{_n(TOP)}"
    body_html = (
        _axis_html(LEFT, TOP, WIDTH - RIGHT, TOP)
        + _tick_html(LEFT - 4, TOP + 10, "0%", "end")
        + _tick_html(LEFT - 4, HEIGHT - BOTTOM, f"-{pct(worst)}", "end")
        + _date_ticks_html(dates[0], dates[-1])
        + f'<polygon class="uw-area" points="{area_html}" />'
    )
    desc_html = (
        f"{esc(title)}: SIMULATED drawdown below the running peak, deepest {pct(worst)} "
        f"on {esc(worst_at.isoformat())}."
    )
    caption_html = (
        f"{esc(title)}: SIMULATED drawdown below the running peak over {esc(len(points))} daily points. "
        f"Deepest {pct(worst)} on {esc(worst_at.isoformat())}; final {pct(dds[-1])}."
        + _downsample_note_html(len(drawn), len(points))
    )
    return _frame(uid, title, desc_html, body_html, caption_html)


def risk_event_chart(
    counts: Mapping[str, int],
    *,
    title: str,
    uid: str = "risk-chart",
) -> str:
    if not counts:
        return unavailable(
            title, "Checked: no risk events were recorded for this run, so there is nothing to chart."
        )
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    peak = max(c for _, c in rows)
    span = WIDTH - 24
    parts, items = [], []
    for i, (rule, count) in enumerate(rows):
        y = 8 + i * ROW
        width = max(span * count / peak, 2.0)
        parts.append(f'<text class="label" x="12" y="{_n(y + 11)}">{esc(rule)}: {esc(count)}</text>')
        parts.append(f'<rect class="bar" x="12" y="{_n(y + 15)}" width="{_n(width)}" height="12" rx="2" />')
        items.append(f"<li>{esc(rule)}: {esc(count)}</li>")
    total = sum(counts.values())
    list_html = "".join(items)
    desc_html = f"{esc(title)}: {esc(total)} risk events across {esc(len(rows))} rules."
    caption_html = f'{desc_html}<ul class="chart-list">{list_html}</ul>'
    return _frame(uid, title, desc_html, "".join(parts), caption_html, height=8 + len(rows) * ROW)
