"""Booking of splits and cash dividends against a Portfolio.

Pure book-keeping: no broker, no warehouse. Shares stay whole, so a split's
fractional remainder is paid out as cash-in-lieu at the prior raw close, and
the cost attributable to that fraction is realized with it. A dividend is
booked to the holder of record as of the prior close.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from signalforge.core.errors import ValidationError
from signalforge.core.types import ZERO, CorporateAction, Portfolio

CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class CorporateActionBooking:
    """What one action did to the book.

    `residual` is the unrounded cash-in-lieu minus the cent-rounded amount
    actually paid, so `cash_in_lieu + residual` recovers the exact figure.
    `realized_pnl_delta` is the P&L realized by the action: the fraction's
    cash less its cost for a split, the cash received for a dividend.
    """

    action_id: str
    symbol: str
    shares_before: int
    shares_after: int
    cash_delta: Decimal
    cash_in_lieu: Decimal
    residual: Decimal
    realized_pnl_delta: Decimal


def dividend_cash(quantity: int, cash_per_share: Decimal) -> Decimal:
    """Signed cash for a dividend: a short position owes it."""
    return (cash_per_share * quantity).quantize(CENT, rounding=ROUND_HALF_EVEN)


def apply_corporate_action(
    portfolio: Portfolio, action: CorporateAction, prior_close: Decimal
) -> CorporateActionBooking | None:
    """Book `action` against `portfolio`, or return None if it changes nothing.

    None means no position was held, or this action_id was already evaluated;
    evaluating it while flat still consumes the entitlement.
    `prior_close` is the raw, unadjusted close before the effective date.
    """
    if not isinstance(prior_close, Decimal) or prior_close <= ZERO:
        raise ValidationError(f"prior_close must be a positive Decimal, got {prior_close!r}")
    if action.action_type not in ("dividend", "split"):
        raise ValidationError(f"{action.action_id}: cannot book action type {action.action_type!r}")
    if action.action_id in portfolio._applied_action_ids:
        return None
    portfolio._applied_action_ids.add(action.action_id)
    position = portfolio.positions.get(action.symbol)
    if position is None or position.quantity == 0:
        return None

    before = position.quantity
    if action.action_type == "dividend":
        cash = dividend_cash(before, action.cash_per_share)
        portfolio.cash += cash
        position.realized_pnl += cash
        return CorporateActionBooking(
            action.action_id, action.symbol, before, before, cash, ZERO, ZERO, cash
        )
    scaled = before * action.numerator
    after = scaled // action.denominator
    remainder = scaled % action.denominator
    exact = Decimal(remainder) * prior_close / action.numerator
    cash = exact.quantize(CENT, rounding=ROUND_HALF_EVEN)
    cost_of_fraction = position.cost_basis * remainder / scaled
    realized = cash - cost_of_fraction
    portfolio.cash += cash
    position.realized_pnl += realized
    position.quantity = after
    position.cost_basis -= cost_of_fraction
    if after == 0:
        portfolio.closed_realized_pnl += position.realized_pnl
        del portfolio.positions[action.symbol]
    return CorporateActionBooking(
        action.action_id, action.symbol, before, after, cash, cash, exact - cash, realized
    )
