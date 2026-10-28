"""Point-in-time market data access.

The structural defence against look-ahead bias. `PriceHistory` holds every
bar; `PointInTimeView` exposes only the bars dated at or before its `as_of`.
Strategies are handed the view and never the history.

To be precise about what that does and does not guarantee, because the
distinction matters: EVERY accessor on the view is truncated at `as_of`
(`bars`, `closes`, `adjusted_closes`, `returns`, `latest`, `has_history`,
`cross_section`, `marks`, `staleness_days` all route through
`_visible_bars`, and `bar()` raises on a future date). A strategy using the
view's API cannot see the future. The view does hold a reference to the
history in `_history`, so code that deliberately reaches through that
private attribute can read anything; the guarantee is on the interface, not
on attribute hiding. `tests/test_point_in_time.py` pins the accessors, and
`tests/test_engine.py` proves a strategy that tries `view.bar(sym, future)`
raises rather than returning a number.

Everything returned is an immutable tuple, so a caller cannot reach back into
the underlying series and edit it.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from signalforge.core.errors import LookAheadError, ValidationError
from signalforge.core.types import Bar

SYMBOL_SAMPLE_SIZE = 8


def summarise_symbols(symbols, sample: int = SYMBOL_SAMPLE_SIZE) -> str:
    """Name a symbol collection without inlining all of it.

    A 6,799-symbol cross-section interpolated into a message produced a 52KB
    string, and that string travelled into `BacktestResult.data_limitations`,
    the reports and the dashboard page. A count plus a sample identifies the
    universe just as well and stays readable.
    """
    items = list(symbols)
    if len(items) <= sample:
        return f"{len(items)} symbol(s): {', '.join(str(s) for s in items)}"
    head = ", ".join(str(s) for s in items[:sample])
    return f"{len(items)} symbols: {head}, ... (+{len(items) - sample} more)"


@dataclass(frozen=True, slots=True)
class PointInTimeView:
    """A read-only window onto history, truncated at `as_of` inclusive."""

    _history: PriceHistory
    as_of: date

    @property
    def trading_days(self) -> tuple[date, ...]:
        return self._history._days_upto(self.as_of)

    def bar(self, symbol: str, day: date) -> Bar:
        """Return one bar. Raises LookAheadError if it is after `as_of`."""
        if day > self.as_of:
            raise LookAheadError(
                f"attempted to read {symbol} on {day} from a view as of {self.as_of}"
            )
        bar = self._history._bars.get((symbol, day))
        if bar is None:
            raise KeyError(f"no bar for {symbol} on {day}")
        return bar

    def bars(self, symbol: str, lookback: int | None = None) -> tuple[Bar, ...]:
        series = self._history._visible_bars(symbol, self.as_of)
        if lookback is not None:
            if lookback < 1:
                raise ValidationError(f"lookback must be positive, got {lookback}")
            series = series[-lookback:]
        return series

    def closes(self, symbol: str, lookback: int | None = None) -> tuple[Decimal, ...]:
        return tuple(b.close for b in self.bars(symbol, lookback))

    def adjusted_closes(self, symbol: str, lookback: int | None = None) -> tuple[Decimal, ...]:
        """Adjusted closes, falling back to raw close where none is published.

        The fallback is explicit rather than silent: `Bar.adj_close is None`
        means the provider published no adjustment, and using the raw close is
        the honest treatment of that.
        """
        return tuple(
            (b.adj_close if b.adj_close is not None else b.close)
            for b in self.bars(symbol, lookback)
        )

    def returns(self, symbol: str, lookback: int | None = None) -> tuple[Decimal, ...]:
        """Simple period returns from adjusted closes visible at `as_of`."""
        prices = self.adjusted_closes(symbol, None if lookback is None else lookback + 1)
        return tuple(
            (prices[i] / prices[i - 1]) - Decimal(1)
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        )

    def latest(self, symbol: str) -> Bar | None:
        """The most recent bar at or before `as_of`, or None if there is none."""
        series = self._history._visible_bars(symbol, self.as_of)
        return series[-1] if series else None

    def has_history(self, symbol: str, required: int) -> bool:
        return len(self._history._visible_bars(symbol, self.as_of)) >= required

    def cross_section(self) -> tuple[str, ...]:
        """Symbols that have at least one bar at or before `as_of`."""
        return tuple(
            s for s in self._history.symbols if self._history._visible_bars(s, self.as_of)
        )

    def marks(self) -> dict[str, Decimal]:
        """Last known close per symbol, for valuing the book at `as_of`."""
        out: dict[str, Decimal] = {}
        for symbol in self._history.symbols:
            bar = self.latest(symbol)
            if bar is not None:
                out[symbol] = bar.close
        return out

    def staleness_days(self, symbol: str) -> int | None:
        """Calendar days between `as_of` and the symbol's last known bar."""
        bar = self.latest(symbol)
        if bar is None:
            return None
        return (self.as_of - bar.trade_date).days


class PriceHistory:
    """Every bar the backtest is allowed to know about, indexed for fast views.

    Construction is the only place the full series is visible. After that,
    access is through `view(as_of)`.
    """

    __slots__ = ("_bars", "_by_symbol", "_symbol_days", "_days", "symbols", "trading_days")

    def __init__(
        self,
        bars: dict[tuple[str, date], Bar],
        by_symbol: dict[str, tuple[Bar, ...]],
        days: tuple[date, ...],
    ) -> None:
        self._bars = bars
        self._by_symbol = by_symbol
        self._symbol_days = {s: tuple(b.trade_date for b in v) for s, v in by_symbol.items()}
        self._days = days
        self.symbols = tuple(sorted(by_symbol))
        self.trading_days = days

    @classmethod
    def from_bars(cls, bars: Iterable[Bar]) -> PriceHistory:
        indexed: dict[tuple[str, date], Bar] = {}
        for bar in bars:
            key = (bar.symbol, bar.trade_date)
            if key in indexed:
                raise ValidationError(
                    f"duplicate bar for {bar.symbol} on {bar.trade_date}; de-duplicate "
                    "before building history so the engine never sees two truths"
                )
            indexed[key] = bar
        by_symbol: dict[str, list[Bar]] = {}
        for (symbol, _), bar in indexed.items():
            by_symbol.setdefault(symbol, []).append(bar)
        frozen = {
            symbol: tuple(sorted(items, key=lambda b: b.trade_date))
            for symbol, items in by_symbol.items()
        }
        days = tuple(sorted({d for _, d in indexed}))
        return cls(indexed, frozen, days)

    def __len__(self) -> int:
        return len(self._bars)

    def _days_upto(self, as_of: date) -> tuple[date, ...]:
        cut = bisect.bisect_right(self._days, as_of)
        return self._days[:cut]

    def _visible_bars(self, symbol: str, as_of: date) -> tuple[Bar, ...]:
        series = self._by_symbol.get(symbol)
        if not series:
            return ()
        cut = bisect.bisect_right(self._symbol_days[symbol], as_of)
        return series[:cut]

    def view(self, as_of: date) -> PointInTimeView:
        if not isinstance(as_of, date):
            raise ValidationError(f"as_of must be a date, got {as_of!r}")
        return PointInTimeView(_history=self, as_of=as_of)

    def next_trading_day(self, after: date) -> date | None:
        """The first trading day strictly after `after`, or None at the end.

        This is how the engine turns a signal date into a fill date without
        assuming the calendar: it uses the days that actually have data.
        """
        index = bisect.bisect_right(self._days, after)
        if index >= len(self._days):
            return None
        return self._days[index]

    def all_bars(self) -> tuple[Bar, ...]:
        return tuple(self._bars.values())

    def bars_for(self, symbol: str) -> tuple[Bar, ...]:
        return self._by_symbol.get(symbol, ())
