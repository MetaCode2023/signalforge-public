"""The Assay study: four ways to copy insiders, two fair coins, one market.

Every copied trade enters at the close of the first trading day AFTER the
filing date (the filing time of day is not in the data, so a filing made
after the close cannot be traded the same day) and exits a fixed number of
sessions later. Its score is its return minus SPY's over the same days,
minus 5 bps each way.

Strategies ("arms"):
- all: every qualifying purchase (one per company per day).
- cluster: two or more different insiders buying the same company within 30
  days, signalled on the filing that makes it two.
- smart_past: insiders whose earlier copied buys, judged only on outcomes
  already known when they buy again, won often enough. The honest version
  of "copy the winners".
- smart_hindsight: insiders picked by how all their buys in the window
  turned out, then copied over that same window. This is the copy-trading
  pitch's method. It uses the future, it is labelled as cheating, and it is
  here to show how good cheating looks.

Fair coins ("nulls"):
- random stock, same days: the copied entry and exit dates, a random
  warehouse stock instead of the insider's. The warehouse holds only
  companies that survived to 2026, so this is the drift every arm gets for
  free.
- random insiders, same months: as many purchases as an arm, drawn from all
  purchases filed in the same months. An arm that does not beat this has
  found nothing beyond "an insider bought".

Statistics are float64: these are research measurements, not money.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from signalforge.core.errors import ValidationError

TRADING_DAYS = 252
MIN_EVIDENCE = 30


@dataclass(frozen=True, eq=False)
class PricePanel:
    days: tuple[date, ...]
    symbols: tuple[str, ...]
    adj: np.ndarray
    """[days, symbols] adjusted closes, NaN where there is no bar."""
    close: np.ndarray
    """[days, symbols] raw closes, for checking a filed price against the market."""

    def __post_init__(self) -> None:
        shape = (len(self.days), len(self.symbols))
        if self.adj.shape != shape or self.close.shape != shape:
            raise ValidationError(f"price arrays must be {shape}")
        if any(b <= a for a, b in zip(self.days, self.days[1:])):
            raise ValidationError("panel days must be strictly increasing")
        object.__setattr__(self, "_col", {s: i for i, s in enumerate(self.symbols)})

    def col(self, symbol: str) -> int | None:
        return self._col.get(symbol)

    def on_or_after(self, day: date) -> int | None:
        i = bisect.bisect_left(self.days, day)
        return i if i < len(self.days) else None

    def on_or_before(self, day: date) -> int | None:
        i = bisect.bisect_right(self.days, day) - 1
        return i if i >= 0 else None


def load_panel(warehouse, symbols, start: date, end: date, source: str | None = None) -> PricePanel:
    """Adjusted and raw closes straight from DuckDB (no Decimal bars needed here)."""
    symbols = sorted(set(symbols))
    if not symbols:
        raise ValidationError("no symbols to load")
    marks = ", ".join("?" * len(symbols))
    sql = (f"SELECT symbol, trade_date, close, adj_close, source FROM bars WHERE symbol IN ({marks}) "
           "AND trade_date BETWEEN ? AND ?")
    params = [*symbols, start, end]
    if source:
        sql += " AND source = ?"
        params.append(source)
    rows = warehouse.con.execute(sql, params).fetchall()
    if not rows:
        raise ValidationError(f"no bars for these symbols between {start} and {end}")
    seen = set()
    for sym, day, *_ in rows:
        if (sym, day) in seen:
            raise ValidationError(
                f"{sym} has two bars on {day} from different sources; pass --source to choose one"
            )
        seen.add((sym, day))
    days = tuple(sorted({r[1] for r in rows}))
    present = sorted({r[0] for r in rows})
    di, si = {d: i for i, d in enumerate(days)}, {s: i for i, s in enumerate(present)}
    adj = np.full((len(days), len(present)), np.nan)
    close = np.full_like(adj, np.nan)
    for sym, day, c, a, _src in rows:
        close[di[day], si[sym]] = float(c)
        adj[di[day], si[sym]] = float(a if a is not None else c)
    return PricePanel(days=days, symbols=tuple(present), adj=adj, close=close)


@dataclass(frozen=True)
class Scored:
    """One purchase, placed on the trading calendar and scored per horizon."""

    accession: str
    symbol: str
    owner_cik: str
    filing_date: date
    signal: int
    entry: int
    exits: tuple[int, ...]
    excess: tuple[float, ...]
    """Net excess return over SPY per horizon; NaN when the exit is past the data."""
    gross: tuple[float, ...]


@dataclass(frozen=True)
class Params:
    window_start: date
    window_end: date
    horizons: tuple[int, ...] = (5, 21, 63)
    primary: int = 21
    cost_bps: float = 5.0
    min_history: int = 3
    min_hit: float = 0.6
    cluster_days: int = 30
    draws: int = 200
    seed: int = 20261101
    benchmark: str = "SPY"
    jump_limit: float = 1.4
    price_band: tuple[float, float] = (0.5, 2.0)

    def __post_init__(self) -> None:
        if self.primary not in self.horizons:
            raise ValidationError(f"primary horizon {self.primary} must be one of {self.horizons}")
        if self.window_end <= self.window_start:
            raise ValidationError("study window is empty")
        if not (0 < self.min_hit <= 1) or self.min_history < 1:
            raise ValidationError("min_hit must be in (0, 1] and min_history at least 1")


def score(purchases, panel: PricePanel, p: Params, acknowledged=frozenset()) -> tuple[list[Scored], Counter]:
    """Place each purchase on the calendar. Purchases before the window are kept as history."""
    bench = panel.col(p.benchmark)
    if bench is None:
        raise ValidationError(f"benchmark {p.benchmark} is not in the price panel")
    end_idx = panel.on_or_before(p.window_end)
    cost = 2 * p.cost_bps / 1e4
    longest = max(p.horizons)
    out, dropped = [], Counter()
    for pur in purchases:
        if pur.filing_date > p.window_end:
            continue
        c = panel.col(pur.symbol)
        sig = panel.on_or_after(pur.filing_date)
        if c is None or sig is None or end_idx is None or sig + 1 > end_idx:
            dropped["no trading day after the filing inside the data"] += 1
            continue
        entry = sig + 1
        t = panel.on_or_before(pur.trans_date)
        ref = panel.close[t, c] if t is not None else np.nan
        if not np.isfinite(ref):
            dropped["no market price on the trade date"] += 1
            continue
        lo, hi = p.price_band
        if not (lo <= pur.price / ref <= hi):
            dropped["filed price far from the market close"] += 1
            continue
        if not np.isfinite(panel.adj[entry, c]) or not np.isfinite(panel.adj[entry, bench]):
            dropped["no price at entry"] += 1
            continue
        stop = min(entry + longest, end_idx)
        seg = panel.adj[entry:stop + 1, c]
        ratios = seg[1:] / seg[:-1]
        bad = np.where(np.isfinite(ratios) & ((ratios > p.jump_limit) | (ratios < 1 / p.jump_limit)))[0]
        if any((pur.symbol, panel.days[entry + 1 + int(k)]) not in acknowledged for k in bad):
            dropped["unexplained price jump while held"] += 1
            continue
        exits, excess, gross = [], [], []
        for h in p.horizons:
            x = entry + h
            exits.append(x)
            if x > end_idx or not np.isfinite(panel.adj[x, c]) or not np.isfinite(panel.adj[x, bench]):
                excess.append(math.nan)
                gross.append(math.nan)
                continue
            r = panel.adj[x, c] / panel.adj[entry, c] - 1
            m = panel.adj[x, bench] / panel.adj[entry, bench] - 1
            gross.append(float(r))
            excess.append(float(r - m - cost))
        out.append(Scored(pur.accession, pur.symbol, pur.owner_cik, pur.filing_date, sig, entry,
                          tuple(exits), tuple(excess), tuple(gross)))
    return out, dropped


def _in_window(e: Scored, panel: PricePanel, p: Params) -> bool:
    return p.window_start <= panel.days[e.signal] <= p.window_end


def _dedupe(events) -> list[Scored]:
    seen, out = set(), []
    for e in sorted(events, key=lambda e: (e.signal, e.symbol, e.accession)):
        if (e.symbol, e.signal) not in seen:
            seen.add((e.symbol, e.signal))
            out.append(e)
    return out


def arm_all(events, panel, p) -> list[Scored]:
    return _dedupe(e for e in events if _in_window(e, panel, p))


def arm_smart_past(events, panel, p, hi: int) -> list[Scored]:
    """Copy an insider only if their closed record, as of this buy, qualifies.

    A past buy counts only if its exit date is before this buy's signal day,
    so the decision never uses an outcome that was not yet known.
    """
    by_owner = defaultdict(list)
    for e in sorted(events, key=lambda e: e.signal):
        by_owner[e.owner_cik].append(e)
    chosen = []
    for owner_events in by_owner.values():
        for e in owner_events:
            if not _in_window(e, panel, p):
                continue
            past = [x.excess[hi] for x in owner_events
                    if x.exits[hi] < e.signal and math.isfinite(x.excess[hi])]
            if len(past) < p.min_history:
                continue
            hits = sum(1 for v in past if v > 0) / len(past)
            if hits >= p.min_hit and float(np.mean(past)) > 0:
                chosen.append(e)
    return _dedupe(chosen)


def arm_smart_hindsight(events, panel, p, hi: int) -> list[Scored]:
    """The pitch's method: rank insiders on outcomes they had not had yet. Cheating."""
    by_owner = defaultdict(list)
    for e in events:
        if _in_window(e, panel, p) and math.isfinite(e.excess[hi]):
            by_owner[e.owner_cik].append(e)
    chosen = []
    for owner_events in by_owner.values():
        vals = [x.excess[hi] for x in owner_events]
        if len(vals) >= p.min_history and sum(v > 0 for v in vals) / len(vals) >= p.min_hit and np.mean(vals) > 0:
            chosen.extend(owner_events)
    return _dedupe(chosen)


def arm_cluster(events, panel, p, hi: int) -> list[Scored]:
    """Two or more insiders buying one company within `cluster_days`."""
    by_symbol = defaultdict(list)
    for e in events:
        by_symbol[e.symbol].append(e)
    chosen = []
    span = timedelta(days=p.cluster_days)
    for sym_events in by_symbol.values():
        sym_events.sort(key=lambda e: (e.filing_date, e.accession))
        busy_until = -1
        for i, e in enumerate(sym_events):
            owners = {x.owner_cik for x in sym_events[: i + 1] if e.filing_date - span < x.filing_date <= e.filing_date}
            if len(owners) >= 2 and e.signal > busy_until and _in_window(e, panel, p):
                chosen.append(e)
                busy_until = e.exits[hi]
    return _dedupe(chosen)


@dataclass(frozen=True)
class ArmStats:
    n: int
    mean: float
    median: float
    hit_rate: float
    t_naive: float
    evidence: str


def stats(events, h: int) -> ArmStats:
    vals = np.array([e.excess[h] for e in events if math.isfinite(e.excess[h])])
    n = int(vals.size)
    if n == 0:
        return ArmStats(0, math.nan, math.nan, math.nan, math.nan, "INSUFFICIENT_EVIDENCE")
    sd = float(vals.std(ddof=1)) if n > 1 else math.nan
    t = float(vals.mean() / (sd / math.sqrt(n))) if n > 1 and sd > 0 else math.nan
    return ArmStats(n, float(vals.mean()), float(np.median(vals)), float((vals > 0).mean()), t,
                    "SUFFICIENT" if n >= MIN_EVIDENCE else "INSUFFICIENT_EVIDENCE")


def null_random_insiders(arm, pool, panel, h: int, draws: int, rng) -> np.ndarray:
    """Means of `draws` same-size, same-months draws from all purchases."""
    month = lambda e: (panel.days[e.signal].year, panel.days[e.signal].month)  # noqa: E731
    need = Counter(month(e) for e in arm if math.isfinite(e.excess[h]))
    by_month = defaultdict(list)
    for e in pool:
        if math.isfinite(e.excess[h]):
            by_month[month(e)].append(e.excess[h])
    pools = {m: np.array(by_month[m]) for m in need}
    if not need:
        return np.array([])
    out = np.empty(draws)
    for d in range(draws):
        picked = [rng.choice(pools[m], size=min(k, pools[m].size), replace=False) for m, k in need.items()]
        out[d] = float(np.concatenate(picked).mean())
    return out


def null_random_stock(arm, panel, p: Params, h: int, draws: int, rng) -> np.ndarray:
    """Same entry and exit days, a random warehouse stock. Survivorship drift, measured."""
    bench = panel.col(p.benchmark)
    cost = 2 * p.cost_bps / 1e4
    usable = [c for c, s in enumerate(panel.symbols) if s != p.benchmark]
    rows = []
    for e in arm:
        if not math.isfinite(e.excess[h]):
            continue
        x = e.exits[h]
        a, b = panel.adj[e.entry, usable], panel.adj[x, usable]
        ok = np.where(np.isfinite(a) & np.isfinite(b))[0]
        if ok.size == 0:
            continue
        pick = ok[rng.integers(0, ok.size, size=draws)]
        m = panel.adj[x, bench] / panel.adj[e.entry, bench] - 1
        rows.append(b[pick] / a[pick] - 1 - m - cost)
    return np.vstack(rows).mean(axis=0) if rows else np.array([])


@dataclass
class Portfolio:
    days: list[str]
    equity: list[float]
    benchmark: list[float]
    total_return: float
    benchmark_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    invested_share: float
    avg_positions: float
    alpha_annual: float
    alpha_t: float


def portfolio(arm, panel, p: Params, h: int, start_cash: float = 100_000.0, points: int = 240) -> Portfolio:
    """Calendar-time book: hold every live copy equally weighted, cash when none.

    Daily rebalancing to equal weight is an idealisation; costs are charged
    on each position's first and last day. The daily excess series on days
    with at least one position gives an alpha and t-statistic that are not
    inflated by overlapping holding periods the way per-trade averages are.
    """
    bench = panel.col(p.benchmark)
    s, t = panel.on_or_after(p.window_start), panel.on_or_before(p.window_end)
    with np.errstate(invalid="ignore", divide="ignore"):
        daily = panel.adj[1:] / panel.adj[:-1] - 1
    daily = np.vstack([np.full((1, daily.shape[1]), np.nan), daily])
    total = np.zeros(len(panel.days))
    count = np.zeros(len(panel.days))
    each = p.cost_bps / 1e4
    for e in arm:
        x = e.exits[h]
        if not math.isfinite(e.excess[h]):
            continue
        c = panel.col(e.symbol)
        seg = daily[e.entry + 1:x + 1, c].copy()
        seg = np.where(np.isfinite(seg), seg, 0.0)
        seg[0] -= each
        seg[-1] -= each
        total[e.entry + 1:x + 1] += seg
        count[e.entry + 1:x + 1] += 1
    window = slice(s + 1, t + 1)
    with np.errstate(invalid="ignore"):
        book = np.where(count[window] > 0, total[window] / np.maximum(count[window], 1), 0.0)
    mkt = np.nan_to_num(daily[window, bench])
    equity = start_cash * np.cumprod(1 + book)
    bench_eq = start_cash * np.cumprod(1 + mkt)
    invested = count[window] > 0
    years = max(len(book) / TRADING_DAYS, 1e-9)
    peak = np.maximum.accumulate(equity)
    ex = (book - mkt)[invested]
    alpha_t = float(ex.mean() / (ex.std(ddof=1) / math.sqrt(ex.size))) if ex.size > 1 and ex.std(ddof=1) > 0 else math.nan
    step = max(1, len(book) // points)
    idx = list(range(0, len(book), step)) + ([len(book) - 1] if (len(book) - 1) % step else [])
    day_labels = panel.days[s + 1:t + 1]
    return Portfolio(
        days=[day_labels[i].isoformat() for i in idx],
        equity=[round(float(equity[i]), 2) for i in idx],
        benchmark=[round(float(bench_eq[i]), 2) for i in idx],
        total_return=float(equity[-1] / start_cash - 1),
        benchmark_return=float(bench_eq[-1] / start_cash - 1),
        cagr=float((equity[-1] / start_cash) ** (1 / years) - 1),
        sharpe=float(book.mean() / book.std(ddof=1) * math.sqrt(TRADING_DAYS)) if book.std(ddof=1) > 0 else math.nan,
        max_drawdown=float((1 - equity / peak).max()),
        invested_share=float(invested.mean()),
        avg_positions=float(count[window][invested].mean()) if invested.any() else 0.0,
        alpha_annual=float(ex.mean() * TRADING_DAYS) if ex.size else math.nan,
        alpha_t=alpha_t,
    )


@dataclass
class StudyResult:
    params: Params
    arms: dict[str, dict[int, ArmStats]] = field(default_factory=dict)
    nulls: dict[str, dict] = field(default_factory=dict)
    portfolios: dict[str, Portfolio] = field(default_factory=dict)
    funnel: list[tuple[str, int]] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    scored: int = 0


ARMS = ("all", "cluster", "smart_past", "smart_hindsight")


def run(purchases, panel: PricePanel, p: Params, acknowledged=frozenset()) -> StudyResult:
    rng = np.random.default_rng(p.seed)
    hi = p.horizons.index(p.primary)
    scored, excluded = score(purchases, panel, p, acknowledged)
    arms = {
        "all": arm_all(scored, panel, p),
        "cluster": arm_cluster(scored, panel, p, hi),
        "smart_past": arm_smart_past(scored, panel, p, hi),
        "smart_hindsight": arm_smart_hindsight(scored, panel, p, hi),
    }
    result = StudyResult(params=p, excluded=dict(excluded.most_common()), scored=len(scored))
    result.sizes = {k: len(v) for k, v in arms.items()}
    for name, events in arms.items():
        result.arms[name] = {h: stats(events, i) for i, h in enumerate(p.horizons)}
        result.portfolios[name] = portfolio(events, panel, p, hi)
    stock_null = null_random_stock(arms["all"], panel, p, hi, p.draws, rng)
    result.nulls["random_stock_same_days"] = _null_summary(stock_null, result.arms["all"][p.primary].mean)
    for name in ("smart_past", "cluster", "smart_hindsight"):
        draws = null_random_insiders(arms[name], arms["all"], panel, hi, p.draws, rng)
        result.nulls[f"random_insiders_vs_{name}"] = _null_summary(draws, result.arms[name][p.primary].mean)
    return result


def _null_summary(draws: np.ndarray, observed: float) -> dict:
    if draws.size == 0 or not math.isfinite(observed):
        return {"draws": int(draws.size), "observed": observed, "p_value": math.nan}
    lo, mid, hi = np.quantile(draws, [0.025, 0.5, 0.975])
    return {
        "draws": int(draws.size), "observed": observed, "median": float(mid), "lo": float(lo), "hi": float(hi),
        "p_value": float((np.sum(draws >= observed) + 1) / (draws.size + 1)),
        "histogram": np.histogram(draws, bins=24)[0].tolist(),
        "edges": [float(v) for v in np.histogram_bin_edges(draws, bins=24)],
    }
