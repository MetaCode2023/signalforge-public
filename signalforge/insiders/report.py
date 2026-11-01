"""Assay records: aggregate JSON for the repo, a markdown table for reading."""

from __future__ import annotations

import math
from dataclasses import asdict

from signalforge.insiders.study import ARMS, StudyResult

LABELS = {
    "all": "Every insider buy",
    "cluster": "2+ insiders buying",
    "smart_past": "Smart insiders, judged on the past",
    "smart_hindsight": "Smart insiders, judged with hindsight (cheating)",
}


def _clean(value):
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else round(value, 6)
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def record(result: StudyResult, provenance: dict | None = None) -> dict:
    p = result.params
    params = asdict(p)
    params["window_start"], params["window_end"] = p.window_start.isoformat(), p.window_end.isoformat()
    return _clean({
        "kind": "insider_assay",
        "provenance": provenance or {},
        "params": params,
        "funnel": [{"step": s, "count": n} for s, n in result.funnel],
        "excluded": result.excluded,
        "sizes": result.sizes,
        "scored": result.scored,
        "arms": {
            name: {"label": LABELS[name], "by_horizon": {str(h): asdict(s) for h, s in result.arms[name].items()},
                   "portfolio": asdict(result.portfolios[name])}
            for name in ARMS
        },
        "nulls": result.nulls,
    })


def _pct(x, digits=2):
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def render_markdown(rec: dict) -> str:
    hs = rec["params"]["horizons"]
    head = "| Strategy | n | " + " | ".join(f"Excess {h}d" for h in hs) + " | Hit rate | Book CAGR | Book alpha (t) | Evidence |"
    lines = [head, "|" + "---|" * (len(hs) + 6)]
    primary = str(rec["params"]["primary"])
    for name in ARMS:
        a = rec["arms"][name]
        s = a["by_horizon"][primary]
        pf = a["portfolio"]
        t = "n/a" if pf["alpha_t"] is None else f"{pf['alpha_t']:.1f}"
        hit = "n/a" if s["hit_rate"] is None else f"{s['hit_rate']:.0%}"
        lines.append(
            f"| {a['label']} | {s['n']} | "
            + " | ".join(_pct(a["by_horizon"][str(h)]["mean"]) for h in hs)
            + f" | {hit} | {_pct(pf['cagr'], 1)} | {_pct(pf['alpha_annual'], 1)} ({t}) | {s['evidence']} |"
        )
    lines.append("")
    for key, n in rec["nulls"].items():
        lines.append(
            f"- {key.replace('_', ' ')}: observed {_pct(n.get('observed'))}, random median {_pct(n.get('median'))}, "
            f"95% of draws {_pct(n.get('lo'))} to {_pct(n.get('hi'))}, p = {n.get('p_value')}"
        )
    return "\n".join(lines) + "\n"
