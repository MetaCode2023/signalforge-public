"""Forecast trials: put a path forecaster (Kronos or a null) on trial.

    python -m signalforge.forecast_cli trial --forecaster bootstrap,random-walk \\
        --symbols SPY,AAPL,JPM --window validation
    python -m signalforge.forecast_cli trial --forecaster kronos,bootstrap,random-walk \\
        --symbols SPY --window validation --model-dir models/kronos-small \\
        --tokenizer-dir models/kronos-tokenizer-base --kronos-src models/kronos-src
    python -m signalforge.forecast_cli cone --symbol SPY --forecaster bootstrap
    python -m signalforge.forecast_cli kronos-check --model-dir ... --tokenizer-dir ... --kronos-src ...

Trials run on the train or validation window only. The held-out test window
is refused here: it is spent once, through `research_cli --run-holdout`, and
a forecast trial that peeked at it first would have turned it into a tuning
set. `cone` forecasts forward from the latest bar, so it scores nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from signalforge.config.models import load_all
from signalforge.core.errors import ConfigError, SignalForgeError, ValidationError
from signalforge.data.storage import Warehouse
from signalforge.forecasting.baselines import BlockBootstrapForecaster, DriftlessRandomWalk
from signalforge.forecasting.cone import DEFAULT_TREND_AGREEMENT, read_cone
from signalforge.forecasting.kronos import KronosForecaster
from signalforge.forecasting.paths import ContextWindow
from signalforge.forecasting.report import render_markdown, trial_record, write_detail
from signalforge.forecasting.paths import derive_seed
from signalforge.forecasting.scoring import score
from signalforge.forecasting.trial import forecast_latest, run_trial
from signalforge.learning.registry import current_git_commit
from signalforge.pipeline import ohlcv_history

DEFAULT_DB = Path("data") / "warehouse" / "signalforge.duckdb"
DEFAULT_OUT = Path("reports") / "forecast_trials"
DETAIL_DIR = Path("data") / "forecast_trials"
DEFAULT_PAGE = Path("dashboard") / "build" / "forecast.html"
REPLAYS = 6
FORECASTERS = ("random-walk", "bootstrap", "kronos")


def build_forecaster(name: str, args) -> object:
    if name == "random-walk":
        return DriftlessRandomWalk(context_length=args.context)
    if name == "bootstrap":
        return BlockBootstrapForecaster(context_length=args.context, block=args.block)
    if name == "kronos":
        if not args.model_dir or not args.tokenizer_dir:
            raise ConfigError("kronos needs --model-dir and --tokenizer-dir (local weights)")
        return KronosForecaster(
            args.model_dir, args.tokenizer_dir, kronos_src=args.kronos_src,
            device=args.device, context_length=args.context,
        )
    raise ConfigError(f"unknown forecaster {name!r}; choose from {FORECASTERS}")


def trial_window(args, splits) -> tuple[date, date]:
    if args.window == "train":
        start, end = splits.train_start, splits.train_end
    elif args.window == "validation":
        start, end = splits.validation_start, splits.validation_end
    else:
        raise ConfigError(
            "forecast trials run on 'train' or 'validation' only. The test window is "
            "spent once, through research_cli --run-holdout."
        )
    if args.start:
        start = max(start, date.fromisoformat(args.start))
    if args.end:
        end = min(end, date.fromisoformat(args.end))
    if end >= splits.test_start:
        raise ConfigError(f"trial window ends {end}, inside the held-out test window")
    return start, end


def open_warehouse(path: str) -> Warehouse:
    if not Path(path).is_file():
        raise ConfigError(
            f"no warehouse at {path}. Build it first (README, Quick start) or pass --db."
        )
    return Warehouse(path, read_only=True)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--config-dir", default=None)
    p.add_argument("--source", default=None, help="restrict bars to one source, e.g. tiingo")
    p.add_argument("--horizon", type=int, default=24)
    p.add_argument("--paths", type=int, default=100)
    p.add_argument("--context", type=int, default=512)
    p.add_argument("--block", type=int, default=5)
    p.add_argument("--seed", type=int, default=20261031)
    p.add_argument("--trend-agreement", type=float, default=DEFAULT_TREND_AGREEMENT)
    p.add_argument("--model-dir", default=None)
    p.add_argument("--tokenizer-dir", default=None)
    p.add_argument("--kronos-src", default=None)
    p.add_argument("--device", default="cpu")


def cmd_trial(args) -> int:
    bundle = load_all(Path(args.config_dir) if args.config_dir else None)
    start, end = trial_window(args, bundle.research.splits)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    names = [n.strip() for n in args.forecaster.split(",") if n.strip()]
    forecasters = [build_forecaster(n, args) for n in names]
    context_days = int(args.context * 1.6) + 30
    wh = open_warehouse(args.db)
    try:
        hist = ohlcv_history(
            wh, symbols, [args.source] if args.source else None, price_basis="adjusted",
            start=start - timedelta(days=context_days), end=end,
        )
    finally:
        wh.close()
    results = []
    for f in forecasters:
        t0 = time.monotonic()
        res = run_trial(
            hist.history, symbols, f, start=start, end=end, horizon=args.horizon,
            n_paths=args.paths, stride=args.stride, seed=args.seed,
            trend_agreement=args.trend_agreement,
        )
        print(f"{f.forecaster_id}: {len(res.outcomes)} forecasts in {time.monotonic() - t0:.1f}s")
        write_detail(res, DETAIL_DIR / f"{f.forecaster_id}-{start}-{end}.jsonl")
        results.append(res)
    record = trial_record(
        results,
        null_id="random_walk" if "random-walk" in names else None,
        provenance={
            "git_commit": current_git_commit(),
            "dataset": hist.description,
            "limitations": list(hist.limitations),
        },
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"trial-{'+'.join(r.forecaster_id for r in results)}-{start}-{end}"
    (out / f"{stem}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    table = render_markdown(record)
    (out / f"{stem}.md").write_text(table, encoding="utf-8")
    print(table)
    if args.no_dashboard and not (args.report or args.report_dry_run or args.backtest):
        return 0
    data = build_page(args, record, hist.history, results, forecasters, symbols, start, end, bundle)
    if data.backtest:
        # The engine run belongs in the saved record too, so the scoreboard can score it later.
        saved = with_backtest(record, data.backtest)
        (out / f"{stem}.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
    if not args.no_dashboard:
        from signalforge.dashboard.forecast_page import write_forecast_page
        from signalforge.dashboard.scoreboard_page import refresh_scoreboard

        print("wrote", write_forecast_page(args.dashboard_out, data))
        board = refresh_scoreboard(Path(args.dashboard_out).parent / "scoreboard.html", args.db, out.parent)
        if board is not None:
            print("wrote", board)
    if args.report or args.report_dry_run:
        return send_report(args, record, data, symbols, start, end)
    return 0


def with_backtest(record: dict, backtest: dict) -> dict:
    """A copy of the trial record carrying each engine run's final equity and Sharpe (no curve, no prices)."""
    summary = {
        label: {"final": run.get("final"), "sharpe": run.get("sharpe")}
        for label, run in backtest.items()
    }
    return {**record, "backtest": summary}


def send_report(args, record, data, symbols, start, end) -> int:
    """Post the Crucible report (scores and a forward cone in returns, no prices)."""
    from datetime import datetime, timezone

    from signalforge.forecasting.report import backtest_payload, cone_payload
    from signalforge.reports import events as ev
    from signalforge.reports import sender

    commit = current_git_commit()
    if not ev._SHA.match(commit):
        print("refused: the report needs a git commit to cite; run from a git checkout", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    event = ev.build_forecast_trial(
        record, now, commit_sha=commit, symbols=symbols, window=(start, end),
        horizon=args.horizon, n_paths=args.paths, trend_agreement=args.trend_agreement,
        cone=cone_payload(data.live[0].forecast, args.trend_agreement) if data.live else None,
        backtest=backtest_payload(data.backtest, start, end) if data.backtest else None,
    )
    try:
        ev.validate_event(event)
    except ev.EventInvalid as exc:
        print(f"refused: the Crucible report would be rejected: {exc}", file=sys.stderr)
        return 2
    if args.report_dry_run:
        print(ev.serialize(event).decode("utf-8"))
        print("dry run: Crucible report valid, nothing sent")
        return 0
    try:
        credentials = sender.load_credentials()
    except sender.MissingCredentials as exc:
        print(str(exc), file=sys.stderr)
        return 2
    import requests

    outcome = sender.send(event, requests.Session(), credentials)
    state = "accepted" if outcome.ok else f"FAILED {outcome.error_code or outcome.status_code}"
    print(f"Crucible report {outcome.event_id}: {state}")
    return 0 if outcome.ok else 1


def _equity_points(result, budget: int = 300) -> list[float]:
    eq = [float(p.equity) for p in result.equity_curve]
    step = max(1, len(eq) // budget)
    return eq[::step] + ([eq[-1]] if (len(eq) - 1) % step else [])


def build_page(args, record, history, results, forecasters, symbols, start, end, bundle):
    """The Crucible tab's data: live cones, replays, scores, optional engine run."""
    from decimal import Decimal

    from signalforge.backtesting.engine import BacktestEngine
    from signalforge.backtesting.metrics import compute_metrics
    from signalforge.dashboard.forecast_page import (
        ForecastPageData,
        LivePanel,
        ReplayPanel,
        write_forecast_page,
    )
    from signalforge.forecasting.strategy import ForecastConeStrategy
    from signalforge.strategies.benchmark import BuyAndHold

    primary_i = next((i for i, f in enumerate(forecasters) if f.forecaster_id == "kronos"), 0)
    primary = forecasters[primary_i]
    symbol = symbols[0]
    notes = []

    live = []
    wh = open_warehouse(args.db)
    try:
        latest = max(b.trade_date for b in wh.load_bars(symbols=[symbol]))
        recent = ohlcv_history(
            wh, [symbol], [args.source] if args.source else None, price_basis="adjusted",
            start=latest - timedelta(days=int(args.context * 1.6) + 30),
        )
    finally:
        wh.close()
    for f in [primary] + [g for g in forecasters if g is not primary]:
        try:
            fc = forecast_latest(recent.history, symbol, f, horizon=args.horizon, n_paths=args.paths, seed=args.seed)
            window = ContextWindow.from_view(recent.history.view(fc.as_of), symbol, f.context_length)
            live.append(LivePanel(fc, window))
        except ValidationError as exc:
            notes.append(f"No live cone from {f.forecaster_id}: {exc}")

    replays = []
    mine = [o for o in results[primary_i].outcomes if o.symbol == symbol]
    if mine:
        picks = sorted({round(i) for i in np.linspace(0, len(mine) - 1, min(REPLAYS, len(mine)))})
        bars = history.bars_for(symbol)
        index = {b.trade_date: i for i, b in enumerate(bars)}
        adj = [float(b.adj_close if b.adj_close is not None else b.close) for b in bars]
        for k in picks:
            origin = mine[k].as_of
            i = index[origin]
            window = ContextWindow.from_view(history.view(origin), symbol, primary.context_length)
            fc = primary.sample_paths(window, args.horizon, args.paths, derive_seed(args.seed, symbol, origin.isoformat()))
            realized = adj[i + 1:i + 1 + args.horizon]
            replays.append(ReplayPanel(fc, adj[max(0, i - 90):i + 1], realized, score(fc, realized, args.trend_agreement)))

    backtest = {}
    if args.backtest:
        research = bundle.research.model_copy(update={"universe": tuple(symbols)})
        weight = Decimal("0.05")
        for label, strat in (
            ("buy_and_hold", BuyAndHold(symbols, weight)),
            (f"cone_{primary.forecaster_id}", ForecastConeStrategy(
                primary, symbols, weight, horizon=args.horizon, n_paths=args.paths,
                trend_agreement=args.trend_agreement, seed=args.seed)),
        ):
            result = BacktestEngine(research, bundle.risk).run(strat, history, start=start, end=end)
            m = compute_metrics(result, config=research)
            backtest[label] = {
                "equity": _equity_points(result),
                "final": float(result.final_equity),
                "sharpe": None if m.sharpe is None else float(m.sharpe),
            }
    return ForecastPageData(record=record, live=live, replays=replays, backtest=backtest,
                            note=" ".join(notes) or None)


def cmd_cone(args) -> int:
    forecaster = build_forecaster(args.forecaster, args)
    wh = open_warehouse(args.db)
    try:
        latest = wh.load_bars(symbols=[args.symbol.upper()])
        if not latest:
            raise ValidationError(f"no bars for {args.symbol}")
        last = max(b.trade_date for b in latest)
        hist = ohlcv_history(
            wh, [args.symbol.upper()], [args.source] if args.source else None,
            price_basis="adjusted", start=last - timedelta(days=int(args.context * 1.6) + 30),
        )
    finally:
        wh.close()
    fc = forecast_latest(
        hist.history, args.symbol.upper(), forecaster,
        horizon=args.horizon, n_paths=args.paths, seed=args.seed,
    )
    r = read_cone(fc, args.trend_agreement)
    print(
        f"{fc.symbol} as of {fc.as_of} | {fc.forecaster_id} | next {fc.horizon} bars, "
        f"{fc.n_paths} paths\n  median {r.median:,.2f} ({r.median_return:+.2%})  "
        f"p10 {r.p10:,.2f}  p90 {r.p90:,.2f}  spread {r.spread_pct:.1%}\n"
        f"  {r.paths_up}/{r.n_paths} paths up -> {r.regime.value} {r.direction.value}\n"
        "  Forward forecast: nothing here is scored, and a cone is not a trade."
    )
    return 0


def cmd_kronos_check(args) -> int:
    forecaster = build_forecaster("kronos", args)
    rng = np.random.default_rng(0)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, args.context)))
    days, d = [], date(2024, 1, 1)
    while len(days) < args.context:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    window = ContextWindow.from_closes("SYNTH", days, closes)
    t0 = time.monotonic()
    fc = forecaster.sample_paths(window, args.horizon, args.paths, args.seed)
    r = read_cone(fc)
    print(
        f"kronos-check OK: {fc.paths.shape[0]} paths x {fc.horizon} bars in "
        f"{time.monotonic() - t0:.1f}s; {r.paths_up}/{r.n_paths} up, spread {r.spread_pct:.1%}"
    )
    distinct = len({tuple(np.round(p, 6)) for p in fc.paths})
    if distinct < fc.paths.shape[0]:
        print(f"WARNING: only {distinct} distinct paths; sampling may be collapsing to the mean")
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="signalforge.forecast_cli", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trial", help="score forecasters walk-forward on train/validation")
    _add_common(t)
    t.add_argument("--forecaster", default="bootstrap,random-walk",
                   help=f"comma-separated: {', '.join(FORECASTERS)}")
    t.add_argument("--symbols", required=True)
    t.add_argument("--window", default="validation")
    t.add_argument("--start", default=None)
    t.add_argument("--end", default=None)
    t.add_argument("--stride", type=int, default=None, help="default: the horizon (no overlap)")
    t.add_argument("--out", default=str(DEFAULT_OUT))
    t.add_argument("--dashboard-out", default=str(DEFAULT_PAGE),
                   help="where to write the Kronos Trial dashboard tab")
    t.add_argument("--no-dashboard", action="store_true")
    t.add_argument("--backtest", action="store_true",
                   help="also run the TRENDING-only rule against buy-and-hold in the engine")
    t.add_argument("--report", action="store_true",
                   help="send the scores to the Cloudflare dashboard's Crucible tab (no prices)")
    t.add_argument("--report-dry-run", action="store_true",
                   help="build and validate the Crucible report, print it, send nothing")
    c = sub.add_parser("cone", help="forward cone from the latest bar")
    _add_common(c)
    c.add_argument("--symbol", required=True)
    c.add_argument("--forecaster", default="bootstrap")
    k = sub.add_parser("kronos-check", help="load local Kronos weights and sample once")
    _add_common(k)
    args = parser.parse_args(argv)
    try:
        return {"trial": cmd_trial, "cone": cmd_cone, "kronos-check": cmd_kronos_check}[args.cmd](args)
    except SignalForgeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
