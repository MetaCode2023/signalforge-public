"""One offline historical engine replay; never consumes registry holdout state."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path


def run_verification(archive: Path, output: Path):
    from signalforge.backtesting.engine import BacktestEngine
    from signalforge.backtesting.metrics import compute_metrics
    from signalforge.config.models import load_research, load_risk
    from signalforge.core.history import PriceHistory
    from signalforge.data.validation import validate_bar_records
    from signalforge.strategies.trend import MovingAverageTrend

    raw = archive.read_bytes()
    records = []
    with archive.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if row["symbol"] != "AAPL":
                continue
            records.append({**row, "trade_date": row["date"], "adj_close": row["adjusted_close"],
                            "source": "plotly_aapl_sample"})
    checked = validate_bar_records(records, as_of=date(2017, 2, 16))
    if checked.quarantined or len(checked.accepted) < 252:
        raise ValueError("the archived AAPL sample did not pass bounded replay validation")
    history = PriceHistory.from_bars(checked.accepted)
    research, risk = load_research(), load_risk()
    strategy = MovingAverageTrend(20, 50, Decimal("0.05"), ("AAPL",))
    limitations = [
        "Historical engineering smoke test, not independent out-of-sample evidence or a promotion.",
        "One surviving stock (AAPL), 506 daily sample bars in 2015-2017; not a broad or current universe.",
        "No intraday, bid/ask, or order-book data; fills and gaps are modelled from daily OHLCV.",
        "Adjusted closes inform signals; explicit historical dividend/split cash flows are unavailable in this sample.",
        "The existing 5% single-name exposure cap leaves most simulated capital in cash.",
    ]
    result = BacktestEngine(research, risk).run(
        strategy, history, start=date(2015, 2, 17), end=date(2017, 2, 16),
        run_id="bounded-aapl-engine-verification", data_limitations=limitations,
    )
    metrics = compute_metrics(result, config=research)
    payload = {
        "paper_only": True, "purpose": "bounded historical engine verification",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0], "declared_python_requirement_met": sys.version_info >= (3, 12),
        "dataset": "plotly_aapl_sample", "source_file": str(archive),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "dataset_fingerprint": result.dataset_fingerprint,
        "symbol": "AAPL", "start": str(result.start_date), "end": str(result.end_date),
        "bar_count": result.bar_count, "strategy": result.strategy_id,
        "parameters": strategy.parameters(),
        "execution": research.execution.model_dump(mode="json"),
        "risk": risk.model_dump(mode="json"), "starting_cash": str(result.starting_cash),
        "final_equity": str(result.final_equity), "metrics": metrics.to_dict(),
        "data_limitations": limitations, "quality_flag_count": len(checked.flags),
        "holdout_registry_modified": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/archive/aapl_sample_2015_2017.csv"))
    parser.add_argument("--out", type=Path, default=Path("data/verification/bounded-backtest.json"))
    args = parser.parse_args(argv)
    try:
        payload = run_verification(args.archive, args.out)
    except Exception as exc:
        print(f"bounded verification failed ({type(exc).__name__})")
        return 2
    print(json.dumps({"output": str(args.out), "dataset": payload["dataset"],
                      "start": payload["start"], "end": payload["end"],
                      "bars": payload["bar_count"], "final_equity": payload["final_equity"],
                      "evidence": payload["metrics"]["evidence"],
                      "python_requirement_met": payload["declared_python_requirement_met"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
