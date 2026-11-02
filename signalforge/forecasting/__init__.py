"""Probabilistic path forecasting, and a trial that holds it to account.

A path forecaster looks at a point-in-time window of bars and returns N
sampled futures. Kronos (a foundation model trained on candlesticks) is one
such forecaster; a block bootstrap and a driftless random walk are two more,
and they are here as the null hypothesis rather than as decoration. A claim
like "a tight spread means the model is confident" is only worth something if
the confident calls are right more often than a forecaster that knows nothing
would manage.

Forecasting math is float64 NumPy. It produces research statistics, never
money: anything that sizes, fills or marks a position still goes through the
Decimal engine and the risk engine.

A forecaster is handed a `ContextWindow` that was cut from a
`PointInTimeView`, never the view and never the history, so it cannot read a
bar dated after the forecast origin even if it tries.
"""
