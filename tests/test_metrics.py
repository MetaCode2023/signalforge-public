"""Performance metrics: correctness against independent calculations, and honesty."""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import date
from decimal import Decimal

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from signalforge.backtesting.metrics import (
    EvidenceLevel,
    PerformanceMetrics,
    compute_metrics,
    drawdown_series,
    regime_labels_from_drawdown,
)
from signalforge.config.models import ResearchConfig
from signalforge.core.errors import ValidationError
from signalforge.core.interfaces import BacktestResult, EquityPoint
from signalforge.core.types import Fill, Side
from tests.conftest import business_days

D = Decimal
START = date(2020, 1, 2)


def curve(equities, positions=None, start=START) -> list[EquityPoint]:
    days = business_days(start, len(equities))
    positions = positions if positions is not None else [1] * len(equities)
    return [
        EquityPoint(
            valuation_date=d, equity=D(str(e)), cash=D(0), gross_exposure=D(0),
            position_count=p, realized_pnl=D(0), unrealized_pnl=D(0),
            drawdown=D(0), peak_equity=D(str(e)),
        )
        for d, e, p in zip(days, equities, positions)
    ]


def fill(order_id, side, qty, price, fees="0", slip="0", day=START, fill_id=""):
    return Fill(
        order_id=order_id, symbol="X", side=side, quantity=qty, price=D(str(price)),
        fees=D(str(fees)), slippage_cost=D(str(slip)), fill_date=day, fill_id=fill_id,
    )


def result(equities, fills=(), positions=None) -> BacktestResult:
    c = curve(equities, positions)
    return BacktestResult(
        strategy_id="t", run_id="r", start_date=c[0].valuation_date if c else START,
        end_date=c[-1].valuation_date if c else START, starting_cash=D(100),
        equity_curve=c, fills=list(fills),
    )


def round_trips(n, win=True):
    out = []
    for i in range(n):
        out.append(fill(f"b{i}", Side.BUY, 10, 10))
        out.append(fill(f"s{i}", Side.SELL, 10, 11 if win else 9))
    return out


def daily_returns(eq):
    a = np.array([float(x) for x in eq])
    return a[1:] / a[:-1] - 1


WAVY = [100, 101, 99, 103, 102, 107, 104, 108, 106, 111, 109, 112]


# --- drawdown ------------------------------------------------------------

def test_drawdown_series_is_positive_fraction_below_peak():
    assert drawdown_series(curve([100, 120, 90, 120, 130])) == [D(0), D(0), D("0.25"), D(0), D(0)]


def test_max_drawdown_positive_magnitude():
    m = compute_metrics(result([100, 120, 90, 120]))
    assert m.max_drawdown == D("0.25")


def test_monotonic_rising_curve_has_exactly_zero_drawdown():
    m = compute_metrics(result([100, 101, 105, 110]))
    assert m.max_drawdown == D(0) and isinstance(m.max_drawdown, Decimal)
    assert m.max_drawdown_duration_days == 0 and m.max_drawdown_duration_sessions == 0
    assert m.calmar is None


def test_drawdown_duration_recovered():
    r = result([100, 110, 100, 95, 110, 120])
    m = compute_metrics(r)
    c = r.equity_curve
    assert m.max_drawdown_duration_sessions == 3  # peak at idx1, regained at idx4
    assert m.max_drawdown_duration_days == (c[4].valuation_date - c[1].valuation_date).days


def test_drawdown_duration_unrecovered_runs_to_end_of_curve():
    r = result([100, 110, 90, 95])
    m = compute_metrics(r)
    c = r.equity_curve
    assert m.max_drawdown_duration_sessions == 2
    assert m.max_drawdown_duration_days == (c[3].valuation_date - c[1].valuation_date).days


def test_drawdown_exact_in_decimal_no_float_drift():
    m = compute_metrics(result([D("0.3") - D("0.1") + D("0.8"), D("0.9")]))
    assert m.max_drawdown == D("0.1")


# --- ratios --------------------------------------------------------------

def test_sharpe_matches_independent_calculation():
    m = compute_metrics(result(WAVY))
    r = daily_returns(WAVY)
    assert float(m.sharpe) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252), rel=1e-9)
    assert float(m.annualised_volatility) == pytest.approx(r.std(ddof=1) * math.sqrt(252), rel=1e-9)


def test_sortino_matches_independent_calculation():
    m = compute_metrics(result(WAVY))
    r = daily_returns(WAVY)
    downside = math.sqrt(np.mean(np.minimum(r, 0.0) ** 2))
    assert float(m.sortino) == pytest.approx(r.mean() / downside * math.sqrt(252), rel=1e-9)


def test_sharpe_and_sortino_none_with_fewer_than_two_returns():
    for eq in ([], [100], [100, 101]):
        m = compute_metrics(result(eq) if eq else BacktestResult("t", "r", START, START, D(100)))
        assert m.sharpe is None and m.sortino is None and m.annualised_volatility is None


def test_sharpe_none_not_zero_when_returns_have_zero_variance():
    flat = compute_metrics(result([100] * 10))
    assert flat.sharpe is None and flat.sortino is None
    growth = [D(100) * D("1.01") ** i for i in range(30)]
    steady = compute_metrics(result(growth))
    assert steady.sharpe is None


def test_sortino_none_when_there_is_no_downside():
    m = compute_metrics(result([100, 101, 103, 106, 110]))
    assert m.sortino is None
    assert m.sharpe is not None and m.sharpe > 0


def test_risk_free_series_is_subtracted():
    rf = {d: D("0.05") for d in business_days(START, len(WAVY))}
    base = compute_metrics(result(WAVY))
    with_rf = compute_metrics(result(WAVY), risk_free_series=rf)
    r = daily_returns(WAVY) - 0.05 / 252
    assert float(with_rf.sharpe) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252), rel=1e-9)
    assert with_rf.sharpe < base.sharpe


def test_cagr_and_calmar():
    eq = [100, 90, 121]
    r = result(eq)
    m = compute_metrics(r)
    days = (r.equity_curve[-1].valuation_date - r.equity_curve[0].valuation_date).days
    expected = 1.21 ** (365.25 / days) - 1
    assert float(m.cagr) == pytest.approx(expected, rel=1e-9)
    assert float(m.total_return) == pytest.approx(0.21)
    assert float(m.calmar) == pytest.approx(expected / 0.10, rel=1e-9)


# --- trades --------------------------------------------------------------

def test_trade_statistics():
    fills = [
        fill("b1", Side.BUY, 10, 10), fill("s1", Side.SELL, 10, 12),
        fill("b2", Side.BUY, 10, 10), fill("s2", Side.SELL, 10, 9),
    ]
    m = compute_metrics(result(WAVY, fills))
    assert m.trade_count == 2 and m.fill_count == 4
    assert m.win_rate == D("0.5")
    assert m.profit_factor == D(2)  # 20 won / 10 lost
    assert m.avg_trade_return == D("0.05")  # (+20% and -10%) / 2


def test_trade_return_includes_fees_on_both_sides():
    fills = [fill("b", Side.BUY, 10, 10, fees="1"), fill("s", Side.SELL, 10, 12, fees="1")]
    m = compute_metrics(result(WAVY, fills))
    assert m.avg_trade_return == D("18") / D("101")  # (120 - 1) - (100 + 1) = 18 on cost 101
    assert m.profit_factor is None


def test_profit_factor_none_when_no_losing_trades_not_inf():
    m = compute_metrics(result(WAVY, round_trips(3, win=True)))
    assert m.profit_factor is None and m.win_rate == D(1)


def test_profit_factor_zero_when_only_losses():
    m = compute_metrics(result(WAVY, round_trips(3, win=False)))
    assert m.profit_factor == D(0) and m.win_rate == D(0)


def test_no_trades_gives_none_not_zero():
    m = compute_metrics(result(WAVY))
    assert m.trade_count == 0
    assert m.win_rate is None and m.avg_trade_return is None and m.profit_factor is None


def test_partial_fills_of_one_order_are_one_trade():
    fills = [
        fill("b", Side.BUY, 10, 10),
        fill("s", Side.SELL, 4, 12, fill_id="s-1"), fill("s", Side.SELL, 6, 12, fill_id="s-2"),
    ]
    assert compute_metrics(result(WAVY, fills)).trade_count == 1


def test_open_position_is_not_a_closed_trade():
    m = compute_metrics(result(WAVY, [fill("b", Side.BUY, 10, 10)]))
    assert m.trade_count == 0 and m.fill_count == 1


def test_costs_turnover_exposure():
    fills = [
        fill("b", Side.BUY, 100, 10, fees="1.5", slip="0.5"),
        fill("s", Side.SELL, 120, 10, fees="2.5", slip="1.0"),
    ]
    eq = [10000, 10000, 10000, 10000]
    m = compute_metrics(result(eq, fills, positions=[0, 1, 1, 1]))
    assert m.total_transaction_costs == D("4.0")
    assert m.total_slippage == D("1.5")
    assert m.turnover == D("2200") / 2 / D("10000")
    assert m.exposure == D("0.75")


# --- evidence honesty ----------------------------------------------------

def sufficient_result():
    eq = [100 + i + (i % 3) for i in range(252)]
    return result(eq, round_trips(30))


def test_sufficient_evidence():
    m = compute_metrics(sufficient_result())
    assert m.evidence is EvidenceLevel.SUFFICIENT and m.evidence_note == ""


def test_insufficient_trades_names_threshold_and_shortfall_but_still_computes():
    eq = [100 + i + (i % 3) for i in range(252)]
    m = compute_metrics(result(eq, round_trips(29)))
    assert m.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE
    assert "29" in m.evidence_note and "30" in m.evidence_note and "short by 1" in m.evidence_note
    assert "observations" not in m.evidence_note
    assert m.cagr is not None and m.sharpe is not None and m.win_rate is not None


def test_insufficient_observations_names_threshold_and_shortfall():
    m = compute_metrics(result([100 + i for i in range(100)], round_trips(30)))
    assert m.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE
    assert "100" in m.evidence_note and "252" in m.evidence_note and "short by 152" in m.evidence_note
    assert "trades" not in m.evidence_note
    assert m.cagr is not None


def test_both_thresholds_reported_together():
    m = compute_metrics(result([100 + i for i in range(100)], round_trips(5)))
    assert "short by 25" in m.evidence_note and "short by 152" in m.evidence_note


def test_thresholds_come_from_config():
    cfg = ResearchConfig(min_trades_for_evidence=2, min_observations_for_metrics=10)
    m = compute_metrics(result(WAVY, round_trips(2)), config=cfg)
    assert m.evidence is EvidenceLevel.SUFFICIENT


def test_zero_trades_is_insufficient_not_suppressed():
    m = compute_metrics(result([100 + i for i in range(300)]))
    assert m.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE and "0" in m.evidence_note
    assert m.cagr is not None


# --- benchmark -----------------------------------------------------------

def test_benchmark_relative_return_tracking_error_information_ratio():
    strat = [100, 102, 101, 106, 108, 112, 111, 118]
    bench = [100, 101, 102, 103, 105, 106, 108, 110]
    m = compute_metrics(result(strat), benchmark_curve=curve(bench))
    assert float(m.benchmark_relative_return) == pytest.approx(1.18 - 1.10)
    active = daily_returns(strat) - daily_returns(bench)
    te = active.std(ddof=1) * math.sqrt(252)
    assert float(m.tracking_error) == pytest.approx(te, rel=1e-9)
    assert float(m.information_ratio) == pytest.approx(active.mean() * 252 / te, rel=1e-9)


def test_no_benchmark_gives_none_for_relative_fields():
    m = compute_metrics(result(WAVY))
    assert m.benchmark_relative_return is None and m.tracking_error is None and m.information_ratio is None


def test_identical_benchmark_has_zero_tracking_error_and_no_information_ratio():
    m = compute_metrics(result(WAVY), benchmark_curve=curve(WAVY))
    assert m.benchmark_relative_return == D(0)
    assert m.tracking_error == D(0) and m.information_ratio is None


def test_benchmark_without_overlap_gives_none():
    far = curve([100, 101, 102], start=date(2030, 1, 1))
    m = compute_metrics(result(WAVY), benchmark_curve=far)
    assert m.benchmark_relative_return is None and m.tracking_error is None


# --- regimes -------------------------------------------------------------

def test_regime_labels_from_drawdown():
    labels = regime_labels_from_drawdown(curve([100, 120, 100, 119, 130]), D("0.10"))
    days = business_days(START, 5)
    assert [labels[d] for d in days] == ["NORMAL", "NORMAL", "DRAWDOWN", "NORMAL", "NORMAL"]


def test_regime_labels_reject_bad_threshold():
    for bad in (D(0), D(-1), D(2)):
        with pytest.raises(ValidationError):
            regime_labels_from_drawdown(curve([100, 101]), bad)


def test_performance_by_regime_uses_prior_day_label_and_flags_thin_regimes():
    eq = [100, 110, 99, 108.9, 98.01, 107.811]
    days = business_days(START, len(eq))
    labels = {d: ("LOW" if i < 3 else "HIGH") for i, d in enumerate(days)}
    m = compute_metrics(result(eq), regime_labels=labels)
    low, high = m.regimes["LOW"], m.regimes["HIGH"]
    assert (low.observations, high.observations) == (3, 2)
    assert float(low.total_return) == pytest.approx(1.1 * 0.9 * 1.1 - 1)
    assert float(high.total_return) == pytest.approx(0.9 * 1.1 - 1)
    assert high.max_drawdown == D("0.1") or float(high.max_drawdown) == pytest.approx(0.1)
    assert low.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE


def test_unlabelled_days_are_reported_not_dropped():
    days = business_days(START, 4)
    m = compute_metrics(result([100, 101, 102, 103]), regime_labels={days[0]: "A"})
    assert m.regimes["A"].observations == 1
    assert m.regimes["UNLABELLED"].observations == 2


def test_no_regime_labels_gives_empty_regimes():
    assert compute_metrics(result(WAVY)).regimes == {}


# --- serialisation / shape -----------------------------------------------

def test_to_dict_is_json_serialisable_and_lossless_for_decimals():
    m = compute_metrics(result(WAVY, round_trips(2)), benchmark_curve=curve(WAVY),
                        regime_labels={d: "R" for d in business_days(START, len(WAVY))})
    d = m.to_dict()
    text = json.dumps(d)
    assert json.loads(text)["evidence"] == "INSUFFICIENT_EVIDENCE"
    assert d["max_drawdown"] == str(m.max_drawdown)
    assert d["profit_factor"] is None
    assert d["regimes"]["R"]["observations"] == len(WAVY) - 1


def test_metrics_are_frozen():
    m = compute_metrics(result(WAVY))
    assert isinstance(m, PerformanceMetrics)
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.sharpe = D(1)


def test_empty_result_does_not_raise():
    m = compute_metrics(BacktestResult("t", "r", START, START, D(100)))
    assert m.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE
    assert m.cagr is None and m.max_drawdown == D(0) and m.trade_count == 0


# --- properties ----------------------------------------------------------

positive_curves = st.lists(st.decimals(min_value="0.01", max_value="1000000", places=2), min_size=1, max_size=60)


@settings(max_examples=150, deadline=None)
@given(positive_curves)
def test_property_max_drawdown_between_zero_and_one(eq):
    m = compute_metrics(result(eq))
    assert D(0) <= m.max_drawdown <= D(1)
    assert all(D(0) <= x <= D(1) for x in drawdown_series(curve(eq)))


@settings(max_examples=150, deadline=None)
@given(st.lists(st.decimals(min_value="0", max_value="1000000", places=2), min_size=0, max_size=60))
def test_property_metrics_never_raise_on_degenerate_curves(eq):
    r = result(eq) if eq else BacktestResult("t", "r", START, START, D(100))
    m = compute_metrics(r, benchmark_curve=curve(eq) if eq else None)
    assert m.max_drawdown >= 0
    json.dumps(m.to_dict())


@settings(max_examples=100, deadline=None)
@given(st.lists(st.decimals(min_value="1", max_value="1000", places=2), min_size=2, max_size=40))
def test_property_sharpe_is_none_or_finite(eq):
    m = compute_metrics(result(eq))
    for v in (m.sharpe, m.sortino):
        assert v is None or v.is_finite()
