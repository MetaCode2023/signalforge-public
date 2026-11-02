"""Scoring a cone against what actually happened.

Four tests, each answering a claim the demos make:

- **Coverage.** The 10-90 band claims 80%. Over many forecasts the realized
  close should land inside it about 80% of the time. Much less means the
  cone is overconfident; much more means it is too wide to be useful.
- **PIT histogram.** Where the realized close falls among the sampled paths,
  as a percentile. A calibrated forecaster spreads these evenly across ten
  bins. Piles at both ends mean the cone is too narrow.
- **CRPS** (continuous ranked probability score), the proper scoring rule
  for a whole distribution, in units of return. Lower is better, and the
  only fair comparison is against another forecaster on the same origins.
- **Does confidence predict accuracy?** Directional hit rate bucketed by
  path agreement and by spread. "Tight spread = confident" is a claim that
  the high-agreement buckets hit more often than the coin-flip buckets. If
  the buckets are flat, the confidence display is decoration.

Overlapping forecasts share outcomes, so a stride shorter than the horizon
inflates the count without adding independent evidence. The summary reports
both and the Wilson intervals use the non-overlapping count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from signalforge.core.errors import ValidationError
from signalforge.forecasting.cone import ConeReading, Direction, Regime, read_cone
from signalforge.forecasting.paths import PathForecast

PIT_BINS = 10
MIN_BUCKET_EVIDENCE = 30
"""A bucket with fewer called forecasts than this gets no lift figure. Same
floor as `min_trades_for_evidence` in research.yaml: three lucky calls are
not a regime that works."""
AGREEMENT_BUCKETS = ((0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.0001))


@dataclass(frozen=True, slots=True)
class ForecastOutcome:
    symbol: str
    as_of: date
    horizon: int
    forecaster_id: str
    reading: ConeReading
    realized_close: float
    pit: float
    crps: float
    """Sample CRPS of the terminal distribution, divided by today's close."""

    @property
    def realized_return(self) -> float:
        return self.realized_close / self.reading.last_close - 1.0

    @property
    def covered(self) -> bool:
        return self.reading.p10 <= self.realized_close <= self.reading.p90

    @property
    def hit(self) -> bool | None:
        """Did the majority of paths call the direction? None on an exact tie."""
        if self.realized_close == self.reading.last_close:
            return None
        went_up = self.realized_close > self.reading.last_close
        return went_up == (self.reading.direction is Direction.UP)


def sample_crps(samples: np.ndarray, observed: float) -> float:
    """CRPS of an empirical distribution: E|X - y| - 0.5 E|X - X'|.

    The pairwise term is computed from sorted samples in O(n log n):
    sum over i<j of (x_j - x_i) equals sum_k x_(k) * (2k - n + 1).
    """
    x = np.sort(np.asarray(samples, dtype=np.float64))
    n = x.size
    if n < 2:
        raise ValidationError("CRPS needs at least two samples")
    first = float(np.mean(np.abs(x - observed)))
    k = np.arange(n)
    pairwise = float(np.sum(x * (2 * k - n + 1))) / (n * n)
    return first - pairwise


def score(
    forecast: PathForecast,
    realized_closes,
    trend_agreement: float | None = None,
) -> ForecastOutcome:
    """Score one forecast. `realized_closes` are the `horizon` closes after as_of."""
    realized = np.asarray(realized_closes, dtype=np.float64)
    if realized.shape != (forecast.horizon,):
        raise ValidationError(
            f"need {forecast.horizon} realized closes after {forecast.as_of}, got {realized.shape}"
        )
    reading = read_cone(forecast) if trend_agreement is None else read_cone(forecast, trend_agreement)
    y = float(realized[-1])
    terminal = forecast.terminal
    below = float(np.sum(terminal < y))
    ties = float(np.sum(terminal == y))
    return ForecastOutcome(
        symbol=forecast.symbol,
        as_of=forecast.as_of,
        horizon=forecast.horizon,
        forecaster_id=forecast.forecaster_id,
        reading=reading,
        realized_close=y,
        pit=(below + 0.5 * ties) / forecast.n_paths,
        crps=sample_crps(terminal, y) / forecast.last_close,
    )


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (math.nan, math.nan)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _effective_n(n: int, stride: int, horizon: int) -> int:
    """Independent outcomes when consecutive forecasts overlap."""
    if stride >= horizon:
        return n
    return max(1, math.floor(n * stride / horizon))


@dataclass(frozen=True, slots=True)
class BucketStat:
    label: str
    n: int
    hits: int
    mean_realized_return: float
    interval: tuple[float, float]

    @property
    def hit_rate(self) -> float:
        return self.hits / self.n if self.n else math.nan


@dataclass(frozen=True, slots=True)
class TrialSummary:
    forecaster_id: str
    n: int
    n_independent: int
    coverage: float
    coverage_interval: tuple[float, float]
    mean_crps: float
    hit_rate: float
    hit_interval: tuple[float, float]
    base_rate_up: float
    """Share of outcomes that went up. A forecaster that always says UP scores
    exactly this, so a hit rate is only interesting above it."""
    mean_spread: float
    trending_share: float
    pit_histogram: tuple[float, ...]
    by_agreement: tuple[BucketStat, ...]
    by_regime: tuple[BucketStat, ...]
    by_spread: tuple[BucketStat, ...]
    notes: tuple[str, ...] = field(default=())

    @property
    def confidence_lift(self) -> float:
        """Hit rate when TRENDING minus hit rate when CHOPPY.

        The demos' whole claim in one number. Near zero means the regime
        label carries no information about whether the call is right.
        """
        buckets = {b.label: b for b in self.by_regime}
        trending = buckets.get(Regime.TRENDING.value)
        choppy = buckets.get(Regime.CHOPPY.value)
        if trending is None or choppy is None:
            return math.nan
        if min(trending.n, choppy.n) < MIN_BUCKET_EVIDENCE:
            return math.nan
        return trending.hit_rate - choppy.hit_rate


def _bucket(label: str, outcomes: list[ForecastOutcome], scale: float) -> BucketStat:
    called = [o for o in outcomes if o.hit is not None]
    hits = sum(1 for o in called if o.hit)
    n_eff = max(1, round(len(called) * scale)) if called else 0
    hits_eff = round(hits * scale) if called else 0
    return BucketStat(
        label=label,
        n=len(called),
        hits=hits,
        mean_realized_return=float(np.mean([o.realized_return for o in called])) if called else math.nan,
        interval=wilson(min(hits_eff, n_eff), n_eff),
    )


def summarize(
    outcomes: list[ForecastOutcome], *, stride: int, horizon: int
) -> TrialSummary:
    if not outcomes:
        raise ValidationError("nothing to summarise: the trial produced no forecasts")
    ids = {o.forecaster_id for o in outcomes}
    if len(ids) != 1:
        raise ValidationError(f"one forecaster per summary, got {sorted(ids)}")
    n = len(outcomes)
    n_eff = _effective_n(n, stride, horizon)
    scale = n_eff / n

    covered = sum(o.covered for o in outcomes)
    called = [o for o in outcomes if o.hit is not None]
    hits = sum(1 for o in called if o.hit)
    pit = np.histogram([o.pit for o in outcomes], bins=PIT_BINS, range=(0.0, 1.0))[0] / n

    by_agreement = []
    for lo, hi in AGREEMENT_BUCKETS:
        members = [o for o in outcomes if lo <= o.reading.agreement < hi]
        label = f"{round(lo * 100)}-{min(100, round(hi * 100))}% agree"
        by_agreement.append(_bucket(label, members, scale))

    by_regime = [
        _bucket(r.value, [o for o in outcomes if o.reading.regime is r], scale) for r in Regime
    ]

    spreads = np.array([o.reading.spread_pct for o in outcomes])
    cuts = np.quantile(spreads, [1 / 3, 2 / 3])
    tight = [o for o in outcomes if o.reading.spread_pct <= cuts[0]]
    middle = [o for o in outcomes if cuts[0] < o.reading.spread_pct <= cuts[1]]
    wide = [o for o in outcomes if o.reading.spread_pct > cuts[1]]
    by_spread = [
        _bucket("tight spread", tight, scale),
        _bucket("middle spread", middle, scale),
        _bucket("wide spread", wide, scale),
    ]

    notes = []
    if n_eff < n:
        notes.append(
            f"{n} forecasts overlap (stride {stride} < horizon {horizon}); intervals use "
            f"{n_eff} effectively independent outcomes."
        )
    return TrialSummary(
        forecaster_id=next(iter(ids)),
        n=n,
        n_independent=n_eff,
        coverage=covered / n,
        coverage_interval=wilson(round(covered * scale), n_eff),
        mean_crps=float(np.mean([o.crps for o in outcomes])),
        hit_rate=hits / len(called) if called else math.nan,
        hit_interval=wilson(round(hits * scale), max(1, round(len(called) * scale))),
        base_rate_up=float(np.mean([o.realized_return > 0 for o in outcomes])),
        mean_spread=float(np.mean(spreads)),
        trending_share=float(np.mean([o.reading.regime is Regime.TRENDING for o in outcomes])),
        pit_histogram=tuple(float(x) for x in pit),
        by_agreement=tuple(by_agreement),
        by_regime=tuple(by_regime),
        by_spread=tuple(by_spread),
        notes=tuple(notes),
    )


def crps_skill(model: list[ForecastOutcome], null: list[ForecastOutcome]) -> float:
    """1 - CRPS_model / CRPS_null on the origins both forecasters scored.

    Positive means the model's distribution beat the null's; 0.02 is a 2%
    improvement. Only matched (symbol, as_of) pairs count, so a forecaster
    cannot win by skipping the hard days.
    """
    a = {(o.symbol, o.as_of): o.crps for o in model}
    b = {(o.symbol, o.as_of): o.crps for o in null}
    keys = sorted(a.keys() & b.keys())
    if not keys:
        raise ValidationError("no common forecast origins to compare")
    return 1.0 - float(np.mean([a[k] for k in keys])) / float(np.mean([b[k] for k in keys]))
