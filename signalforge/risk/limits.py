"""Quantity caps.

Each function answers one question: given this book and this price, what is
the largest number of shares this rule permits? The engine takes the minimum
across all of them, so the tightest rule binds and is named in the decision.

A cap of 0 means the rule refuses the trade outright.
"""

from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal

from signalforge.config.models import ExecutionConfig, RiskConfig
from signalforge.core.history import PointInTimeView
from signalforge.core.types import Portfolio

ZERO = Decimal("0")

# Sizing the per-trade risk budget needs a loss-per-share estimate. We use a
# volatility proxy rather than a hard stop price, because a daily-bar backtest
# cannot honestly promise a stop would have filled at its trigger.
RISK_VOL_WINDOW = 20
STOP_SIGMA_MULTIPLE = Decimal("2")


def _floor_div(numerator: Decimal, denominator: Decimal) -> int:
    if denominator <= ZERO:
        return 0
    if numerator <= ZERO:
        return 0
    return int((numerator / denominator).to_integral_value(rounding=ROUND_FLOOR))


def single_name_cap(
    symbol: str,
    price: Decimal,
    equity: Decimal,
    portfolio: Portfolio,
    config: RiskConfig,
    reserved: int = 0,
) -> int:
    """Shares that keep this symbol inside its share of equity.

    `reserved` is the quantity already committed by orders that have not
    filled yet. Ignoring it would let one bar's worth of split orders add up
    to several times the cap while every one of them passed.
    """
    allowed_value = equity * config.max_single_name_exposure
    held_value = (portfolio.quantity_of(symbol) + reserved) * price
    return _floor_div(allowed_value - held_value, price)


def gross_leverage_cap(
    price: Decimal, equity: Decimal, gross_exposure: Decimal, config: RiskConfig
) -> int:
    """Shares that keep total exposure inside the leverage ceiling."""
    allowed_value = equity * config.max_gross_leverage
    return _floor_div(allowed_value - gross_exposure, price)


def cluster_cap(
    symbol: str,
    price: Decimal,
    equity: Decimal,
    portfolio: Portfolio,
    marks: dict[str, Decimal],
    cluster_symbols: tuple[str, ...],
    config: RiskConfig,
    reservations: dict[str, int] | None = None,
) -> int:
    """Shares that keep a correlated group inside its combined ceiling.

    This is what stops several strategies independently piling into the same
    underlying risk: the cap is computed across the whole cluster and across
    every strategy, because the book does not care which strategy bought.
    """
    allowed_value = equity * config.max_correlated_cluster_exposure
    current = ZERO
    reservations = reservations or {}
    for member in cluster_symbols:
        mark = marks.get(member)
        if mark is None:
            continue
        current += (portfolio.quantity_of(member) + reservations.get(member, 0)) * mark
    return _floor_div(allowed_value - current, price)


def cash_cap(price: Decimal, cash: Decimal, execution: ExecutionConfig) -> int:
    """Shares the available cash can actually pay for, commission included.

    No margin: the answer is bounded by settled cash, not by buying power.

    Delegates to `ExecutionConfig.max_affordable_quantity`, which solves it
    in closed form. `FillModel.affordable_quantity` answers the same question
    at fill time and went through the same solver, so the two cannot disagree.

    Both previously searched: start from `(cash - min_commission) / price`, an
    upper bound that ignores the per-share charge entirely, then walk down one
    share at a time. The number of steps scales as cash/price, so it was
    invisible on a 100 stock and crippling on a penny one -- 980,382,157
    decrements at a 0.0001 price with 100,000 of cash. Measured on the real
    6,799-symbol cross-section, 85 of whose symbols trade under a cent, this
    one function held 442.094 of the run's 443.030 seconds across 41 calls.
    It returned the right cap; it just took minutes per call to do it.
    """
    return execution.max_affordable_quantity(price, cash)


def _total_cost(quantity: int, price: Decimal, execution: ExecutionConfig) -> Decimal:
    return execution.total_cost(quantity, price)


def participation_cap(volume: int, execution: ExecutionConfig) -> int:
    """Shares we are willing to assume we could have traded in one session.

    Assuming you could buy an unlimited quantity of a thin name is one of the
    most flattering and least realistic errors a backtest can make.
    """
    if volume <= 0:
        return 0
    return int(
        (Decimal(volume) * execution.max_participation_of_volume).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )


def risk_budget_cap(
    symbol: str, price: Decimal, equity: Decimal, view: PointInTimeView, config: RiskConfig
) -> int | None:
    """Shares that keep the modelled loss inside the per-trade risk budget.

    Returns None when trailing volatility cannot be estimated or is zero: a
    zero-volatility series implies zero risk per share, and dividing by it
    would produce an infinite position. None means "this rule has no opinion",
    which is honest, rather than a fabricated cap.
    """
    returns = view.returns(symbol, lookback=RISK_VOL_WINDOW)
    if len(returns) < 2:
        return None
    # Volatility is an estimate, so float arithmetic is appropriate here. The
    # result is converted back to Decimal before it touches a share count.
    values = [float(r) for r in returns]
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    sigma = math.sqrt(variance)
    if sigma <= 0.0:
        return None
    risk_per_share = STOP_SIGMA_MULTIPLE * Decimal(str(sigma)) * price
    if risk_per_share <= ZERO:
        return None
    budget = equity * config.risk_budget_per_trade
    return _floor_div(budget, risk_per_share)
