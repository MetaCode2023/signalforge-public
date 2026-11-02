"""The Allocation tab: what the trend book holds this month, and how far each asset is from its high.

Same rules as the other pages: static HTML, no script, nothing loaded from the
network, every interpolation escaped, and DATA UNAVAILABLE until a state has
been computed. The state is the rule's position now, never a performance
figure, so nothing here reads or reveals an evaluation window.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from signalforge.allocation_state import ON, OFF, YOUNG, MarketState
from signalforge.dashboard.charts import DATA_UNAVAILABLE, esc
from signalforge.dashboard.render import CSS, tab_strip

EXTRA_CSS = """
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:12px 0}
.cards div{border:1px solid var(--line);border-radius:10px;padding:12px}
.cards strong{display:block;font-size:1.45rem;font-variant-numeric:tabular-nums}
.cards span{font-size:13px;color:var(--muted)}
.state{display:inline-block;min-width:4.5em;text-align:center;font-weight:700;border-radius:999px;padding:2px 10px;border:2px solid}
.state-on{border-color:var(--accent)}
.state-off{border-style:dashed;color:var(--muted)}
.state-young{border-style:dotted;color:var(--muted)}
td.num{font-variant-numeric:tabular-nums;text-align:right}
"""

STATE_LABEL = {ON: "HELD", OFF: "CASH", YOUNG: "WAITING"}
STATE_CLASS = {ON: "state-on", OFF: "state-off", YOUNG: "state-young"}


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{float(value) * 100:+.1f}%"


def cards_html(s: MarketState) -> str:
    invested = float(s.invested) * 100
    held = sum(1 for a in s.assets if a.state == ON)
    cards = (
        (f"{invested:.0f}% invested", f"{held} of {len(s.assets)} assets held at {float(s.target_weight) * 100:.0f}% each; "
                                      f"{100 - invested:.0f}% in cash"),
        (f"{s.reference:%b %d, %Y}", "the close this month's decision was taken from"),
        (f"{s.next_decision_month:%B %Y}", "next decision, on the month's first trading session"),
        (f"{s.as_of:%b %d, %Y}", "latest bar in the warehouse"),
    )
    items = "".join(f"<div><strong>{esc(big)}</strong><span>{esc(small)}</span></div>" for big, small in cards)
    return f'<div class="cards">{items}</div>'


def table_html(s: MarketState) -> str:
    rows = []
    for a in s.assets:
        stale = ' <span class="stale">stale</span>' if a.stale else ""
        last = "none" if a.last_date is None else f"{a.last_date:%Y-%m-%d}"
        rows.append(
            "<tr>"
            f'<td data-label="Fund"><strong>{esc(a.symbol)}</strong></td>'
            f'<td data-label="Holds">{esc(a.asset_class or "")}</td>'
            f'<td data-label="This month"><span class="state {STATE_CLASS[a.state]}">{esc(STATE_LABEL[a.state])}</span></td>'
            f'<td data-label="12-month return at decision" class="num">{esc(_pct(a.decided_return))}</td>'
            f'<td data-label="12-month return today" class="num">{esc(_pct(a.current_return))}</td>'
            f'<td data-label="Below 1-year high" class="num">{esc(_pct(a.below_high))}</td>'
            f'<td data-label="Last close">{esc(last)}{stale}</td>'
            "</tr>"
        )
    head = (
        "<thead><tr><th>Fund</th><th>Holds</th><th>This month</th><th>12-month return at decision</th>"
        "<th>12-month return today</th><th>Below 1-year high</th><th>Last close</th></tr></thead>"
    )
    return (
        '<section id="allocation-table"><h2>The book this month</h2>'
        f'<div class="table-wrap"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'
        f'<p class="muted">An asset is held while its own {s.lookback}-session return, measured to the close '
        "before the month began, is positive. Otherwise its slot sits in cash. "
        '"Below 1-year high" uses dividend-adjusted closes.</p></section>'
    )


def watch_html(s: MarketState) -> str:
    leaning = [a for a in s.assets if a.flip_watch]
    if not leaning:
        body = ("<p>Every asset's trailing return still agrees with this month's decision, "
                "so no change is pointing at next month yet.</p>")
    else:
        items = "".join(
            f"<li><strong>{esc(a.symbol)}</strong> is {esc('held' if a.state == ON else 'in cash')} this month, "
            f"but its 12-month return is now {esc(_pct(a.current_return))}. If that holds to the month's last "
            f"close it {esc('moves to cash' if a.state == ON else 'is bought')} next month.</li>"
            for a in leaning
        )
        body = f"<ul>{items}</ul>"
    return (
        '<section id="allocation-watch"><h2>Leaning for next month</h2>'
        f"{body}<p class=\"muted\">Nothing changes before the first session of {esc(f'{s.next_decision_month:%B %Y}')}. "
        "A mid-month move is deliberately not acted on.</p></section>"
    )


def render_allocation_page(state: MarketState | None, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if state is None:
        body_html = (
            '<section id="allocation-empty"><h2>No allocation state yet</h2>'
            f'<p class="empty-state">{esc(DATA_UNAVAILABLE)}: the allocation state has not been computed. Run '
            "<code>python -m signalforge.allocation_cli --state</code>.</p></section>"
        )
        title_html = ""
    else:
        body_html = "".join((cards_html(state), table_html(state), watch_html(state)))
        title_html = f'<p class="muted">Allocation book {esc(state.name)}, rule decided on the close of {esc(state.reference)}.</p>'
    css_html = "".join((CSS, EXTRA_CSS))
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>SIGNALFORGE Allocation</title>"
        f"<style>{css_html}</style></head><body>"
        "<header><h1>SIGNALFORGE operator dashboard</h1>"
        '<p class="sim-banner" role="note"><strong>SIMULATION ONLY.</strong> '
        "Nothing on this page is an order. It shows where the research rule stands; no money follows it.</p>"
        f"{tab_strip('allocation.html')}"
        f"{title_html}"
        f'<p class="muted">Rendered {esc(now.strftime("%Y-%m-%d %H:%M"))} local time.</p></header>'
        f"<main>{body_html}</main>"
        '<footer><p class="muted">Static page. It loads nothing from the network and runs no script. '
        "Prices: Tiingo daily adjusted closes from the local warehouse. Research basis: docs/DECISIONS.md 19.</p></footer>"
        "</body></html>"
    )


def write_allocation_page(path, state: MarketState | None, now: datetime | None = None) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_allocation_page(state, now), encoding="utf-8")
    return out
