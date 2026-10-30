"""Risk-off gates and the overlay that flattens the book when one is on."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ResearchConfig, RiskConfig
from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import SignalDirection
from signalforge.strategies.regime import RegimeOverlay, RiskOffGate
from signalforge.strategies.trend import MovingAverageTrend
from tests.conftest import business_days, make_bar

D = Decimal
ENTER, EXIT = SignalDirection.ENTER_LONG, SignalDirection.EXIT_LONG
DAYS = business_days(date(2020, 1, 1), 200)


def history(symbol="UP", n=200):
    return PriceHistory.from_bars([make_bar(symbol, d, D(100 + i)) for i, d in enumerate(DAYS[:n])])


def vix_gate(observations, lag=1):
    return RiskOffGate("vix30", "VIXCLS", ">", D("30"), lag, tuple(observations))


def curve_gate(observations, lag=2):
    return RiskOffGate("curve0", "T10Y2Y", "<", D("0"), lag, tuple(observations))


def flat(value, upto=200):
    return [(d, D(value)) for d in DAYS[:upto]]


def state_on(gate, hist, day):
    return gate.state(hist.view(day))


def test_gate_uses_observation_strictly_before_as_of():
    obs = flat(15)
    obs[10] = (DAYS[10], D("35"))
    gate, hist = vix_gate(obs), history()
    assert state_on(gate, hist, DAYS[10]) == "risk_on"
    assert state_on(gate, hist, DAYS[11]) == "risk_off"
    assert state_on(gate, hist, DAYS[12]) == "risk_on"


def test_gate_lag_two_sessions_skips_t_minus_1():
    obs = [(d, D("1")) for d in DAYS]
    obs[10] = (DAYS[10], D("-0.5"))
    gate, hist = curve_gate(obs), history()
    assert state_on(gate, hist, DAYS[11]) == "risk_on"
    assert state_on(gate, hist, DAYS[12]) == "risk_off"
    assert state_on(gate, hist, DAYS[13]) == "risk_on"


def test_gate_skips_null_observations_to_prior_value():
    obs = flat(15)
    obs[8] = (DAYS[8], D("35"))
    obs[9] = (DAYS[9], None)
    gate, hist = vix_gate(obs), history()
    assert state_on(gate, hist, DAYS[10]) == "risk_off"


def test_gate_unknown_when_stale_over_10_days_is_risk_on_and_says_so():
    obs = [(DAYS[0], D("45"))]
    gate, hist = vix_gate(obs), history()
    assert state_on(gate, hist, DAYS[3]) == "risk_off"
    assert state_on(gate, hist, DAYS[30]) == "unknown"
    assert state_on(gate, hist, DAYS[0]) == "unknown"
    counts = gate.count_states(hist.trading_days, DAYS[25], DAYS[35])
    assert counts == {"risk_off": 0, "risk_on": 0, "unknown": 11}
    overlay = RegimeOverlay(MovingAverageTrend(5, 20, D("0.05"), ("UP",)), gate)
    sigs = overlay.generate_signals(DAYS[30], hist.view(DAYS[30]))
    assert sigs and all(s.direction is ENTER for s in sigs)


def test_gate_refuses_duplicate_observation_dates():
    with pytest.raises(ValidationError, match="duplicate"):
        vix_gate([(DAYS[0], D("15")), (DAYS[0], D("16"))])


def test_gate_rejects_bad_rule_and_lag():
    with pytest.raises(ValidationError):
        RiskOffGate("g", "S", "==", D("1"), 1, ())
    with pytest.raises(ValidationError):
        RiskOffGate("g", "S", ">", D("1"), 0, ())


def test_gate_risk_off_session_count():
    obs = flat(15)
    for i in range(20, 25):
        obs[i] = (DAYS[i], D("40"))
    gate, hist = vix_gate(obs), history()
    counts = gate.count_states(hist.trading_days, DAYS[0], DAYS[199])
    assert counts["risk_off"] == 5
    assert counts["unknown"] == 1  # the first session has no prior reference
    assert sum(counts.values()) == 200
    assert gate.count_states(hist.trading_days, DAYS[22], DAYS[23]) == {
        "risk_off": 2, "risk_on": 0, "unknown": 0,
    }


def _overlay(obs, **kw):
    return RegimeOverlay(MovingAverageTrend(5, 20, D("0.05"), ("UP",)), vix_gate(obs, **kw))


def test_overlay_risk_off_turns_every_enter_into_exit():
    hist, day = history(), DAYS[100]
    obs = flat(40)
    sigs = _overlay(obs).generate_signals(day, hist.view(day))
    assert sigs and all(s.direction is EXIT and s.target_weight == 0 for s in sigs)
    assert all("risk-off" in s.reason and "vix30" in s.reason for s in sigs)


def test_overlay_risk_on_passes_signals_through_unchanged():
    hist, day = history(), DAYS[100]
    inner = MovingAverageTrend(5, 20, D("0.05"), ("UP",))
    assert _overlay(flat(15)).generate_signals(day, hist.view(day)) == inner.generate_signals(
        day, hist.view(day)
    )


def test_overlay_strategy_id_and_parameters():
    inner = MovingAverageTrend(5, 20, D("0.05"), ("UP",))
    overlay = _overlay(flat(15))
    assert overlay.strategy_id == "ma_trend_riskoff_vix30"
    assert overlay.warmup_days == inner.warmup_days
    assert overlay.parameters() == {
        **inner.parameters(),
        "riskoff_series": "VIXCLS",
        "riskoff_rule": ">",
        "riskoff_threshold": D("30"),
        "riskoff_lag_sessions": 1,
    }


def test_overlay_end_to_end_flattens_after_lag():
    hist = history()
    obs = flat(15)
    spike = 100
    for i in range(spike, spike + 5):
        obs[i] = (DAYS[i], D("40"))
    overlay = _overlay(obs)
    result = BacktestEngine(ResearchConfig(universe=("UP",)), RiskConfig()).run(overlay, hist)
    held = {p.valuation_date: p.position_count for p in result.equity_curve}
    assert held[DAYS[spike]] == 1
    assert held[DAYS[spike + 1]] == 1
    assert held[DAYS[spike + 2]] == 0
    assert held[DAYS[spike + 6]] == 0
    assert held[DAYS[spike + 8]] == 1
    assert result.strategy_id == "ma_trend_riskoff_vix30"
