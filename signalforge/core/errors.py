"""Exception hierarchy for SIGNALFORGE.

Separated by blast radius: a ValidationError means an input was malformed, an
AccountingError means a book-keeping invariant would have been broken, and a
LiveTradingAttempt means something tried to reach a real brokerage.
"""


class SignalForgeError(Exception):
    """Base for every error this system raises deliberately."""


class ValidationError(SignalForgeError):
    """An input value is malformed or internally inconsistent."""


class AccountingError(SignalForgeError):
    """An operation would have violated a portfolio accounting invariant."""


class DataQualityError(SignalForgeError):
    """Market data failed a validation rule and cannot be used as-is."""


class ProviderError(SignalForgeError):
    """A data provider was unreachable, unauthorised, or returned garbage."""


class TransientProviderError(ProviderError):
    """A transport failure, HTTP 429, or HTTP 5xx eligible for bounded retry."""


class RiskViolation(SignalForgeError):
    """A risk control refused an action."""


class LookAheadError(SignalForgeError):
    """Something tried to read data dated after the decision point.

    This is never recoverable: a backtest that catches and continues past one
    of these is reporting a result the strategy could not have achieved.
    """


class LiveTradingAttempt(SignalForgeError):
    """Something tried to reach a real brokerage. This system is PAPER ONLY.

    Raising this is always a bug or an attack, never a recoverable condition.
    """


class ConfigError(SignalForgeError):
    """Configuration is missing, malformed, or internally contradictory."""


class PromotionError(SignalForgeError):
    """A strategy promotion was attempted without the required approval."""
