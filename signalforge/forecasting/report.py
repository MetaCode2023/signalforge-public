"""Trial records: an aggregate JSON for the repo, a per-forecast file for the operator.

The aggregate holds statistics only. The per-forecast detail carries realized
closes derived from licensed bars, so it is written under data/ (gitignored)
and never committed.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path

from signalforge.forecasting.scoring import TrialSummary, crps_skill
from signalforge.forecasting.trial import TrialResult


def _clean(value):
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, 6)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def summary_dict(result: TrialResult, summary: TrialSummary) -> dict:
    body = asdict(summary)
    body["confidence_lift"] = summary.confidence_lift
    for group in ("by_agreement", "by_regime", "by_spread"):
        for bucket, stat in zip(body[group], getattr(summary, group)):
            bucket["hit_rate"] = stat.hit_rate
    return _clean({
        "forecaster": result.forecaster_id,
        "parameters": {k: str(v) for k, v in result.parameters.items()},
        "symbols": list(result.symbols),
        "window": [result.start.isoformat(), result.end.isoformat()],
        "horizon": result.horizon,
        "n_paths": result.n_paths,
        "stride": result.stride,
        "seed": result.seed,
        "trend_agreement": result.trend_agreement,
        "skipped": result.skipped,
        "summary": body,
    })


def trial_record(results: list[TrialResult], *, null_id: str | None = None, provenance: dict | None = None) -> dict:
    by_id = {r.forecaster_id: r for r in results}
    record = {
        "kind": "forecast_trial",
        "provenance": provenance or {},
        "forecasters": [summary_dict(r, r.summary()) for r in results],
    }
    if null_id and null_id in by_id:
        record["crps_skill_vs_" + null_id] = {
            r.forecaster_id: round(crps_skill(r.outcomes, by_id[null_id].outcomes), 6)
            for r in results
            if r.forecaster_id != null_id
        }
    return record


def write_detail(result: TrialResult, path: Path) -> None:
    """One JSON line per forecast. Licensed-derived: keep under data/."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for o in result.outcomes:
            r = o.reading
            fh.write(json.dumps({
                "symbol": o.symbol, "as_of": o.as_of.isoformat(), "last_close": r.last_close,
                "p10": r.p10, "median": r.median, "p90": r.p90, "paths_up": r.paths_up,
                "n_paths": r.n_paths, "regime": r.regime.value, "realized_close": o.realized_close,
                "pit": o.pit, "crps": o.crps, "covered": o.covered, "hit": o.hit,
            }) + "\n")


def _lift(value) -> str:
    return "n/a (too few calls)" if value is None else f"{value:+.1%}"


def render_markdown(record: dict) -> str:
    lines = ["| Forecaster | n (indep.) | 10-90 coverage | Mean CRPS | Hit rate | Up base rate | TRENDING share | Hit TRENDING | Hit CHOPPY | Lift |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for f in record["forecasters"]:
        s = f["summary"]
        regime = {b["label"]: b for b in s["by_regime"]}
        def pct(x):
            return "n/a" if x is None else f"{x:.1%}"
        lines.append(
            f"| {f['forecaster']} | {s['n']} ({s['n_independent']}) | {pct(s['coverage'])} | "
            f"{s['mean_crps']:.4f} | {pct(s['hit_rate'])} | {pct(s['base_rate_up'])} | "
            f"{pct(s['trending_share'])} | {pct(regime['TRENDING'].get('hit_rate'))} "
            f"(n={regime['TRENDING']['n']}) | {pct(regime['CHOPPY'].get('hit_rate'))} | "
            f"{_lift(s['confidence_lift'])} |"
        )
    return "\n".join(lines) + "\n"
