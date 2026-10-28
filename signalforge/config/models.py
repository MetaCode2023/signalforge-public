"""Typed, validated configuration.

Pydantic validates shape; the model validators below enforce the things that
matter more: that no two evaluation windows overlap (data leakage), that the
risk config is not internally contradictory, and that PAPER_ONLY is not a
tunable a config file can switch off.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError
from pydantic import field_validator, model_validator

from signalforge.core.errors import AccountingError, ConfigError

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
Window = Literal["train", "validation", "test"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _floor_shares(numerator: Decimal, denominator: Decimal) -> int:
    """Whole shares that fit, floored. Zero for a non-positive input."""
    if denominator <= Decimal("0") or numerator <= Decimal("0"):
        return 0
    return int((numerator / denominator).to_integral_value(rounding=ROUND_FLOOR))


def _fraction(name: str, value: Decimal, *, upper: Decimal = Decimal("1")) -> Decimal:
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {value}")
    if value > upper:
        raise ConfigError(f"{name} must not exceed {upper}, got {value}")
    return value


class RiskConfig(_Frozen):
    """Risk limits. These are research defaults, not guarantees.

    A gap through a limit still loses money; these caps govern what orders the
    risk engine will accept, not what the market will do.
    """

    risk_budget_per_trade: Decimal = Decimal("0.005")
    max_single_name_exposure: Decimal = Decimal("0.05")
    max_daily_loss: Decimal = Decimal("0.02")
    max_portfolio_drawdown: Decimal = Decimal("0.10")
    max_gross_leverage: Decimal = Decimal("1.0")
    max_positions: int = 20
    max_orders_per_day: int = 50
    max_orders_per_symbol_per_day: int = 2
    stale_data_max_age_days: int = 5
    max_correlated_cluster_exposure: Decimal = Decimal("0.20")
    allow_margin: bool = False
    allow_short: bool = False
    allow_options: bool = False
    allow_averaging_down: bool = False
    allow_pyramiding: bool = False

    @field_validator(
        "risk_budget_per_trade",
        "max_single_name_exposure",
        "max_daily_loss",
        "max_portfolio_drawdown",
        "max_correlated_cluster_exposure",
    )
    @classmethod
    def _check_fraction(cls, value: Decimal, info) -> Decimal:
        return _fraction(info.field_name, value)

    @field_validator("max_gross_leverage")
    @classmethod
    def _check_leverage(cls, value: Decimal) -> Decimal:
        if value < Decimal("0"):
            raise ConfigError(f"max_gross_leverage must not be negative, got {value}")
        return value

    @field_validator(
        "max_positions",
        "max_orders_per_day",
        "max_orders_per_symbol_per_day",
        "stale_data_max_age_days",
    )
    @classmethod
    def _check_positive_int(cls, value: int, info) -> int:
        if value < 1:
            raise ConfigError(f"{info.field_name} must be at least 1, got {value}")
        return value

    @model_validator(mode="after")
    def _check_coherent(self) -> RiskConfig:
        if self.allow_margin and self.max_gross_leverage <= Decimal("1"):
            raise ConfigError(
                "allow_margin is true but max_gross_leverage is "
                f"{self.max_gross_leverage}; margin with no leverage headroom is "
                "contradictory. Set leverage above 1 or disable margin."
            )
        if not self.allow_margin and self.max_gross_leverage > Decimal("1"):
            raise ConfigError(
                f"max_gross_leverage is {self.max_gross_leverage} but allow_margin is "
                "false; leverage above 1 requires borrowing."
            )
        if self.max_single_name_exposure > self.max_gross_leverage:
            raise ConfigError(
                f"max_single_name_exposure {self.max_single_name_exposure} exceeds "
                f"max_gross_leverage {self.max_gross_leverage}"
            )
        if self.max_daily_loss >= self.max_portfolio_drawdown:
            raise ConfigError(
                f"max_daily_loss {self.max_daily_loss} is not tighter than "
                f"max_portfolio_drawdown {self.max_portfolio_drawdown}; the daily "
                "breaker would never fire before the drawdown breaker"
            )
        if self.allow_short:
            raise ConfigError("short selling is not implemented; allow_short must be false")
        if self.allow_options:
            raise ConfigError("options are not implemented; allow_options must be false")
        return self


class ExecutionConfig(_Frozen):
    """Modelled trading frictions.

    `fill_delay_days` of at least 1 is the structural defence against
    look-ahead bias: a signal from today's close cannot fill at today's close.
    """

    commission_per_share: Decimal = Decimal("0.005")
    min_commission: Decimal = Decimal("1.00")
    slippage_bps: Decimal = Decimal("5")
    fill_delay_days: int = 1
    max_participation_of_volume: Decimal = Decimal("0.01")
    allow_partial_fills: bool = True
    gap_slippage_multiplier: Decimal = Decimal("2.0")

    @field_validator("commission_per_share", "min_commission", "slippage_bps")
    @classmethod
    def _non_negative(cls, value: Decimal, info) -> Decimal:
        if value < 0:
            raise ConfigError(f"{info.field_name} must not be negative, got {value}")
        return value

    @field_validator("fill_delay_days")
    @classmethod
    def _delay_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ConfigError(
                f"fill_delay_days must be at least 1, got {value}; a same-bar fill "
                "assumes knowledge the strategy did not have"
            )
        return value

    @field_validator("max_participation_of_volume")
    @classmethod
    def _participation_fraction(cls, value: Decimal) -> Decimal:
        return _fraction("max_participation_of_volume", value)

    @field_validator("gap_slippage_multiplier")
    @classmethod
    def _gap_multiplier(cls, value: Decimal) -> Decimal:
        if value < Decimal("1"):
            raise ConfigError(
                f"gap_slippage_multiplier must be at least 1, got {value}; a gap "
                "cannot cost less than a normal fill"
            )
        return value

    # ------------------------------------------------------------------
    # The commission schedule, and its inverse
    # ------------------------------------------------------------------
    # These live here rather than in the risk or execution layer because both
    # of those need them and neither owns the schedule. Two implementations of
    # the inverse already existed -- `risk.limits.cash_cap` and
    # `FillModel.affordable_quantity` -- and both searched for the answer one
    # share at a time, which gauntlet round 6 measured at 442 of 443 seconds
    # of a penny-priced run. One solver cannot disagree with itself.

    def commission(self, quantity: int) -> Decimal:
        """What the broker charges for `quantity` shares."""
        return max(self.min_commission, self.commission_per_share * quantity)

    def total_cost(self, quantity: int, price: Decimal) -> Decimal:
        """Cash out the door for `quantity` shares at `price`."""
        return price * quantity + self.commission(quantity)

    def max_affordable_quantity(self, price: Decimal, cash: Decimal) -> int:
        """Largest whole-share quantity `cash` can pay for at `price`.

        `total_cost` is piecewise linear in quantity with one kink, where the
        per-share charge overtakes the commission floor. Each piece inverts
        directly, so this costs two divisions instead of a search whose length
        scales as cash/price.

        Returns the largest affordable quantity, never a larger one: the
        caller spends real cash against it.
        """
        if price <= Decimal("0"):
            return 0
        # Piece 1: quantities small enough that the floor still binds.
        best = _floor_shares(cash - self.min_commission, price)
        if self.commission_per_share > Decimal("0"):
            kink = _floor_shares(self.min_commission, self.commission_per_share)
            best = min(best, kink)
            # Piece 2: quantities where the per-share charge binds. Valid only
            # ABOVE the kink; below it the per-share charge is not what the
            # order would pay, and taking this bound there overstated the
            # answer by one share and overdrew the account by a cent.
            above_kink = _floor_shares(cash, price + self.commission_per_share)
            if above_kink > kink and above_kink > best:
                best = above_kink
        if best <= 0:
            return 0
        if self.total_cost(best, price) > cash:
            # Unreachable by the algebra above; an assertion rather than a
            # silent over-estimate, because an overstated answer is an
            # overdraft in whichever caller spends against it.
            raise AccountingError(
                f"max_affordable_quantity computed {best} shares at {price}, "
                f"costing {self.total_cost(best, price)} against {cash} of cash"
            )
        return best


class SplitConfig(_Frozen):
    """Train / validation / test windows. Strictly ordered and disjoint."""

    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date

    @model_validator(mode="after")
    def _check_disjoint_and_ordered(self) -> SplitConfig:
        bounds = [
            ("train_start", self.train_start),
            ("train_end", self.train_end),
            ("validation_start", self.validation_start),
            ("validation_end", self.validation_end),
            ("test_start", self.test_start),
            ("test_end", self.test_end),
        ]
        for (prev_name, prev), (next_name, nxt) in zip(bounds, bounds[1:]):
            if nxt <= prev:
                raise ConfigError(
                    f"{next_name} ({nxt}) must come strictly after {prev_name} ({prev}); "
                    "overlapping windows leak data between tuning and evaluation"
                )
        return self

    def window_for(self, day: date) -> Window | None:
        if self.train_start <= day <= self.train_end:
            return "train"
        if self.validation_start <= day <= self.validation_end:
            return "validation"
        if self.test_start <= day <= self.test_end:
            return "test"
        return None


class ResearchConfig(_Frozen):
    starting_cash: Decimal = Decimal("100000")
    universe: tuple[str, ...] = ()
    benchmark_symbol: str = "NASDAQCOM"
    paper_only: bool = True
    random_seed: int = 20261026
    min_trades_for_evidence: int = 30
    min_observations_for_metrics: int = 252
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    splits: SplitConfig | None = None

    @field_validator("universe", mode="before")
    @classmethod
    def _normalise_universe(cls, value):
        if value is None:
            return ()
        symbols = [str(s).strip().upper() for s in value if str(s).strip()]
        if len(set(symbols)) != len(symbols):
            duplicates = sorted({s for s in symbols if symbols.count(s) > 1})
            raise ConfigError(f"duplicate symbols in universe: {duplicates}")
        return tuple(symbols)

    @field_validator("starting_cash")
    @classmethod
    def _positive_cash(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ConfigError(f"starting_cash must be positive, got {value}")
        return value

    @field_validator("paper_only")
    @classmethod
    def _paper_only_is_not_tunable(cls, value: bool) -> bool:
        if value is not True:
            raise ConfigError(
                "paper_only cannot be set false. This system has no live-order code "
                "path; disabling the flag would only hide that fact."
            )
        return value

    @field_validator("min_trades_for_evidence", "min_observations_for_metrics")
    @classmethod
    def _positive_int(cls, value: int, info) -> int:
        if value < 1:
            raise ConfigError(f"{info.field_name} must be at least 1, got {value}")
        return value


class ProviderConfig(_Frozen):
    """One data source, with its licence position recorded alongside it.

    `status` is part of the audit trail: a source we investigated and refused
    stays in the config with its reason, so the decision is not re-litigated
    from memory later.
    """

    name: str
    kind: Literal["series", "bars", "archive"]
    status: Literal["active", "pending_key", "rejected"] = "active"
    enabled: bool = False
    requires_key: bool = False
    api_key_env: str | None = None
    base_url: str | None = None
    licence_note: str
    terms_url: str
    rejection_reason: str | None = None
    rate_limit_per_minute: int | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check_coherent(self) -> ProviderConfig:
        if self.status == "rejected":
            if self.enabled:
                raise ConfigError(f"provider {self.name} is rejected but still enabled")
            if not self.rejection_reason:
                raise ConfigError(f"provider {self.name} is rejected with no reason recorded")
        if self.requires_key and not self.api_key_env:
            raise ConfigError(
                f"provider {self.name} requires a key but names no api_key_env. "
                "Config records the env var name, never the secret itself."
            )
        if self.status == "active" and self.requires_key and not self.enabled:
            raise ConfigError(
                f"provider {self.name} is active and keyed but not enabled; use "
                "status 'pending_key' until the key exists"
            )
        return self


class DataSourcesConfig(_Frozen):
    providers: dict[str, ProviderConfig]
    user_agent: str = "signalforge-research/0.1"

    @property
    def active(self) -> dict[str, ProviderConfig]:
        return {n: p for n, p in self.providers.items() if p.enabled}


class ConfigBundle(_Frozen):
    research: ResearchConfig
    risk: RiskConfig
    data_sources: DataSourcesConfig


def validate_paper_runtime(research: ResearchConfig, risk: RiskConfig):
    """Revalidate copied configs and enforce the operator's execution policy."""
    research = ResearchConfig.model_validate(research.model_dump())
    risk = RiskConfig.model_validate(risk.model_dump())
    if risk.allow_margin or risk.max_gross_leverage > Decimal("1"):
        raise ConfigError("paper execution is cash-only; margin and leverage are prohibited")
    if risk.allow_averaging_down:
        raise ConfigError("paper execution cannot enable averaging down")
    return research, risk


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return loaded


def _build(model: type[BaseModel], payload: dict, path: Path):
    try:
        return model(**payload)
    except ConfigError:
        raise
    except PydanticValidationError as exc:
        raise ConfigError(f"{path} failed validation: {exc}") from exc


def load_risk(path: Path | None = None) -> RiskConfig:
    path = path or CONFIG_DIR / "risk.yaml"
    return _build(RiskConfig, _read_yaml(path), path)


def load_research(path: Path | None = None) -> ResearchConfig:
    path = path or CONFIG_DIR / "research.yaml"
    return _build(ResearchConfig, _read_yaml(path), path)


def load_data_sources(path: Path | None = None) -> DataSourcesConfig:
    path = path or CONFIG_DIR / "data_sources.yaml"
    payload = _read_yaml(path)
    raw_providers = payload.get("providers") or {}
    providers: dict[str, ProviderConfig] = {}
    for name, spec in raw_providers.items():
        spec = dict(spec or {})
        spec.setdefault("name", name)
        providers[name] = _build(ProviderConfig, spec, path)
    return _build(
        DataSourcesConfig,
        {"providers": providers, "user_agent": payload.get("user_agent", "signalforge-research/0.1")},
        path,
    )


def load_all(config_dir: Path | None = None) -> ConfigBundle:
    directory = config_dir or CONFIG_DIR
    return ConfigBundle(
        research=load_research(directory / "research.yaml"),
        risk=load_risk(directory / "risk.yaml"),
        data_sources=load_data_sources(directory / "data_sources.yaml"),
    )
