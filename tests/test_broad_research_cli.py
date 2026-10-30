"""research_cli with --universe-file and --overlays: windows, refusals, notes."""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from signalforge.config.models import EligibilityConfig, UniverseConfig
from signalforge.data.storage import Warehouse
from signalforge.pipeline import SURVIVORSHIP_NOTE
from signalforge.reports.events import dataset_kind
from signalforge.research_cli import (
    BROAD_UNIVERSE_NOTE,
    ELIGIBILITY_NOTE,
    format_table,
    main,
    parse_args,
    run_research,
)
from tests.test_ohlcv_research import DAYS, research, seed_universe

D = Decimal


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "wh.duckdb"))
    seed_universe(w)
    yield w
    w.close()


def universe(**over) -> UniverseConfig:
    payload = {
        "name": "broad",
        "source": "fixture",
        "benchmark": "SPY",
        "symbols": ("AAA", "BBB", "CCC"),
        "excluded": {"ZZZ": "unverified"},
        "load_start": DAYS[0],
        "train_start": DAYS[60],
        "eligibility": EligibilityConfig(
            min_history_sessions=50, min_median_dollar_volume=D("1000"), dollar_volume_window=20
        ),
        "max_names": 2,
        **over,
    }
    return UniverseConfig(**payload)


def put_gate_series(w):
    rows = []
    for series_id, calm, spike in (("VIXCLS", D("15"), D("40")), ("T10Y2Y", D("1"), D("-1"))):
        for i, d in enumerate(DAYS):
            value = spike if 300 <= i < 320 else calm
            rows.append({"series_id": series_id, "observation_date": d, "value": value,
                         "source": "fred", "retrieved_at": None})
    w.upsert_series(rows)


def experiments(w):
    return w.con.execute(
        "SELECT strategy_name, dataset_description, evaluated_window, train_start FROM experiments"
    ).fetchall()


def test_universe_file_run_records_ohlcv_broad_kind(warehouse):
    output = run_research(warehouse, research(), universe=universe())
    rows = experiments(warehouse)
    assert rows
    assert {dataset_kind(r[1]) for r in rows} == {"ohlcv-broad-fixture"}
    assert all(r[1].endswith(", universe broad") for r in rows)
    assert {r[2] for r in rows} == {"train", "validation"}
    assert {r[3] for r in rows} == {DAYS[60]}
    assert [o.strategy_id for o in output.runs[0].outcomes] == [
        "ma_trend", "ma_trend_nobreaker", "xs_momentum", "xs_momentum_nobreaker", "buy_and_hold",
    ]


def test_universe_file_run_never_loads_bars_after_validation_end(warehouse, monkeypatch):
    seen = []
    original = warehouse.load_bars

    def spy(*args, **kwargs):
        bars = original(*args, **kwargs)
        seen.extend(b.trade_date for b in bars)
        return bars

    monkeypatch.setattr(warehouse, "load_bars", spy)
    output = run_research(warehouse, research(), universe=universe())
    assert seen and max(seen) <= DAYS[700]
    assert {r.window for r in output.runs} == {"train", "validation"}


def test_universe_file_with_run_holdout_refused_exit_2(tmp_path, capsys):
    db = tmp_path / "x.duckdb"
    Warehouse(str(db)).close()
    path = tmp_path / "u.yaml"
    path.write_text(yaml.safe_dump({
        "name": "broad", "source": "tiingo", "benchmark": "SPY", "symbols": ["AAA"],
        "load_start": "1992-01-02", "train_start": "1993-02-01",
        "eligibility": {"min_history_sessions": 252, "min_median_dollar_volume": "500000",
                        "dollar_volume_window": 63},
        "max_names": 20,
    }), encoding="utf-8")
    code = main(["--db", str(db), "--universe-file", str(path), "--run-holdout", "--no-dashboard"])
    assert code == 2
    assert "run-holdout" in capsys.readouterr().out


def test_overlays_with_run_holdout_refused(tmp_path, capsys):
    db = tmp_path / "x.duckdb"
    Warehouse(str(db)).close()
    assert main(["--db", str(db), "--universe", "--overlays", "--run-holdout", "--no-dashboard"]) == 2
    assert main(["--db", str(db), "--overlays", "--run-holdout", "--no-dashboard"]) == 2
    assert "run-holdout" in capsys.readouterr().out


def test_overlays_need_an_ohlcv_run(tmp_path, capsys):
    db = tmp_path / "x.duckdb"
    Warehouse(str(db)).close()
    assert main(["--db", str(db), "--overlays", "--no-dashboard"]) == 2
    assert "overlays" in capsys.readouterr().out


def test_parse_args_accepts_universe_file_and_overlays(tmp_path):
    args = parse_args(["--universe-file", str(tmp_path / "u.yaml"), "--overlays"])
    assert args.universe_file == tmp_path / "u.yaml" and args.overlays is True
    assert parse_args([]).universe_file is None and parse_args([]).overlays is False
    with pytest.raises(SystemExit):
        parse_args(["--universe", "--universe-file", "u.yaml"])


def test_universe_file_does_not_touch_research_universe(warehouse):
    config = research()
    before = config.model_dump()
    output = run_research(warehouse, config, universe=universe())
    assert config.model_dump() == before
    assert config.universe == ("AAA",)
    traded = {s for o in output.runs[0].outcomes[:2] for s in o.result.parameters["symbols"]}
    assert traded == {"AAA", "BBB", "CCC"}


def test_broad_limitations_carry_survivorship_note(warehouse):
    output = run_research(warehouse, research(), universe=universe())
    for run in output.runs:
        assert SURVIVORSHIP_NOTE in run.limitations
        assert any(n.startswith("BROAD UNIVERSE") for n in run.limitations)
        assert any(n.startswith("ELIGIBILITY") for n in run.limitations)
    broad = next(n for n in output.runs[0].limitations if n.startswith("BROAD UNIVERSE"))
    assert DAYS[60].isoformat() in broad and DAYS[500].isoformat() in broad
    assert "never loaded" in broad
    assert BROAD_UNIVERSE_NOTE.startswith("BROAD UNIVERSE")
    assert ELIGIBILITY_NOTE.startswith("ELIGIBILITY")


def test_overlays_add_four_specs_and_regime_notes(warehouse):
    put_gate_series(warehouse)
    output = run_research(warehouse, research(), universe=universe(), overlays=True)
    ids = [o.strategy_id for o in output.runs[0].outcomes]
    assert ids[:5] == [
        "ma_trend", "ma_trend_nobreaker", "xs_momentum", "xs_momentum_nobreaker", "buy_and_hold",
    ]
    overlay_ids = [
        "ma_trend_riskoff_vix30", "xs_momentum_riskoff_vix30",
        "ma_trend_riskoff_curve0", "xs_momentum_riskoff_curve0",
    ]
    assert sorted(ids[5:]) == sorted(overlay_ids + [i + "_nobreaker" for i in overlay_ids])
    for run in output.runs:
        vix = next(n for n in run.limitations if n.startswith("REGIME GATE vix30"))
        assert "risk-off" in vix and "unknown" in vix and run.window in vix
    train_vix = next(n for n in output.run_for("train").limitations if n.startswith("REGIME GATE vix30"))
    assert "risk-off 20," in train_vix
    names = {r[0] for r in experiments(warehouse)}
    assert "ma_trend_riskoff_curve0" in names


def test_overlays_work_on_the_plain_symbols_run(warehouse):
    put_gate_series(warehouse)
    output = run_research(
        warehouse, research(), symbols=["AAA", "BBB", "CCC"], source="fixture", overlays=True
    )
    ids = {o.strategy_id for o in output.runs[0].outcomes}
    assert {"ma_trend_riskoff_vix30", "xs_momentum_riskoff_curve0"} <= ids
    assert len(ids) == 9 + 8


def test_overlays_without_the_series_name_the_missing_one(warehouse):
    from signalforge.core.errors import ValidationError

    with pytest.raises(ValidationError, match="VIXCLS"):
        run_research(warehouse, research(), universe=universe(), overlays=True)


def test_format_table_widens_for_long_labels(warehouse):
    put_gate_series(warehouse)
    output = run_research(warehouse, research(), universe=universe(), overlays=True)
    lines = format_table(output).splitlines()
    rows = [ln for ln in lines if ln and not ln.startswith(("strategy", "==="))]
    assert any(" + riskoff " in ln for ln in rows)
    starts = {len(ln) - len(ln.rsplit("  ", 1)[1]) for ln in rows}
    assert len(starts) == 1
