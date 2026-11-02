"""Strategy library: look-ahead immunity, determinism, honest warmup, honest parameters."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from signalforge.core.errors import ValidationError
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import Signal, SignalDirection, Strategy
from signalforge.strategies.benchmark import BuyAndHold
from signalforge.strategies.mean_reversion import ZScoreMeanReversion
from signalforge.strategies.momentum import CrossSectionalMomentum
from signalforge.strategies.registry import STRATEGIES, build
from signalforge.strategies.trend import MovingAverageTrend
from signalforge.strategies.volatility import VolatilityTargetedTrend
from tests.conftest import business_days, make_bar

D = Decimal
ENTER, EXIT, HOLD = SignalDirection.ENTER_LONG, SignalDirection.EXIT_LONG, SignalDirection.HOLD
N = 400


def wavy(i: int, level: float, amp: float, period: float, drift: float) -> Decimal:
    return D(str(round(level + amp * math.sin(i / period) + drift * i, 4)))


# symbol -> (level, amplitude, period, drift, first bar index)
SPECS = {
    "A": (100.0, 12.0, 9.0, 0.05, 0),
    "B": (80.0, 9.0, 13.0, 0.01, 0),
    "C": (120.0, 15.0, 7.0, -0.02, 150),
}


def multi_history(end: int | None = None, future_scramble: bool = False, cut: int = 0) -> PriceHistory:
    """Three wavy symbols (C lists late). `end` truncates; `future_scramble`
    rewrites every bar after `cut` into a different, scaled series."""
    days = business_days(date(2020, 1, 1), N)
    bars = []
    for sym, (level, amp, period, drift, start) in SPECS.items():
        for i, d in enumerate(days):
            if i < start:
                continue
            if end is not None and i >= end:
                break
            if future_scramble and i > cut:
                idx = max(1, 2 * cut - i)
                price = wavy(idx, level, amp, period, drift) * D("0.37")
            else:
                price = wavy(i, level, amp, period, drift)
            bars.append(make_bar(sym, d, price))
    return PriceHistory.from_bars(bars)


def single(symbol: str, prices: list[Decimal], start: date = date(2020, 1, 1)) -> PriceHistory:
    days = business_days(start, len(prices))
    return PriceHistory.from_bars([make_bar(symbol, d, p) for d, p in zip(days, prices)])


def run_all(strategy: Strategy, history: PriceHistory) -> list[Signal]:
    out: list[Signal] = []
    for d in history.trading_days:
        out.extend(strategy.generate_signals(d, history.view(d)))
    return out


def counts(sigs):
    return {d.value: sum(1 for s in sigs if s.direction is d) for d in (ENTER, EXIT, HOLD)}


FACTORIES = {
    "trend": (
        lambda **kw: MovingAverageTrend(**{"fast_window": 10, "slow_window": 30, "target_weight": D("0.5"), "symbols": ("A", "B", "C"), **kw}),
        {"fast_window": 5, "slow_window": 60, "target_weight": D("0.25"), "symbols": ("A",)},
    ),
    "momentum": (
        lambda **kw: CrossSectionalMomentum(**{"lookback_days": 60, "skip_days": 5, "top_n": 1, "target_weight_total": D("0.6"), "min_history_days": 70, **kw}),
        {"lookback_days": 20, "skip_days": 0, "top_n": 2, "target_weight_total": D("0.3"), "min_history_days": 300},
    ),
    "meanrev": (
        lambda **kw: ZScoreMeanReversion(**{"window": 20, "entry_z": D("1.0"), "exit_z": D("0"), "target_weight": D("0.5"), "max_holding_days": 20, "symbols": ("A", "B", "C"), **kw}),
        {"window": 40, "entry_z": D("1.5"), "exit_z": D("0.5"), "target_weight": D("0.25"), "max_holding_days": 3, "symbols": ("B",)},
    ),
    "voltrend": (
        lambda **kw: VolatilityTargetedTrend(**{"fast_window": 10, "slow_window": 30, "target_volatility": D("0.10"), "vol_window": 20, "max_weight": D("1.0"), "annualisation_factor": 252, "symbols": ("A", "B", "C"), **kw}),
        {"fast_window": 5, "slow_window": 60, "target_volatility": D("0.20"), "vol_window": 40, "max_weight": D("0.1"), "annualisation_factor": 100, "symbols": ("A",)},
    ),
    "buyhold": (
        lambda **kw: BuyAndHold(**{"symbols": ("A", "B", "C"), "target_weight": D("0.5"), **kw}),
        {"symbols": ("A",), "target_weight": D("0.25")},
    ),
}
IDS = list(FACTORIES)


@pytest.mark.parametrize("name", IDS)
def test_conforms_to_strategy_protocol(name):
    s = FACTORIES[name][0]()
    assert isinstance(s, Strategy)
    assert s.warmup_days >= 1 and s.strategy_id


@pytest.mark.parametrize("name", IDS)
def test_no_lookahead_signals_identical_with_or_without_future_bars(name):
    make = FACTORIES[name][0]
    full = multi_history()
    for cut in range(0, N, 9):
        as_of = full.trading_days[cut]
        truncated = multi_history(end=cut + 1)
        scrambled = multi_history(future_scramble=True, cut=cut)
        a = make().generate_signals(as_of, full.view(as_of))
        b = make().generate_signals(as_of, truncated.view(as_of))
        c = make().generate_signals(as_of, scrambled.view(as_of))
        assert a == b == c, f"{name} leaked the future at {as_of}"


@pytest.mark.parametrize("name", IDS)
def test_deterministic_repeated_calls_and_fresh_instances(name):
    make = FACTORIES[name][0]
    h = multi_history()
    s = make()
    assert run_all(s, h) == run_all(s, h) == run_all(make(), h)


@pytest.mark.parametrize("name", IDS)
def test_as_of_must_match_view(name):
    h = multi_history()
    with pytest.raises(ValidationError):
        FACTORIES[name][0]().generate_signals(h.trading_days[200], h.view(h.trading_days[100]))


@pytest.mark.parametrize("name", IDS)
def test_warmup_is_honest(name):
    s = FACTORIES[name][0]()
    h = multi_history()
    days = h.trading_days
    for i, d in enumerate(days[: s.warmup_days - 1]):
        assert s.generate_signals(d, h.view(d)) == [], f"{name} signalled with {i + 1} bars"
    d = days[s.warmup_days - 1]
    assert s.generate_signals(d, h.view(d)) != []


@pytest.mark.parametrize("name", IDS)
def test_every_declared_parameter_changes_behaviour(name):
    make, alt = FACTORIES[name]
    base = make()
    assert set(base.parameters()) == set(alt), f"{name}: parameters() and alt overrides disagree"
    h = multi_history()
    base_signals = run_all(base, h)
    for key, value in alt.items():
        assert run_all(make(**{key: value}), h) != base_signals, f"{name}: parameter {key} has no effect"


@pytest.mark.parametrize("name", IDS)
def test_parameters_are_reconstructible(name):
    s = FACTORIES[name][0]()
    rebuilt = type(s)(**s.parameters())
    assert rebuilt.parameters() == s.parameters()
    h = multi_history()
    assert run_all(rebuilt, h) == run_all(s, h)


@pytest.mark.parametrize("name", IDS)
def test_signals_are_decimal_weighted_and_explained(name):
    h = multi_history()
    s = FACTORIES[name][0]()
    sigs = run_all(s, h)
    assert sigs
    for sig in sigs:
        assert isinstance(sig.target_weight, Decimal)
        assert 0 <= sig.target_weight <= 1
        assert sig.reason and sig.strategy_id == s.strategy_id
        if sig.direction is EXIT:
            assert sig.target_weight == 0


# --- trend ---------------------------------------------------------------

def test_trend_fixture_behaviour(uptrend_history, downtrend_history, flat_history):
    up = run_all(MovingAverageTrend(10, 30, D("0.5"), ("UP",)), uptrend_history)
    dn = run_all(MovingAverageTrend(10, 30, D("0.5"), ("DOWN",)), downtrend_history)
    fl = run_all(MovingAverageTrend(10, 30, D("0.5"), ("FLAT",)), flat_history)
    print("TREND up/down/flat", counts(up), counts(dn), counts(fl))
    assert counts(up)["ENTER_LONG"] == 300 - 29 and counts(up)["EXIT_LONG"] == 0
    assert counts(dn)["ENTER_LONG"] == 0
    assert counts(fl)["ENTER_LONG"] == 0


def test_trend_exits_when_fast_falls_to_slow():
    prices = [D(100 + i) for i in range(40)] + [D(139 - 3 * i) for i in range(1, 40)]
    h = single("X", prices)
    dirs = [s.direction for s in run_all(MovingAverageTrend(5, 20, D("0.5"), ("X",)), h)]
    assert dirs[0] is ENTER and EXIT in dirs
    assert dirs.index(EXIT) > dirs.index(ENTER)


def test_trend_rejects_fast_not_below_slow():
    with pytest.raises(ValidationError):
        MovingAverageTrend(30, 30, D("0.5"), ("X",))


def test_trend_ignores_symbols_without_bars_yet():
    h = multi_history()
    d = h.trading_days[100]
    sigs = MovingAverageTrend(10, 30, D("0.5"), ("A", "C")).generate_signals(d, h.view(d))
    assert {s.symbol for s in sigs} == {"A"}


def _ramps(slopes: dict[str, str], n: int = 60) -> PriceHistory:
    days = business_days(date(2020, 1, 1), n)
    return PriceHistory.from_bars(
        [make_bar(sym, d, D(100) + D(slope) * i) for sym, slope in slopes.items() for i, d in enumerate(days)]
    )


def _last_day_signals(strategy, history) -> dict[str, Signal]:
    d = history.trading_days[-1]
    return {s.symbol: s for s in strategy.generate_signals(d, history.view(d))}


def test_ma_trend_without_max_names_parameters_unchanged(uptrend_history):
    s = MovingAverageTrend(10, 30, D("0.5"), ("UP",))
    assert set(s.parameters()) == {"fast_window", "slow_window", "target_weight", "symbols"}
    capped_off = MovingAverageTrend(10, 30, D("0.5"), ("UP",), max_names=None)
    assert run_all(capped_off, uptrend_history) == run_all(s, uptrend_history)
    h = multi_history()
    assert run_all(MovingAverageTrend(10, 30, D("0.5"), ("A", "B", "C")), h) == run_all(
        MovingAverageTrend(10, 30, D("0.5"), ("A", "B", "C"), max_names=None), h
    )


def test_ma_trend_max_names_enters_top_n_by_strength():
    h = _ramps({"S1": "0.1", "S2": "0.3", "S3": "0.2", "S4": "0.05"})
    sigs = _last_day_signals(MovingAverageTrend(10, 30, D("0.5"), tuple(h.symbols), max_names=2), h)
    assert {k for k, v in sigs.items() if v.direction is ENTER} == {"S2", "S3"}
    for sym in ("S1", "S4"):
        assert sigs[sym].direction is EXIT
        assert "outside top 2" in sigs[sym].reason
    assert sigs["S2"].target_weight == D("0.5") and sigs["S1"].target_weight == 0


def test_ma_trend_max_names_ties_break_by_symbol():
    h = _ramps({"B": "0.2", "A": "0.2", "C": "0.2"})
    sigs = _last_day_signals(MovingAverageTrend(10, 30, D("0.5"), ("A", "B", "C"), max_names=2), h)
    assert {k for k, v in sigs.items() if v.direction is ENTER} == {"A", "B"}
    assert sigs["C"].direction is EXIT


def test_ma_trend_max_names_non_trending_symbol_exits_normally():
    h = _ramps({"UP": "0.3", "DN": "-0.3"})
    sigs = _last_day_signals(MovingAverageTrend(10, 30, D("0.5"), ("UP", "DN"), max_names=5), h)
    assert sigs["UP"].direction is ENTER
    assert sigs["DN"].direction is EXIT and "outside top" not in sigs["DN"].reason


def test_ma_trend_max_names_parameter_is_reported_and_reconstructible():
    s = MovingAverageTrend(10, 30, D("0.5"), ("A",), max_names=3)
    assert s.parameters()["max_names"] == 3
    assert type(s)(**s.parameters()).parameters() == s.parameters()


@pytest.mark.parametrize("bad", [0, -1, True, 2.5, "3"])
def test_ma_trend_max_names_must_be_positive_int(bad):
    with pytest.raises(ValidationError):
        MovingAverageTrend(10, 30, D("0.5"), ("A",), max_names=bad)


# --- momentum ------------------------------------------------------------

def test_momentum_fixture_behaviour(uptrend_history, downtrend_history, flat_history):
    m = lambda: CrossSectionalMomentum(60, 5, 1, D("0.6"), 70)
    up, dn, fl = (run_all(m(), h) for h in (uptrend_history, downtrend_history, flat_history))
    print("MOM up/down/flat", counts(up), counts(dn), counts(fl))
    assert counts(up)["ENTER_LONG"] > 0


def test_momentum_picks_top_n_equal_weight_and_exits_the_rest():
    days = business_days(date(2020, 1, 1), 100)
    bars = []
    for i, d in enumerate(days):
        bars.append(make_bar("HI", d, D(100 + 2 * i)))
        bars.append(make_bar("MID", d, D(100 + i)))
        bars.append(make_bar("LO", d, D(100 + i // 4)))
    h = PriceHistory.from_bars(bars)
    s = CrossSectionalMomentum(lookback_days=20, skip_days=0, top_n=2, target_weight_total=D("0.6"), min_history_days=21)
    by = {x.symbol: x for x in s.generate_signals(days[-1], h.view(days[-1]))}
    assert by["HI"].direction is ENTER and by["MID"].direction is ENTER
    assert by["HI"].target_weight == by["MID"].target_weight == D("0.3")
    assert by["LO"].direction is EXIT and by["LO"].target_weight == 0


def test_momentum_skip_days_excludes_recent_bars():
    days = business_days(date(2020, 1, 1), 60)
    bars = []
    for i, d in enumerate(days):
        bars.append(make_bar("RECENT", d, D(100) if i < 55 else D(100 + 40 * (i - 54))))
        bars.append(make_bar("STEADY", d, D(100 + i)))
    h = PriceHistory.from_bars(bars)

    def top(skip):
        s = CrossSectionalMomentum(lookback_days=30, skip_days=skip, top_n=1, target_weight_total=D("1"), min_history_days=36)
        return [x.symbol for x in s.generate_signals(days[-1], h.view(days[-1])) if x.direction is ENTER]

    assert top(0) == ["RECENT"]
    assert top(5) == ["STEADY"]


def test_momentum_never_ranks_symbols_without_a_bar_yet():
    h = multi_history()
    s = CrossSectionalMomentum(60, 5, 2, D("0.6"), 70)
    c_first = h.bars_for("C")[0].trade_date
    checked = 0
    for d in h.trading_days:
        if d >= c_first:
            break
        sigs = s.generate_signals(d, h.view(d))
        assert all(x.symbol in h.view(d).cross_section() for x in sigs)
        assert "C" not in {x.symbol for x in sigs}
        checked += len(sigs)
    assert checked > 0


def test_momentum_skips_symbols_with_insufficient_own_history():
    h = multi_history()
    s = CrossSectionalMomentum(60, 5, 3, D("0.6"), 70)
    d = h.trading_days[150 + 30]  # C has only 31 bars
    sigs = s.generate_signals(d, h.view(d))
    assert sigs and "C" not in {x.symbol for x in sigs}


# --- mean reversion --------------------------------------------------------

def dip_prices(tail: int) -> list[Decimal]:
    return [D(100 + (i % 2)) for i in range(30)] + [D(90)] * tail


def test_meanrev_fixture_behaviour(uptrend_history, downtrend_history, flat_history):
    m = lambda s: ZScoreMeanReversion(20, D("2"), D("0"), D("0.5"), 20, (s,))
    up = run_all(m("UP"), uptrend_history)
    dn = run_all(m("DOWN"), downtrend_history)
    fl = run_all(m("FLAT"), flat_history)
    print("MEANREV up/down/flat", counts(up), counts(dn), counts(fl))
    assert counts(fl)["ENTER_LONG"] == 0


def test_meanrev_enters_on_deep_dip_and_exits_on_recovery():
    h = single("X", dip_prices(1) + [D(100), D(101), D(100)])
    s = ZScoreMeanReversion(20, D("2"), D("0"), D("0.5"), 20, ("X",))
    by_day = {d: s.generate_signals(d, h.view(d)) for d in h.trading_days}
    assert by_day[h.trading_days[30]][0].direction is ENTER
    assert by_day[h.trading_days[31]][0].direction is EXIT
    assert not any(sig.direction is ENTER for d in h.trading_days[:30] for sig in by_day[d])


def test_meanrev_max_holding_days_forces_exit():
    h = single("X", dip_prices(10))
    short = ZScoreMeanReversion(20, D("2"), D("0"), D("0.5"), 3, ("X",))
    long_ = ZScoreMeanReversion(20, D("2"), D("0"), D("0.5"), 100, ("X",))
    d3 = h.trading_days[33]
    assert short.generate_signals(d3, h.view(d3))[0].direction is EXIT
    assert long_.generate_signals(d3, h.view(d3))[0].direction is ENTER


def test_meanrev_zero_variance_is_never_an_entry(flat_history):
    s = ZScoreMeanReversion(20, D("2"), D("0"), D("0.5"), 20, ("FLAT",))
    assert all(x.direction is not ENTER for x in run_all(s, flat_history))


def test_meanrev_rejects_exit_z_not_inside_entry_z():
    with pytest.raises(ValidationError):
        ZScoreMeanReversion(20, D("1"), D("1"), D("0.5"), 20, ("X",))


# --- volatility targeted ---------------------------------------------------

def test_voltrend_fixture_behaviour(uptrend_history, downtrend_history, flat_history):
    mk = lambda s: VolatilityTargetedTrend(10, 30, D("0.10"), 20, D("1.0"), 252, (s,))
    up = run_all(mk("UP"), uptrend_history)
    dn = run_all(mk("DOWN"), downtrend_history)
    fl = run_all(mk("FLAT"), flat_history)
    print("VOLTREND up/down/flat", counts(up), counts(dn), counts(fl))
    assert counts(up)["ENTER_LONG"] > 0
    assert counts(dn)["ENTER_LONG"] == 0 and counts(fl)["ENTER_LONG"] == 0


def test_voltrend_zero_vol_gives_the_cap_not_a_division_by_zero():
    # price doubles every day: every return is exactly 1, so realised vol is exactly 0
    h = single("X", [D(100) * (D(2) ** i) for i in range(45)])
    s = VolatilityTargetedTrend(5, 20, D("0.10"), 20, D("0.8"), 252, ("X",))
    sig = s.generate_signals(h.trading_days[-1], h.view(h.trading_days[-1]))[0]
    assert sig.direction is ENTER and sig.target_weight == D("0.8")


def _alternating(amp: Decimal) -> list[Decimal]:
    p, out = D(100), []
    for i in range(80):
        p = p * (D(1) + amp if i % 2 else D("1.0005"))
        out.append(p)
    return out


def test_voltrend_weight_matches_target_over_realised_vol():
    h = single("X", _alternating(D("0.02")))
    s = VolatilityTargetedTrend(5, 20, D("0.10"), 20, D("1.0"), 252, ("X",))
    view = h.view(h.trading_days[-1])
    sig = s.generate_signals(h.trading_days[-1], view)[0]
    r = np.array([float(x) for x in view.returns("X", 20)])
    expected = 0.10 / (r.std(ddof=1) * math.sqrt(252))
    assert sig.direction is ENTER
    assert expected < 1.0
    assert float(sig.target_weight) == pytest.approx(expected, abs=1e-5)


def test_voltrend_higher_vol_gets_lower_weight():
    w = {}
    for name, amp in (("calm", D("0.005")), ("wild", D("0.04"))):
        h = single("X", _alternating(amp))
        sig = VolatilityTargetedTrend(5, 20, D("0.10"), 20, D("1.0"), 252, ("X",)).generate_signals(
            h.trading_days[-1], h.view(h.trading_days[-1]))[0]
        w[name] = sig.target_weight
    assert w["wild"] < w["calm"]


def test_voltrend_weight_never_exceeds_cap():
    h = multi_history()
    s = VolatilityTargetedTrend(10, 30, D("0.50"), 20, D("0.3"), 252, ("A", "B"))
    assert max(x.target_weight for x in run_all(s, h)) <= D("0.3")


# --- buy and hold --------------------------------------------------------

def test_buyhold_asserts_long_from_its_first_data_day_and_never_sells(uptrend_history, downtrend_history, flat_history):
    # Corrected contract. Entering only on the symbol's first-ever bar meant
    # that over a window starting later than the series, the benchmark emitted
    # HOLD from day one and never bought at all (real NASDAQCOM validation
    # window returned exactly 0.0%). Buy-and-hold now re-asserts the desired
    # state every bar; the engine's no-pyramiding no-op makes it buy once.
    # See tests/test_benchmark_window.py for the end-to-end regression.
    for hist, sym in ((uptrend_history, "UP"), (downtrend_history, "DOWN"), (flat_history, "FLAT")):
        sigs = run_all(BuyAndHold((sym,), D("1.0")), hist)
        print("BUYHOLD", sym, counts(sigs))
        c = counts(sigs)
        assert c["EXIT_LONG"] == 0, "buy and hold must never sell"
        assert c["ENTER_LONG"] == len(sigs), "the desired state is asserted every bar"
        assert sigs[0].direction is ENTER and sigs[0].as_of == hist.trading_days[0]


def test_buyhold_does_not_signal_a_symbol_before_it_has_data():
    h = multi_history()
    sigs = run_all(BuyAndHold(("A", "C"), D("0.5")), h)
    c_first = h.bars_for("C")[0].trade_date
    c_enters = [x.as_of for x in sigs if x.symbol == "C" and x.direction is ENTER]
    assert c_enters, "C should be entered once it lists"
    assert min(c_enters) == c_first
    assert not any(x.symbol == "C" and x.as_of < c_first for x in sigs)


# --- registry ------------------------------------------------------------

def test_registry_has_every_strategy_id():
    assert set(STRATEGIES) == {"ma_trend", "xs_momentum", "zscore_mean_reversion", "vol_targeted_trend",
                               "tsmom", "buy_and_hold"}
    for sid, cls in STRATEGIES.items():
        assert cls.strategy_id == sid


def test_registry_build_roundtrips_parameters():
    s = build("ma_trend", fast_window=5, slow_window=20, target_weight=D("0.4"), symbols=["A", "B"])
    assert isinstance(s, MovingAverageTrend)
    assert build("ma_trend", **s.parameters()).parameters() == s.parameters()


def test_registry_unknown_id_and_unknown_param_raise_validation_error():
    with pytest.raises(ValidationError):
        build("nope")
    with pytest.raises(ValidationError):
        build("ma_trend", fast_window=5, slow_window=20, target_weight=D("0.4"), symbols=["A"], bogus=1)


# --- registry wrappers ---------------------------------------------------

def test_build_wraps_eligibility_then_overlay():
    from signalforge.strategies.eligibility import EligibilityFilter
    from signalforge.strategies.regime import RegimeOverlay, RiskOffGate

    gate = RiskOffGate("vix30", "VIXCLS", ">", D("30"), 1, ())
    built = build(
        "ma_trend",
        fast_window=10, slow_window=30, target_weight=D("0.05"), symbols=("A", "B"),
        max_names=1,
        eligibility={"min_history_sessions": 252, "min_median_dollar_volume": D("500000"),
                     "dollar_volume_window": 63,
                     "max_stale_days": 5},
        riskoff_gate=gate,
    )
    assert isinstance(built, RegimeOverlay)
    assert isinstance(built.inner, EligibilityFilter)
    assert isinstance(built.inner.inner, MovingAverageTrend)
    assert built.inner.candidates == frozenset({"A", "B"})
    assert built.strategy_id == "ma_trend_riskoff_vix30"
    assert built.parameters()["elig_min_history_sessions"] == 252
    assert built.parameters()["max_names"] == 1


def test_build_without_wrappers_returns_plain_class():
    built = build("ma_trend", fast_window=10, slow_window=30, target_weight=D("0.05"), symbols=("A",))
    assert type(built) is MovingAverageTrend


def test_build_eligibility_only_returns_the_filter():
    from signalforge.strategies.eligibility import EligibilityFilter

    built = build(
        "xs_momentum", lookback_days=60, skip_days=5, top_n=1, target_weight_total=D("0.6"),
        min_history_days=70, symbols=("A", "B"),
        eligibility={"min_history_sessions": 252, "min_median_dollar_volume": D("500000"),
                     "dollar_volume_window": 63,
                     "max_stale_days": 5},
    )
    assert isinstance(built, EligibilityFilter) and built.strategy_id == "xs_momentum"


def test_build_eligibility_needs_symbols():
    with pytest.raises(ValidationError):
        build(
            "xs_momentum", lookback_days=60, skip_days=5, top_n=1, target_weight_total=D("0.6"),
            min_history_days=70,
            eligibility={"min_history_sessions": 252, "min_median_dollar_volume": D("500000"),
                         "dollar_volume_window": 63,
                     "max_stale_days": 5},
        )
