"""Circuit breakers and data-integrity kill switches.

Every function here returns a `(rule, reason)` pair when it trips, or None.

All of these block *risk-increasing* orders only. An exit is never blocked by
a breaker: a halted system that cannot reduce an existing position is more
dangerous than one that keeps trading, so the asymmetry is deliberate.
"""

from __future__ import annotations

from decimal import Decimal

from signalforge.config.models import RiskConfig
from signalforge.core.history import PointInTimeView

Trip = tuple[str, str] | None


def check_manual_pause(state) -> Trip:
    if state.paused:
        return (
            "manual_pause",
            f"paper trading is paused: {state.pause_reason or 'no reason recorded'}",
        )
    return None


def check_drawdown(state, config: RiskConfig) -> Trip:
    drawdown = state.drawdown_fraction
    if drawdown >= config.max_portfolio_drawdown:
        return (
            "breaker_drawdown",
            f"portfolio drawdown {drawdown:.2%} has reached the "
            f"{config.max_portfolio_drawdown:.2%} limit (peak equity "
            f"{state.peak_equity}, current {state.current_equity}); "
            "risk-increasing orders are refused",
        )
    return None


def check_daily_loss(state, config: RiskConfig) -> Trip:
    loss = state.daily_loss_fraction
    if loss >= config.max_daily_loss:
        return (
            "breaker_daily_loss",
            f"session loss {loss:.2%} has reached the {config.max_daily_loss:.2%} "
            f"limit (opened at {state.day_start_equity}, now "
            f"{state.current_equity}); risk-increasing orders are refused",
        )
    return None


def check_price_available(symbol: str, view: PointInTimeView) -> Trip:
    bar = view.latest(symbol)
    if bar is None:
        return (
            "invalid_price",
            f"no price for {symbol} at or before {view.as_of}; refusing to trade "
            "an instrument we cannot value",
        )
    if bar.close <= Decimal("0"):
        return ("invalid_price", f"{symbol} close is {bar.close}, which is not a tradeable price")
    return None


def check_staleness(symbol: str, view: PointInTimeView, config: RiskConfig) -> Trip:
    age = view.staleness_days(symbol)
    if age is None:
        return None
    if age > config.stale_data_max_age_days:
        bar = view.latest(symbol)
        return (
            "stale_data",
            f"last {symbol} bar is {bar.trade_date}, {age} days before {view.as_of}, "
            f"beyond the {config.stale_data_max_age_days}-day limit; the price we "
            "would trade on is not current",
        )
    return None


def check_liquidity(symbol: str, view: PointInTimeView) -> Trip:
    bar = view.latest(symbol)
    if bar is not None and not bar.volume_known:
        # No volume published for this instrument (an index series). The
        # check cannot be performed, so it is skipped and the run's
        # limitations say so. It is not silently treated as a pass.
        return None
    if bar is not None and bar.volume <= 0:
        return (
            "zero_volume_liquidity",
            f"{symbol} traded zero shares on {bar.trade_date}; an order could not "
            "have been filled",
        )
    return None
