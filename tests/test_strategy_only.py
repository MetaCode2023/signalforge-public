"""Every research strategy is reported twice: as traded, and strategy only.

"Strategy only" is the same strategy with just the portfolio drawdown breaker
off. It exists because that breaker never resets, so after one deep drawdown it
turns the rest of a run into cash and the table stops measuring the strategy.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import (
    ConfigBundle,
    DataSourcesConfig,
    EligibilityConfig,
    ResearchConfig,
    RiskConfig,
    SplitConfig,
    load_risk,
)
from signalforge.data.storage import Warehouse
from signalforge.pipeline import broad_specs, ohlcv_history, overlay_specs, run_window, strategy_only_risk
from signalforge.reports import events as ev
from signalforge.research_cli import _stringify, run_research
from signalforge.risk import breakers
from signalforge.strategies.regime import RiskOffGate
from signalforge.strategies.registry import build
from tests.conftest import make_bar
from tests.test_ohlcv_research import DAYS, research, seed_universe
from tests.test_report_events import backtest

D = Decimal
PACKAGE = Path(__file__).resolve().parents[1] / "signalforge"


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "wh.duckdb"))
    yield w
    w.close()


def crash_book(warehouse):
    """AAA rises, crashes hard (drawdown breaker trips), then recovers."""
    closes = [100 + i for i in range(80)] + [180 - 6 * i for i in range(1, 16)] + [
        90 + 2 * i for i in range(1, 300)
    ]
    days = DAYS[: len(closes)]
    warehouse.upsert_bars(
        [make_bar("AAA", d, D(c), volume=5_000_000, adj_close=D(c)) for d, c in zip(days, closes)]
        + [make_bar("BBB", d, D(100 + i // 3), volume=5_000_000, adj_close=D(100 + i // 3))
           for i, d in enumerate(days)]
        + [make_bar("SPY", d, D(100), volume=5_000_000, adj_close=D(100)) for d in days]
    )
    step = days[-1] - days[-2]
    splits = SplitConfig(
        train_start=days[0], train_end=days[-3], validation_start=days[-2],
        validation_end=days[-1], test_start=days[-1] + step, test_end=days[-1] + 2 * step,
    )
    return ohlcv_history(warehouse, ["AAA", "BBB", "SPY"], sources=["fixture"],
                         price_basis="adjusted"), splits


def config(splits, **risk):
    return ConfigBundle(
        research=ResearchConfig(starting_cash=D("100000"), splits=splits),
        risk=RiskConfig(max_single_name_exposure=D("0.5"), **risk),
        data_sources=DataSourcesConfig(providers={}),
    )


def spec(symbols=("AAA",)):
    return [
        ("MA", "ma_trend", {"fast_window": 10, "slow_window": 30,
                            "target_weight": D("0.5"), "symbols": symbols}),
        ("Hold", "buy_and_hold", {"symbols": ("SPY",), "target_weight": D("0.05")}),
    ]


def test_strategy_only_risk_changes_only_the_drawdown_breaker():
    base = RiskConfig()
    free = strategy_only_risk(base)
    changed = {k for k, v in free.model_dump().items() if v != base.model_dump()[k]}
    assert changed == {"max_portfolio_drawdown"}
    assert base.max_portfolio_drawdown == D("0.10")
    assert load_risk().max_portfolio_drawdown == D("0.10")
    assert RiskConfig().max_portfolio_drawdown == D("0.10")


def test_strategy_only_keeps_every_other_breaker_live():
    free = strategy_only_risk(RiskConfig())
    heavy_loss = SimpleNamespace(
        daily_loss_fraction=D("0.03"), drawdown_fraction=D("0.5"), peak_equity=D("100"),
        current_equity=D("50"), day_start_equity=D("100"),
    )
    assert breakers.check_drawdown(heavy_loss, free) is None
    assert breakers.check_drawdown(heavy_loss, RiskConfig()) is not None
    assert breakers.check_daily_loss(heavy_loss, free)[0] == "breaker_daily_loss"
    assert free.max_positions == 20 and free.max_single_name_exposure == D("0.05")
    assert BacktestEngine(research(), free).risk_config.max_daily_loss == D("0.02")


def test_default_run_window_has_no_strategy_only_rows(warehouse):
    bundle, splits = crash_book(warehouse)
    run = run_window(bundle, config(splits), "train", specs=spec())
    assert [o.strategy_id for o in run.outcomes] == ["ma_trend", "buy_and_hold"]


def test_as_traded_outcome_is_unchanged_by_the_second_row(warehouse):
    bundle, splits = crash_book(warehouse)
    plain = run_window(bundle, config(splits), "train", specs=spec())
    both = run_window(bundle, config(splits), "train", specs=spec(), strategy_only=True)
    a, b = plain.by_id("ma_trend"), both.by_id("ma_trend")
    assert a.result.equity_curve == b.result.equity_curve
    assert a.result.fills == b.result.fills
    assert a.metrics.to_dict() == b.metrics.to_dict()
    assert a.label == b.label == "MA"


def test_strategy_only_never_emits_the_drawdown_breaker(warehouse):
    bundle, splits = crash_book(warehouse)
    run = run_window(bundle, config(splits), "train", specs=spec(), strategy_only=True)
    as_traded, free = run.by_id("ma_trend"), run.by_id("ma_trend_nobreaker")
    assert any(e.rule == "breaker_drawdown" for e in as_traded.result.risk_events)
    assert not any(e.rule == "breaker_drawdown" for e in free.result.risk_events)
    assert len(free.result.fills) > len(as_traded.result.fills)
    assert free.label == "MA (strategy only)"
    assert free.result.parameters["risk_drawdown_breaker"] == "off"
    assert "risk_drawdown_breaker" not in as_traded.result.parameters
    assert any(n.startswith("MA: BREAKER LOCKOUT") for n in run.limitations)
    assert not any("(strategy only)" in n and "BREAKER LOCKOUT" in n for n in run.limitations)


def test_each_strategy_is_followed_by_its_strategy_only_row_and_benchmark_is_not_doubled(warehouse):
    bundle, splits = crash_book(warehouse)
    run = run_window(bundle, config(splits), "train", specs=spec(), strategy_only=True)
    assert [o.strategy_id for o in run.outcomes] == ["ma_trend", "ma_trend_nobreaker", "buy_and_hold"]


def test_strategy_only_still_respects_the_position_cap(warehouse):
    bundle, splits = crash_book(warehouse)
    run = run_window(
        bundle, config(splits, max_positions=1), "train", specs=spec(("AAA", "BBB")),
        strategy_only=True,
    )
    free = run.by_id("ma_trend_nobreaker")
    assert max(p.position_count for p in free.result.equity_curve) == 1
    assert any(e.rule == "max_positions" for e in free.result.risk_events)


def test_strategy_only_ids_are_distinct_valid_slugs():
    elig = EligibilityConfig(min_history_sessions=252, min_median_dollar_volume=D("500000"),
                             dollar_volume_window=63)
    gates = [RiskOffGate("vix30", "VIXCLS", ">", D("30"), 1, ()),
             RiskOffGate("curve0", "T10Y2Y", "<", D("0"), 2, ())]
    base = broad_specs(("AAA", "BBB"), "SPY", elig, 20, 5)
    specs = base + overlay_specs(base, gates)
    kind = ev.dataset_kind(
        "1 OHLCV bars for 470 symbols from tiingo, 1992-01-02 to 2014-12-31, "
        "adjusted price basis, universe broad"
    )
    from signalforge.pipeline import StrategyOnly

    ids = []
    for _, strategy_id, params in specs:
        for free in (False, True):
            if free and strategy_id == "buy_and_hold":
                continue
            strategy = build(strategy_id, **params)
            if free:
                strategy = StrategyOnly(strategy)
            parameters = json_dumps(_stringify(strategy.parameters()))
            sid = ev._strategy_id(kind, strategy.strategy_id, parameters)
            assert len(sid) <= 64
            ev.validate_event(backtest(strategy_id=sid, baseline_strategy_id=f"{kind}.buy_and_hold"))
            ids.append(sid)
    assert len(ids) == 7 + 6
    assert len(set(ids)) == len(ids)
    assert any(i.endswith("_nobreaker") or "nobreaker" in i for i in ids)


def json_dumps(value):
    import json

    return json.dumps(value)


def test_run_research_reports_both_rows_and_records_distinct_experiments(warehouse):
    seed_universe(warehouse, source="fixture")
    output = run_research(warehouse, research(), symbols=["AAA", "BBB", "CCC"], source="fixture")
    for run in output.runs:
        ids = [o.strategy_id for o in run.outcomes]
        assert ids.count("buy_and_hold") == 1
        for base in ("ma_trend", "xs_momentum", "zscore_mean_reversion", "vol_targeted_trend"):
            assert base in ids and f"{base}_nobreaker" in ids
    names = [r[0] for r in warehouse.con.execute("SELECT strategy_name FROM experiments").fetchall()]
    assert names.count("ma_trend_nobreaker") == 2 and names.count("ma_trend") == 2
    assert "ma_trend_nobreaker" in names and "ma_trend" in names


def test_series_run_with_holdout_never_runs_strategy_only_on_the_test_window(tmp_path):
    from tests.test_research_cli import DAYS as SERIES_DAYS

    w = Warehouse(str(tmp_path / "s.duckdb"))
    try:
        w.upsert_series([
            {"series_id": "NASDAQCOM", "observation_date": d, "value": D(1000) + D(i) / 2,
             "source": "fred", "retrieved_at": None}
            for i, d in enumerate(SERIES_DAYS)
        ])
        from tests.test_research_cli import _config

        output = run_research(w, _config(), run_holdout=True)
        by_window = {r.window: [o.strategy_id for o in r.outcomes] for r in output.runs}
        assert "ma_trend_nobreaker" in by_window["train"]
        assert "ma_trend_nobreaker" in by_window["validation"]
        assert not any(i.endswith("_nobreaker") for i in by_window["test"])
    finally:
        w.close()


def test_paper_code_cannot_reach_the_breaker_off_variant():
    # paper_cli legitimately imports other pipeline helpers, so it is held to
    # the names; the paper package itself may not touch pipeline at all.
    paper_files = sorted((PACKAGE / "paper").rglob("*.py")) + [PACKAGE / "paper_cli.py"]
    names = {"strategy_only_risk", "StrategyOnly", "research_cli"}
    offenders = {}
    for path in paper_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = set()
        forbidden_names = names if path.name == "paper_cli.py" else names | {"pipeline"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                hits |= {a.name for a in node.names} & forbidden_names
                hits |= set((node.module or "").split(".")) & forbidden_names
            elif isinstance(node, ast.Import):
                for a in node.names:
                    hits |= set(a.name.split(".")) & forbidden_names
            elif isinstance(node, ast.Name) and node.id in forbidden_names:
                hits.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                hits.add(node.attr)
        if hits:
            offenders[path.name] = sorted(hits)
    assert offenders == {}
    assert load_risk().max_portfolio_drawdown == D("0.10")
