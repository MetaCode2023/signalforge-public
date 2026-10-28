"""End-of-day statements for the simulated book.

Every statement is stamped SIMULATED. The label is not decoration: a
statement that reads like a brokerage document and is not one is exactly the
artefact that causes someone to believe a number that was never real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from signalforge.core.types import Fill, Position

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Statement:
    statement_date: date
    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fees: Decimal
    total_slippage_cost: Decimal
    drawdown: Decimal
    peak_equity: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    fills: tuple[Fill, ...] = ()
    simulated: bool = True

    def render(self) -> str:
        lines = [
            "=" * 62,
            f"  SIMULATED END-OF-DAY STATEMENT  {self.statement_date.isoformat()}",
            "  SIGNALFORGE PAPER TRADING. No real capital. No real orders.",
            "=" * 62,
            f"  Simulated equity          {self.equity:>16,.2f}",
            f"  Simulated cash            {self.cash:>16,.2f}",
            f"  Gross exposure            {self.gross_exposure:>16,.2f}",
            f"  Realized P&L (cumulative) {self.realized_pnl:>16,.2f}",
            f"  Unrealized P&L            {self.unrealized_pnl:>16,.2f}",
            f"  Fees paid (cumulative)    {self.total_fees:>16,.2f}",
            f"  Slippage (cumulative)     {self.total_slippage_cost:>16,.2f}",
            f"  Drawdown from peak        {self.drawdown:>15.2%}",
            "-" * 62,
        ]
        if self.positions:
            lines.append("  OPEN POSITIONS (simulated)")
            for symbol in sorted(self.positions):
                position = self.positions[symbol]
                lines.append(
                    f"    {symbol:<10} {position.quantity:>8} sh  "
                    f"avg cost {position.average_cost:>12,.4f}"
                )
        else:
            lines.append("  OPEN POSITIONS (simulated): none. Checked, book is flat.")
        lines.append("-" * 62)
        if self.fills:
            lines.append("  FILLS TODAY (simulated)")
            for fill in self.fills:
                lines.append(
                    f"    {fill.side.value:<4} {fill.quantity:>8} {fill.symbol:<10} "
                    f"@ {fill.price:>12,.4f}  fees {fill.fees:>8,.2f}"
                )
        else:
            lines.append("  FILLS TODAY (simulated): none. Checked, no trades settled.")
        lines.append("=" * 62)
        return "\n".join(lines)
