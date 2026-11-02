"""Broad-universe plumbing in pipeline.py: specs, gates, coverage and lockout notes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from signalforge.config.models import (
    ConfigBundle,
    DataSourcesConfig,
    EligibilityConfig,
    ResearchConfig,
    RiskConfig,
    SplitConfig,
)
from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import RiskAction, RiskEvent
from signalforge.data.storage import Warehouse
from signalforge.pipeline import (
    RISKOFF_GATES,
    breaker_lockout_note,
    broad_specs,
    coverage_notes,
    load_gate,
    load_riskoff_gates,
    ohlcv_history,
    overlay_specs,
    run_window,
)
from signalforge.strategies.regime import RiskOffGate
from tests.conftest import business_days, make_bar
from tests.test_ohlcv_research import DAYS, research, seed_universe

D = Decimal
ELIG = EligibilityConfig(
    min_history_sessions=252, min_median_dollar_volume=D("500000"), dollar_volume_window=63
)


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "wh.duckdb"))
    yield w
    w.close()


def put_series(warehouse, series_id, rows):
    warehouse.upsert_series(
        [
            {"series_id": series_id, "observation_date": d, "value": v,
             "source": "fred", "retrieved_at": None}
            for d, v in rows
        ]
    )


def test_broad_specs_shape():
    specs = broad_specs(("AAA", "BBB"), "SPY", ELIG, 20, 5)
    assert [s[1] for s in specs] == ["ma_trend", "xs_momentum", "buy_and_hold"]
    _, _, trend = specs[0]
    assert trend["fast_window"] == 50 and trend["slow_window"] == 200
    assert trend["target_weight"] == D("0.05") and trend["max_names"] == 20
    assert trend["symbols"] == ("AAA", "BBB")
    assert trend["eligibility"] == {
        "min_history_sessions": 252,
        "min_median_dollar_volume": D("500000"),
        "dollar_volume_window": 63,
        "max_stale_days": 5,
    }
    _, _, mom = specs[1]
    assert (mom["lookback_days"], mom["skip_days"], mom["top_n"]) == (126, 21, 20)
    assert mom["target_weight_total"] == D("1.0") and mom["min_history_days"] == 150
    assert mom["symbols"] == ("AAA", "BBB") and mom["eligibility"] == trend["eligibility"]
    _, _, hold = specs[2]
    assert hold == {"symbols": ("SPY",), "target_weight": D("0.05")}


def test_overlay_specs_two_gates_two_strategies():
    gates = [
        RiskOffGate("vix30", "VIXCLS", ">", D("30"), 1, ()),
        RiskOffGate("curve0", "T10Y2Y", "<", D("0"), 2, ()),
    ]
    base = broad_specs(("AAA",), "SPY", ELIG, 20, 5)
    overlays = overlay_specs(base, gates)
    assert len(overlays) == 4
    assert sorted((s[1], s[2]["riskoff_gate"].name) for s in overlays) == [
        ("ma_trend", "curve0"), ("ma_trend", "vix30"),
        ("xs_momentum", "curve0"), ("xs_momentum", "vix30"),
    ]
    assert all("riskoff" in s[0] for s in overlays)
    assert all("riskoff_gate" not in s[2] for s in base)


def test_riskoff_gate_definitions_are_the_two_decided_ones():
    assert [tuple(g) for g in RISKOFF_GATES] == [
        ("vix30", "VIXCLS", ">", D("30"), 1),
        ("curve0", "T10Y2Y", "<", D("0"), 2),
    ]


def test_load_gate_missing_series_raises_named_error(warehouse):
    with pytest.raises(ValidationError, match="T10Y2Y"):
        load_gate(warehouse, "curve0", "T10Y2Y", "<", D("0"), 2)


def test_load_riskoff_gates_reads_both_series(warehouse):
    put_series(warehouse, "VIXCLS", [(DAYS[0], D("20")), (DAYS[1], D("35"))])
    put_series(warehouse, "T10Y2Y", [(DAYS[0], D("1.2")), (DAYS[1], D("-0.3"))])
    gates = load_riskoff_gates(warehouse)
    assert [g.name for g in gates] == ["vix30", "curve0"]
    assert gates[0].series_id == "VIXCLS" and gates[1].lag_sessions == 2


def test_run_window_records_instance_strategy_id(warehouse):
    seed_universe(warehouse)
    bundle = ohlcv_history(warehouse, ["AAA", "BBB", "SPY"], sources=["fixture"], price_basis="adjusted")
    gate = RiskOffGate("vix30", "VIXCLS", ">", D("30"), 1, tuple((d, D("15")) for d in DAYS))
    specs = [
        ("MA trend + riskoff", "ma_trend",
         {"fast_window": 10, "slow_window": 30, "target_weight": D("0.05"),
          "symbols": ("AAA", "BBB"), "riskoff_gate": gate}),
        ("Buy and hold", "buy_and_hold", {"symbols": ("SPY",), "target_weight": D("0.05")}),
    ]
    config = ConfigBundle(research=research(), risk=RiskConfig(), data_sources=DataSourcesConfig(providers={}))
    run = run_window(bundle, config, "train", specs=specs)
    assert [o.strategy_id for o in run.outcomes] == ["ma_trend_riskoff_vix30", "buy_and_hold"]
    assert run.outcomes[0].result.strategy_id == "ma_trend_riskoff_vix30"
    assert run.by_id("ma_trend_riskoff_vix30") is run.outcomes[0]


def test_coverage_notes_summarise_long_lists():
    days = business_days(date(2010, 1, 4), 40)
    bars = [make_bar("SPY", d, D(100)) for d in days]
    for i in range(400):
        for d in days[10:12]:
            bars.append(make_bar(f"LATE{i:03d}", d, D(50)))
    history = PriceHistory.from_bars(bars)
    splits = SplitConfig(
        train_start=date(2009, 1, 1), train_end=date(2009, 12, 31),
        validation_start=date(2010, 1, 1), validation_end=date(2010, 12, 31),
        test_start=date(2011, 1, 1), test_end=date(2011, 12, 31),
    )
    full = coverage_notes(history, splits, "SPY")
    short = coverage_notes(history, splits, "SPY", summarise=True)
    assert "LATE399" in full[1]
    validation = short[1]
    assert len(validation.encode()) < 1024
    assert "400" in validation and "LATE399" not in validation
    assert coverage_notes(history, splits, "SPY", summarise=False) == full


def event(day, rule="breaker_drawdown"):
    return RiskEvent(event_date=day, rule=rule, action=RiskAction.REJECT, symbol="AAA", reason="x")


def test_breaker_lockout_note_counts_sessions():
    a, b, c = date(2020, 3, 2), date(2020, 3, 3), date(2020, 3, 9)
    events = [event(a), event(a), event(b), event(c), event(a, "breaker_daily_loss")]
    note = breaker_lockout_note(events)
    assert "3 session" in note and "2020-03-02" in note and "2020-03-09" in note
    assert breaker_lockout_note([event(a, "breaker_daily_loss")]) is None
    assert breaker_lockout_note([]) is None


def test_run_window_appends_breaker_lockout_note_per_outcome(warehouse):
    # Rises, crashes hard (drawdown breaker trips), recovers into a fresh ENTER.
    closes = [100 + i for i in range(80)] + [180 - 6 * i for i in range(1, 16)] + [
        90 + 2 * i for i in range(1, 300)
    ]
    days = DAYS[: len(closes)]
    warehouse.upsert_bars(
        [make_bar("AAA", d, D(c), volume=5_000_000, adj_close=D(c)) for d, c in zip(days, closes)]
        + [make_bar("SPY", d, D(100), volume=5_000_000, adj_close=D(100)) for d in days]
    )
    bundle = ohlcv_history(warehouse, ["AAA", "SPY"], sources=["fixture"], price_basis="adjusted")
    step = days[-1] - days[-2]
    splits = SplitConfig(
        train_start=days[0], train_end=days[-3], validation_start=days[-2],
        validation_end=days[-1], test_start=days[-1] + step, test_end=days[-1] + 2 * step,
    )
    config = ConfigBundle(
        research=ResearchConfig(starting_cash=D("100000"), splits=splits),
        risk=RiskConfig(max_single_name_exposure=D("0.5")),
        data_sources=DataSourcesConfig(providers={}),
    )
    specs = [("MA", "ma_trend", {"fast_window": 10, "slow_window": 30,
                                  "target_weight": D("0.5"), "symbols": ("AAA",)})]
    run = run_window(bundle, config, "train", specs=specs)
    assert any(n.startswith("MA: BREAKER LOCKOUT") for n in run.limitations)
