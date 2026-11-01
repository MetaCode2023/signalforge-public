"""Assay: does copying insiders' disclosed purchases beat buying the market?

The idea comes from copy-trading pitches ("find the wallets that win, copy
them"). The stock-market version is the Form 4 filing: a company insider who
buys their own company's shares in the open market has to disclose it within
two business days, and anyone can copy it after that.

What makes the pitch look brilliant is a trap: choosing the "winners" by how
their trades turned out, then copying those same trades. This package
measures that trap next to the honest version, where an insider counts as
"smart" only on trades whose outcomes were already known when they bought
again, and every copied trade enters the day after the filing was public.

Research only, paper only. Nothing here imports a network library, and
nothing in `paper/`, `strategies/` or `risk/` imports this package.
"""
