"""dataset_kind and strategy ids for the broad universe and the risk-off overlays."""

from __future__ import annotations

import json
from decimal import Decimal

from signalforge.config.models import EligibilityConfig
from signalforge.pipeline import broad_specs, overlay_specs
from signalforge.reports import events as ev
from signalforge.research_cli import _stringify
from signalforge.strategies.regime import RiskOffGate
from signalforge.strategies.registry import build
from tests.test_report_events import backtest

D = Decimal
LEGACY = (
    "211979 OHLCV bars for 21 symbols from tiingo, 1972-01-25 to 2026-10-28, "
    "adjusted price basis"
)


def test_dataset_kind_universe_suffix():
    broad = "300000 OHLCV bars for 470 symbols from tiingo, 1992-01-02 to 2014-12-31, adjusted price basis, universe broad"
    assert ev.dataset_kind(broad) == "ohlcv-broad-tiingo"


def test_dataset_kind_legacy_description_unchanged():
    assert ev.dataset_kind(LEGACY) == "ohlcv21-tiingo"
    assert ev.dataset_kind("NASDAQCOM daily close from fred, 100 observations 2000-01-01 to 2000-06-01") == "nasdaqcom"
    assert ev.dataset_kind("garbage") is None


def test_broad_overlay_strategy_ids_distinct_and_valid_slugs():
    elig = EligibilityConfig(min_history_sessions=252, min_median_dollar_volume=D("500000"), dollar_volume_window=63)
    gates = [
        RiskOffGate("vix30", "VIXCLS", ">", D("30"), 1, ()),
        RiskOffGate("curve0", "T10Y2Y", "<", D("0"), 2, ()),
    ]
    base = broad_specs(("AAA", "BBB"), "SPY", elig, 20, 5)
    specs = base + overlay_specs(base, gates)
    kind = ev.dataset_kind(
        "1 OHLCV bars for 470 symbols from tiingo, 1992-01-02 to 2014-12-31, adjusted price basis, universe broad"
    )
    ids = []
    for _, strategy_id, params in specs:
        strategy = build(strategy_id, **params)
        parameters = json.dumps(_stringify(strategy.parameters()))
        sid = ev._strategy_id(kind, strategy.strategy_id, parameters)
        assert len(sid) <= 64
        ev.validate_event(backtest(strategy_id=sid, baseline_strategy_id=f"{kind}.buy_and_hold"))
        ids.append(sid)
    assert len(specs) == 7
    assert len(set(ids)) == len(ids)
