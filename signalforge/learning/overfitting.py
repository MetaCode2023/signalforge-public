"""Overfitting diagnostics.

Each function answers one version of the same question: is this edge real, or
is it what a search produces? None of them proves an edge. They exist to make
the cheap ways of fooling yourself visible and, where a rule decides a verdict,
to say which rule.

Diagnostics evaluate on the train window by default and refuse any explicit
range that touches the held-out test window, so a diagnostic cannot become a
back-door second look at the test data.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import NormalDist
from typing import Callable, Sequence

import numpy as np

from signalforge.backtesting.metrics import _closed_trades, compute_metrics
from signalforge.core.errors import ValidationError
from signalforge.learning.registry import StrategySpec, canonical_json

COLLAPSE_FRACTION = 0.5
CHEAP_COST_BPS = 10.0
MIN_BOOTSTRAP_SAMPLE = 20
SIGNIFICANCE = 0.95
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


@dataclass(frozen=True)
class Score:
    total_return: float
    sharpe: float | None


def _score(engine, spec: StrategySpec, history, config, start, end, execution=None) -> Score:
    result = engine.run(spec.build(), history, start=start, end=end, execution=execution)
    metrics = compute_metrics(result, config=config)
    return Score(
        total_return=float(metrics.total_return) if metrics.total_return is not None else 0.0,
        sharpe=float(metrics.sharpe) if metrics.sharpe is not None else None,
    )


def _resolve_range(config, start: date | None, end: date | None) -> tuple[date | None, date | None]:
    splits = config.splits
    if start is None and end is None:
        return (splits.train_start, splits.train_end) if splits is not None else (None, None)
    if splits is not None:
        lo = start or date.min
        hi = end or date.max
        if lo <= splits.test_end and hi >= splits.test_start:
            raise ValidationError(
                f"range {start}..{end} touches the held-out test window "
                f"{splits.test_start}..{splits.test_end}; diagnostics must not read it"
            )
    return start, end


# --------------------------------------------------------- parameter sensitivity
@dataclass(frozen=True)
class SensitivityPoint:
    parameter: str | None
    value: object
    total_return: float
    sharpe: float | None
    is_base: bool


@dataclass(frozen=True)
class SensitivityReport:
    points: list
    base_return: float
    mean_return: float
    std_return: float
    min_return: float
    max_return: float
    fragile: bool
    rule: str
    fragile_points: list = field(default_factory=list)


def parameter_sensitivity(
    engine, strategy_spec: StrategySpec, history, config, grid: dict, *,
    start: date | None = None, end: date | None = None,
) -> SensitivityReport:
    """Move each parameter one notch either side of its base value, one at a time.

    Fragile rule: with a positive base return, a one-notch neighbour whose return
    is non-positive, or below (1 - COLLAPSE_FRACTION) of the base return, marks the
    result fragile. A base with no positive return has no edge to be fragile.
    """
    start, end = _resolve_range(config, start, end)
    base_score = _score(engine, strategy_spec, history, config, start, end)
    points = [SensitivityPoint(None, None, base_score.total_return, base_score.sharpe, True)]
    neighbours = []
    for name, values in grid.items():
        ordered = sorted(values)
        base_value = strategy_spec.params.get(name)
        if base_value not in ordered:
            raise ValidationError(
                f"base value {name}={base_value!r} is not on the grid {ordered}; "
                "the neighbourhood is defined around the base"
            )
        i = ordered.index(base_value)
        for j in (i - 1, i + 1):
            if 0 <= j < len(ordered):
                spec = strategy_spec.with_params(**{name: ordered[j]})
                s = _score(engine, spec, history, config, start, end)
                point = SensitivityPoint(name, ordered[j], s.total_return, s.sharpe, False)
                points.append(point)
                neighbours.append(point)

    returns = [p.total_return for p in points]
    base = base_score.total_return
    fragile_points: list = []
    if base <= 0:
        fragile = False
        rule = (f"no positive base return ({base:.4f}): there is no edge, so sensitivity "
                "of an edge is not meaningful")
    else:
        reasons = []
        for p in neighbours:
            if p.total_return <= 0:
                reasons.append(f"{p.parameter}={p.value} return {p.total_return:.4f} is non-positive "
                               f"against a base of {base:.4f}")
                fragile_points.append(p)
            elif p.total_return < (1 - COLLAPSE_FRACTION) * base:
                reasons.append(f"{p.parameter}={p.value} return {p.total_return:.4f} lost more than "
                               f"{COLLAPSE_FRACTION:.0%} of the base return {base:.4f}")
                fragile_points.append(p)
        fragile = bool(reasons)
        rule = ("fragile: " + "; ".join(reasons)) if fragile else (
            f"stable: every one-notch neighbour keeps a positive return of at least "
            f"{1 - COLLAPSE_FRACTION:.0%} of the base return {base:.4f}"
        )
    return SensitivityReport(
        points=points, base_return=base, mean_return=statistics.fmean(returns),
        std_return=statistics.stdev(returns) if len(returns) > 1 else 0.0,
        min_return=min(returns), max_return=max(returns),
        fragile=fragile, rule=rule, fragile_points=fragile_points,
    )


# ------------------------------------------------------------ cost sensitivity
@dataclass(frozen=True)
class CostRow:
    slippage_bps: Decimal
    commission_per_share: Decimal
    total_return: float


@dataclass(frozen=True)
class CostSensitivityReport:
    rows: list
    breakeven_slippage_bps: float | None
    breakeven_by_commission: dict
    edge_at_lowest_cost: bool
    edge_survives_all_levels: bool
    robust_to_10bps: bool
    note: str


def _breakeven(series: list[tuple[float, float]]) -> float | None:
    """Slippage at which return crosses zero, interpolated linearly. None if it never does."""
    if series[0][1] <= 0:
        return series[0][0]
    for (s0, r0), (s1, r1) in zip(series, series[1:]):
        if r1 <= 0:
            return s0 + (r0 / (r0 - r1)) * (s1 - s0)
    return None


def cost_sensitivity(
    engine, strategy_spec: StrategySpec, history, config,
    slippage_bps_levels: Sequence, commission_levels: Sequence, *,
    start: date | None = None, end: date | None = None,
) -> CostSensitivityReport:
    if not slippage_bps_levels or not commission_levels:
        raise ValidationError("cost_sensitivity needs at least one slippage level and one commission level")
    if any(Decimal(x) < 0 for x in list(slippage_bps_levels) + list(commission_levels)):
        raise ValidationError("cost levels must not be negative")
    start, end = _resolve_range(config, start, end)
    slippages = sorted(Decimal(x) for x in slippage_bps_levels)
    commissions = sorted(Decimal(x) for x in commission_levels)

    rows = []
    for c in commissions:
        for s in slippages:
            score = _score(engine, strategy_spec, history, config, start, end,
                           execution={"slippage_bps": s, "commission_per_share": c})
            rows.append(CostRow(s, c, score.total_return))

    by_commission = {}
    for c in commissions:
        series = [(float(r.slippage_bps), r.total_return) for r in rows if r.commission_per_share == c]
        by_commission[str(c)] = _breakeven(series)
    first_series = [(float(r.slippage_bps), r.total_return) for r in rows if r.commission_per_share == commissions[0]]
    breakeven = by_commission[str(commissions[0])]
    edge_at_lowest = first_series[0][1] > 0
    survives = breakeven is None
    robust = survives or breakeven > CHEAP_COST_BPS + 1e-9
    if survives:
        note = (f"edge survives every tested slippage level (up to {first_series[-1][0]:g} bps) "
                f"at commission {commissions[0]}")
    elif not edge_at_lowest:
        note = (f"no edge even at the lowest tested cost level ({first_series[0][0]:g} bps): "
                "return is already non-positive")
    else:
        note = f"edge disappears at about {breakeven:.2f} bps of slippage per side"
        if not robust:
            note += "; an edge that dies at 10 bps or less is not an edge"
    return CostSensitivityReport(rows, breakeven, by_commission, edge_at_lowest, survives, robust, note)


# --------------------------------------------------------------- monte carlo
@dataclass(frozen=True)
class MonteCarloResult:
    mode: str
    iterations: int
    seed: int
    n_trades: int
    starting_equity: float
    actual_terminal: float | None
    terminal_median: float | None
    terminal_p05: float | None
    terminal_p95: float | None
    drawdown_median: float | None
    drawdown_p05: float | None
    drawdown_p95: float | None
    probability_of_loss: float | None
    note: str


def _trade_pnls(result) -> list[float]:
    return [float(p - b) for b, p in _closed_trades(result.fills)]


def monte_carlo_trade_order(result, iterations: int, seed: int, *, mode: str = "shuffle") -> MonteCarloResult:
    """Resample realised closed-trade P&L (dollars) onto the starting equity.

    mode="shuffle" permutes the trades: it shows how bad the drawdown could have
    been with the same trades in another order. Terminal equity of a permutation
    is the same sum, so it cannot move. mode="bootstrap" draws trades with
    replacement, which also gives a terminal distribution. Drawdown is measured
    on realised (closed-trade) equity only.
    """
    if not isinstance(iterations, int) or iterations < 1:
        raise ValidationError(f"iterations must be a positive integer, got {iterations!r}")
    if mode not in ("shuffle", "bootstrap"):
        raise ValidationError(f"mode must be 'shuffle' or 'bootstrap', got {mode!r}")
    pnls = np.asarray(_trade_pnls(result), dtype=float)
    start = float(result.starting_cash)
    n = len(pnls)
    if n < 2:
        return MonteCarloResult(mode, iterations, seed, n, start, None, None, None, None,
                                None, None, None, None,
                                f"only {n} closed trade(s); resampling the order of fewer than 2 "
                                "trades says nothing")
    rng = np.random.default_rng(seed)
    if mode == "shuffle":
        draws = np.stack([rng.permutation(pnls) for _ in range(iterations)])
        note = ("Reordering realised trades does not change the terminal equity (it is the same "
                "sum); only the path and the drawdown vary. Use mode='bootstrap' for a terminal "
                "distribution.")
    else:
        draws = rng.choice(pnls, size=(iterations, n), replace=True)
        note = ("Trades drawn with replacement from the realised set: a terminal distribution "
                "under the assumption that future trades look like past ones.")
    paths = start + np.cumsum(draws, axis=1)
    peaks = np.maximum.accumulate(np.maximum(paths, start), axis=1)
    drawdowns = np.where(peaks > 0, (peaks - paths) / peaks, 0.0).max(axis=1)
    terminal = paths[:, -1]
    q = lambda a, p: float(np.quantile(a, p))
    return MonteCarloResult(
        mode=mode, iterations=iterations, seed=seed, n_trades=n, starting_equity=start,
        actual_terminal=start + float(pnls.sum()),
        terminal_median=q(terminal, 0.5), terminal_p05=q(terminal, 0.05), terminal_p95=q(terminal, 0.95),
        drawdown_median=q(drawdowns, 0.5), drawdown_p05=q(drawdowns, 0.05), drawdown_p95=q(drawdowns, 0.95),
        probability_of_loss=float((terminal < start).mean()), note=note,
    )


# ------------------------------------------------------------------ bootstrap
@dataclass(frozen=True)
class BootstrapInterval:
    point: float | None
    lower: float | None
    upper: float | None
    confidence: float
    iterations: int
    n: int
    seed: int
    note: str


def bootstrap_metric(
    returns, metric_fn: Callable, iterations: int, seed: int, confidence: float, *,
    block_length: int | None = None,
) -> BootstrapInterval:
    """Percentile bootstrap interval for `metric_fn(returns)`.

    IID by default; pass `block_length` (mean block length) for the stationary
    bootstrap of Politis and Romano (1994), which keeps serial dependence.
    Floor: below MIN_BOOTSTRAP_SAMPLE observations the interval is None, because
    resampling 19 points reproduces those 19 points and a band around them would
    be manufactured confidence.
    """
    if not (0 < confidence < 1):
        raise ValidationError(f"confidence must be strictly between 0 and 1, got {confidence}")
    if not isinstance(iterations, int) or iterations < 1:
        raise ValidationError(f"iterations must be a positive integer, got {iterations!r}")
    data = np.asarray([float(x) for x in returns], dtype=float)
    n = len(data)
    point = float(metric_fn(data)) if n else None
    if point is not None and not math.isfinite(point):
        point = None
    if n < MIN_BOOTSTRAP_SAMPLE:
        return BootstrapInterval(point, None, None, confidence, iterations, n, seed,
                                 f"{n} observation(s) is below the bootstrap floor of "
                                 f"{MIN_BOOTSTRAP_SAMPLE}; no interval is reported")
    rng = np.random.default_rng(seed)
    if block_length is None:
        idx = rng.integers(0, n, size=(iterations, n))
    else:
        if block_length < 1:
            raise ValidationError(f"block_length must be at least 1, got {block_length}")
        restart = rng.random((iterations, n)) < 1.0 / block_length
        starts = rng.integers(0, n, size=(iterations, n))
        idx = np.empty((iterations, n), dtype=int)
        idx[:, 0] = starts[:, 0]
        for t in range(1, n):
            idx[:, t] = np.where(restart[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    stats = np.array([metric_fn(data[row]) for row in idx], dtype=float)
    valid = stats[np.isfinite(stats)]
    if len(valid) < iterations / 2:
        return BootstrapInterval(point, None, None, confidence, iterations, n, seed,
                                 f"metric was undefined in {iterations - len(valid)} of "
                                 f"{iterations} resamples; no interval is reported")
    alpha = (1 - confidence) / 2
    lower, upper = np.quantile(valid, [alpha, 1 - alpha])
    return BootstrapInterval(point, float(lower), float(upper), confidence, iterations, n, seed, "")


# ------------------------------------------------------------- deflated sharpe
@dataclass(frozen=True)
class DeflatedSharpe:
    observed_sharpe: float
    n_trials: int
    n_observations: int
    expected_max_sharpe: float
    deflated_sharpe: float
    significant: bool
    message: str


def deflated_sharpe(
    observed_sharpe: float, n_trials: int, n_observations: int, *,
    periods_per_year: int = 252, skewness: float = 0.0, kurtosis: float = 3.0,
    trials_sharpe_variance: float | None = None,
) -> DeflatedSharpe:
    """Deflated Sharpe Ratio: the probability the best of `n_trials` is a real edge.

    Source: Bailey and Lopez de Prado, "The Deflated Sharpe Ratio: Correcting for
    Selection Bias, Backtest Overfitting and Non-Normality", Journal of Portfolio
    Management 40(5), 2014.

        SR0 = sqrt(V) * ((1 - g) * Z^-1(1 - 1/N) + g * Z^-1(1 - 1/(N e)))
        DSR = Z( (SR - SR0) * sqrt(T - 1) / sqrt(1 - skew*SR + (kurt - 1)/4 * SR^2) )

    with SR per-period, g the Euler-Mascheroni constant, N the number of trials,
    T the number of observations and Z the standard normal cdf. SR0 is the Sharpe
    the best of N pure-noise strategies is expected to show. V is the variance of
    the trials' Sharpe estimates; absent the real trial Sharpes it defaults to the
    null value 1/(T - 1). `observed_sharpe` is annualised and converted with
    `periods_per_year` (pass 1 if it is already per-period).

    The point: testing 50 variants and reporting the best one is not a discovery.
    A Sharpe that looks excellent as a single test can be exactly what noise
    delivers as a maximum.
    """
    if n_trials < 1:
        raise ValidationError(f"n_trials must be at least 1, got {n_trials}")
    if n_observations < 2:
        raise ValidationError(f"n_observations must be at least 2, got {n_observations}")
    scale = math.sqrt(periods_per_year)
    sr = observed_sharpe / scale
    variance = trials_sharpe_variance if trials_sharpe_variance is not None else 1.0 / (n_observations - 1)
    if n_trials == 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(variance) * (
            (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1.0 / n_trials)
            + EULER_GAMMA * _N.inv_cdf(1 - 1.0 / (n_trials * math.e))
        )
    denom = 1 - skewness * sr + (kurtosis - 1) / 4.0 * sr * sr
    if denom <= 0:
        raise ValidationError("skewness/kurtosis make the Sharpe variance term non-positive")
    dsr = _N.cdf((sr - sr0) * math.sqrt(n_observations - 1) / math.sqrt(denom))
    significant = dsr >= SIGNIFICANCE
    if n_trials == 1:
        message = (f"Single test: probability the Sharpe is real is {dsr:.1%}; no "
                   "multiple-testing adjustment applies.")
    else:
        message = (
            f"Testing {n_trials} variants and reporting the best one is not a discovery. "
            f"The best of {n_trials} pure-noise trials is expected to show a Sharpe of "
            f"{sr0 * scale:.2f}; observed {observed_sharpe:.2f}. After adjusting for "
            f"{n_trials} trials the probability the edge is real is {dsr:.1%} "
            f"({'clears' if significant else 'does not clear'} the {SIGNIFICANCE:.0%} bar)."
        )
    return DeflatedSharpe(observed_sharpe, n_trials, n_observations, sr0 * scale, dsr, significant, message)



def _as_int(value) -> int | None:
    """Coerce a metrics value to int. Registry JSON can hand back a string."""
    if value is None:
        return None
    try:
        return int(Decimal(str(value)))
    except Exception:
        return None


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except Exception:
        return None


@dataclass(frozen=True)
class MultipleTestingSummary:
    strategy_name: str
    n_variants: int
    n_experiments: int
    best_observed_sharpe: float | None
    expected_max_sharpe_under_null: float | None
    excess_over_null: float | None
    deflated_sharpe: DeflatedSharpe | None
    message: str


def multiple_testing_summary(registry, strategy_name: str) -> MultipleTestingSummary:
    """How many variants were really tried, and how much of the best one that explains.

    Variants are distinct parameter sets; re-running one under another window or
    cost assumption is not a new variant. The count comes from the registry, not
    from anyone's memory of how many things they tried.
    """
    records = registry.list_experiments(strategy_name=strategy_name)
    if not records:
        return MultipleTestingSummary(strategy_name, 0, 0, None, None, None, None,
                                      f"No experiments recorded for {strategy_name}.")
    n_variants = len({canonical_json(r.parameters) for r in records})
    sharpes = [v for v in (_as_float(r.metrics.get("sharpe")) for r in records) if v is not None]
    obs = [v for v in (_as_int(r.metrics.get("observations")) for r in records) if v is not None]
    t = int(statistics.median(obs)) if obs else 0
    if not sharpes or t < 2:
        return MultipleTestingSummary(
            strategy_name, n_variants, len(records), None, None, None, None,
            f"{n_variants} variant(s) tried for {strategy_name}, but the Sharpe is undefined "
            "for all of them, so no selection-bias estimate is possible.",
        )
    best = max(sharpes)
    dsr = deflated_sharpe(best, n_variants, t)
    return MultipleTestingSummary(
        strategy_name, n_variants, len(records), best, dsr.expected_max_sharpe,
        best - dsr.expected_max_sharpe, dsr,
        f"{n_variants} variant(s) tried for {strategy_name} across {len(records)} experiment(s). "
        + dsr.message,
    )

@dataclass(frozen=True)
class FamilyWiseSummary:
    """Selection bias across EVERY variant tried, not one strategy's variants.

    `multiple_testing_summary` counts per strategy. On a registry where each
    strategy had exactly one parameter set it reported 1 trial each and no
    adjustment, while five strategies had in fact been tried and the best of
    them reported. The question a researcher actually asks is "did any of the
    things I tried beat the benchmark", and its denominator is the number of
    things tried.
    """

    n_variants: int
    n_experiments: int
    n_strategies: int
    best_strategy: str | None
    best_observed_sharpe: float | None
    expected_max_sharpe_under_null: float | None
    excess_over_null: float | None
    deflated_sharpe: DeflatedSharpe | None
    message: str


def family_wise_summary(
    registry,
    *,
    window: str | None = None,
    n_variants_override: int | None = None,
) -> FamilyWiseSummary:
    """Deflate the best result in the registry by how many variants were tried.

    `window` restricts the comparison to one evaluation window, which is what
    you want when asking about out-of-sample results specifically.
    `n_variants_override` exists only so a test can contrast one trial against
    many; it is not for making a result look better.
    """
    records = [r for r in registry.list_experiments()]
    if window is not None:
        records = [r for r in records if getattr(r, "evaluated_window", None) == window]
    if not records:
        scope = f" for window {window!r}" if window else ""
        return FamilyWiseSummary(
            0, 0, 0, None, None, None, None, None,
            f"No experiments recorded{scope}; nothing to correct.",
        )

    variants = {(r.strategy_name, canonical_json(r.parameters)) for r in records}
    n_variants = n_variants_override or len(variants)
    strategies = {r.strategy_name for r in records}

    scored = [
        (value, r.strategy_name)
        for r, value in ((r, _as_float(r.metrics.get("sharpe"))) for r in records)
        if value is not None
    ]
    obs = [v for v in (_as_int(r.metrics.get("observations")) for r in records) if v is not None]
    t = int(statistics.median(obs)) if obs else 0
    if not scored or t < 2:
        return FamilyWiseSummary(
            n_variants, len(records), len(strategies), None, None, None, None, None,
            f"{n_variants} variant(s) tried across {len(strategies)} strategies, but "
            "the Sharpe is undefined for all of them, so no selection-bias estimate "
            "is possible.",
        )

    best, best_strategy = max(scored)
    dsr = deflated_sharpe(best, n_variants, t)
    verdict = (
        "survives the correction"
        if dsr.significant
        else "does NOT survive the correction: it is not distinguishable from the "
        "best of that many random tries"
    )
    return FamilyWiseSummary(
        n_variants, len(records), len(strategies), best_strategy, best,
        dsr.expected_max_sharpe, best - dsr.expected_max_sharpe, dsr,
        f"{n_variants} variant(s) tried across {len(strategies)} strategies. Best "
        f"observed Sharpe {best:.3f} ({best_strategy}); expected best under the null "
        f"with that many tries is {dsr.expected_max_sharpe:.3f}. It {verdict}.",
    )
