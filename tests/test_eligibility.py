"""Point-in-time eligibility: who the strategy is allowed to see on a given day."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from signalforge.core.errors import LookAheadError, ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import SignalDirection
from signalforge.core.types import Bar
from signalforge.strategies.eligibility import EligibilityFilter, EligibleView
from signalforge.strategies.momentum import CrossSectionalMomentum
from signalforge.strategies.trend import MovingAverageTrend
from tests.conftest import business_days, make_bar

D = Decimal
ENTER, EXIT = SignalDirection.ENTER_LONG, SignalDirection.EXIT_LONG
DAYS = business_days(date(2020, 1, 1), 60)


def series(symbol, closes, volumes, *, known=True, scale=D(1)):
    out = []
    for day, c, v in zip(DAYS, closes, volumes):
        px = D(c) * scale
        out.append(
            Bar(symbol=symbol, trade_date=day, open=px, high=px, low=px, close=px,
                volume=int(v / scale) if known else 0, adj_close=px, source="fixture",
                volume_known=known)
        )
    return out


def flt(inner, symbols, *, sessions=5, floor="1000", window=3, stale=5):
    return EligibilityFilter(inner, symbols, sessions, D(floor), window, stale)


def trend(symbols):
    return MovingAverageTrend(2, 3, D("0.05"), symbols)


def signals_by_day(strategy, history):
    return {
        d: strategy.generate_signals(d, history.view(d)) for d in history.trading_days
    }


def rising(n=60):
    return [100 + i for i in range(n)]


def test_ineligible_until_min_history_sessions():
    history = PriceHistory.from_bars(series("AAA", rising(), [1000] * 60))
    by_day = signals_by_day(flt(trend(("AAA",)), ("AAA",), sessions=5), history)
    first_enter = next(i for i, d in enumerate(DAYS) if any(s.direction is ENTER for s in by_day[d]))
    assert first_enter == 4  # the fifth visible session


def test_dollar_volume_floor_uses_close_times_volume_median():
    # dollar volume per bar = 100 * volume. Median over 3 bars, floor 4000.
    steady_with_dip = [60, 50, 50] + [50] * 57      # median 5000 even if one dips
    spike = [1] * 59 + [100000]                    # mean huge, median tiny
    bars = series("AAA", [100] * 60, [v for v in steady_with_dip])
    bars += series("BBB", [100] * 60, spike)
    history = PriceHistory.from_bars(bars)
    view = history.view(DAYS[-1])
    eligible = flt(trend(("AAA", "BBB")), ("AAA", "BBB"), floor="4000").eligible(view)
    assert eligible == frozenset({"AAA"})


def test_dollar_volume_requires_volume_known():
    history = PriceHistory.from_bars(series("AAA", rising(), [10**9] * 60, known=False))
    view = history.view(DAYS[-1])
    assert flt(trend(("AAA",)), ("AAA",)).eligible(view) == frozenset()


def test_dollar_volume_invariant_to_future_adjustment():
    closes, vols = rising(), [100] * 60
    plain = PriceHistory.from_bars(series("AAA", closes, vols))
    # A later 4:1 split rescales every earlier bar: price / 4, volume * 4.
    adjusted = PriceHistory.from_bars(series("AAA", closes, vols, scale=D("0.25")))
    f = flt(trend(("AAA",)), ("AAA",), floor="5000")
    for day in DAYS:
        assert f.eligible(plain.view(day)) == f.eligible(adjusted.view(day))


def test_symbols_outside_the_static_list_are_never_eligible():
    history = PriceHistory.from_bars(series("AAA", rising(), [1000] * 60) + series("ZZZ", rising(), [1000] * 60))
    view = history.view(DAYS[-1])
    assert flt(trend(("AAA",)), ("AAA",)).eligible(view) == frozenset({"AAA"})


def test_filter_emits_exit_for_held_symbol_that_becomes_ineligible():
    volumes = [1000] * 30 + [0] * 30
    history = PriceHistory.from_bars(series("AAA", rising(), volumes))
    by_day = signals_by_day(flt(trend(("AAA",)), ("AAA",), floor="1000"), history)
    early = by_day[DAYS[20]]
    assert [s.direction for s in early] == [ENTER]
    late = by_day[DAYS[-1]]
    assert [s.direction for s in late] == [EXIT]
    assert "ineligible" in late[0].reason
    assert late[0].strategy_id == "ma_trend"


def test_xs_momentum_ranks_only_eligible():
    bars = series("HOT", [100 + 3 * i for i in range(60)], [1] * 60)
    bars += series("OK1", [100 + i for i in range(60)], [1000] * 60)
    bars += series("OK2", [100 + i // 2 for i in range(60)], [1000] * 60)
    history = PriceHistory.from_bars(bars)
    inner = CrossSectionalMomentum(10, 2, 1, D("1.0"), 12)
    sigs = flt(inner, ("HOT", "OK1", "OK2"), floor="1000").generate_signals(
        DAYS[-1], history.view(DAYS[-1])
    )
    by_symbol = {s.symbol: s.direction for s in sigs}
    assert by_symbol["OK1"] is ENTER
    assert by_symbol["OK2"] is EXIT
    assert by_symbol["HOT"] is EXIT and "ineligible" in next(s.reason for s in sigs if s.symbol == "HOT")


def test_eligible_view_cannot_see_future():
    history = PriceHistory.from_bars(series("AAA", rising(), [1000] * 60))
    view = EligibleView(history.view(DAYS[10]), frozenset({"AAA"}))
    assert view.as_of == DAYS[10]
    assert len(view.closes("AAA")) == 11
    with pytest.raises(LookAheadError):
        view.bar("AAA", DAYS[11])


def test_eligible_view_hides_ineligible_from_has_history_and_cross_section():
    history = PriceHistory.from_bars(series("AAA", rising(), [1000] * 60) + series("BBB", rising(), [1000] * 60))
    view = EligibleView(history.view(DAYS[30]), frozenset({"AAA"}))
    assert view.cross_section() == ("AAA",)
    assert view.has_history("AAA", 5) and not view.has_history("BBB", 5)


def test_filter_parameters_include_elig_keys():
    inner = trend(("AAA",))
    f = flt(inner, ("AAA",), sessions=252, floor="500000", window=63)
    params = f.parameters()
    assert params["elig_min_history_sessions"] == 252
    assert params["elig_min_median_dollar_volume"] == D("500000")
    assert params["elig_dollar_volume_window"] == 63
    assert params["elig_max_stale_days"] == 5
    assert {k: v for k, v in params.items() if not k.startswith("elig_")} == inner.parameters()
    assert f.strategy_id == "ma_trend"
    assert f.warmup_days == inner.warmup_days


def test_gap_makes_a_name_ineligible_until_it_prints_again():
    closes, vols = rising(), [1000] * 60
    aaa = [b for i, b in enumerate(series("AAA", closes, vols)) if not 25 <= i <= 40]
    bbb = series("BBB", closes, vols)
    history = PriceHistory.from_bars(aaa + bbb)
    by_day = signals_by_day(flt(trend(("AAA", "BBB")), ("AAA", "BBB"), stale=5), history)

    def aaa_sig(i):
        return [s for s in by_day[DAYS[i]] if s.symbol == "AAA"]

    assert [s.direction for s in aaa_sig(20)] == [ENTER]
    assert [s.direction for s in aaa_sig(24)] == [ENTER]
    for i in (33, 38, 40):
        sigs = aaa_sig(i)
        assert [s.direction for s in sigs] == [EXIT] and "ineligible" in sigs[0].reason
    assert [s.direction for s in aaa_sig(41)] == [ENTER]
    assert [s.direction for s in aaa_sig(55)] == [ENTER]


def test_staleness_limit_comes_from_the_argument_not_a_constant():
    bars = [b for i, b in enumerate(series("AAA", rising(), [1000] * 60)) if not 25 <= i <= 40]
    history = PriceHistory.from_bars(bars + series("BBB", rising(), [1000] * 60))
    view = history.view(DAYS[33])
    assert flt(trend(("AAA",)), ("AAA",), stale=5).eligible(view) == frozenset()
    assert flt(trend(("AAA",)), ("AAA",), stale=30).eligible(view) == frozenset({"AAA"})


@pytest.mark.parametrize("kwargs", [{"sessions": 0}, {"window": 0}, {"floor": "-1"}, {"stale": 0}])
def test_filter_rejects_bad_settings(kwargs):
    with pytest.raises(ValidationError):
        flt(trend(("AAA",)), ("AAA",), **kwargs)


def test_staleness_boundary_matches_the_risk_engine():
    from datetime import timedelta

    from signalforge.config.models import RiskConfig
    from signalforge.risk.breakers import check_staleness

    bars = [b for i, b in enumerate(series("AAA", rising(), [1000] * 60)) if i <= 24]
    history = PriceHistory.from_bars(bars + series("BBB", rising(), [1000] * 60))
    last = DAYS[24]
    f = flt(trend(("AAA",)), ("AAA",), stale=5)
    risk = RiskConfig(stale_data_max_age_days=5)
    at_limit, past_limit = history.view(last + timedelta(5)), history.view(last + timedelta(6))
    assert f.eligible(at_limit) == frozenset({"AAA"})
    assert check_staleness("AAA", at_limit, risk) is None
    assert f.eligible(past_limit) == frozenset()
    assert check_staleness("AAA", past_limit, risk) is not None


def test_exit_reason_names_every_rule_of_the_screen():
    bars = [b for i, b in enumerate(series("AAA", rising(), [1000] * 60)) if not 25 <= i <= 40]
    history = PriceHistory.from_bars(bars + series("BBB", rising(), [1000] * 60))
    sigs = [s for s in flt(trend(("AAA",)), ("AAA",)).generate_signals(DAYS[33], history.view(DAYS[33]))
            if s.symbol == "AAA"]
    assert "staleness" in sigs[0].reason and "volume" in sigs[0].reason
