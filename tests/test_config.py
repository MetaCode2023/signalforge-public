"""Configuration loading and validation."""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.config.models import (
    DataSourcesConfig,
    ExecutionConfig,
    ResearchConfig,
    RiskConfig,
    SplitConfig,
    load_all,
    load_research,
    load_risk,
)
from signalforge.core.errors import ConfigError

D = Decimal


class TestRiskConfig:
    def test_shipped_defaults_match_the_mandated_research_values(self):
        cfg = load_risk()
        assert cfg.risk_budget_per_trade == D("0.005")
        assert cfg.max_single_name_exposure == D("0.05")
        assert cfg.max_daily_loss == D("0.02")
        assert cfg.max_portfolio_drawdown == D("0.10")
        assert cfg.allow_margin is False
        assert cfg.allow_short is False
        assert cfg.allow_options is False
        assert cfg.allow_averaging_down is False
        assert cfg.max_gross_leverage == D("1.0")

    def test_rejects_a_fraction_above_one(self):
        with pytest.raises(ConfigError):
            RiskConfig(max_single_name_exposure=D("1.5"))

    def test_rejects_a_negative_fraction(self):
        with pytest.raises(ConfigError):
            RiskConfig(max_daily_loss=D("-0.01"))

    def test_rejects_margin_enabled_without_leverage_above_one(self):
        # Contradictory config must fail loudly rather than pick a side.
        with pytest.raises(ConfigError):
            RiskConfig(allow_margin=True, max_gross_leverage=D("1.0"))

    def test_rejects_leverage_above_one_without_margin(self):
        with pytest.raises(ConfigError):
            RiskConfig(allow_margin=False, max_gross_leverage=D("2.0"))

    def test_single_name_cap_must_not_exceed_gross_leverage(self):
        with pytest.raises(ConfigError):
            RiskConfig(max_single_name_exposure=D("1.0"), max_gross_leverage=D("0.5"))

    def test_daily_loss_limit_must_be_tighter_than_drawdown_limit(self):
        with pytest.raises(ConfigError):
            RiskConfig(max_daily_loss=D("0.20"), max_portfolio_drawdown=D("0.10"))

    def test_is_immutable(self):
        cfg = load_risk()
        with pytest.raises(Exception):
            cfg.max_daily_loss = D("0.5")


class TestExecutionConfig:
    def test_rejects_negative_slippage(self):
        with pytest.raises(ConfigError):
            ExecutionConfig(slippage_bps=D("-1"))

    def test_rejects_negative_commission(self):
        with pytest.raises(ConfigError):
            ExecutionConfig(commission_per_share=D("-0.01"))

    def test_defaults_are_conservative_not_frictionless(self):
        cfg = ExecutionConfig()
        assert cfg.slippage_bps > 0, "a frictionless default flatters every backtest"
        assert cfg.fill_delay_days >= 1, "same-bar fills are look-ahead bias"


class TestSplitConfig:
    def test_rejects_overlapping_train_and_validation(self):
        with pytest.raises(ConfigError):
            SplitConfig(
                train_start=date(2000, 1, 1),
                train_end=date(2010, 1, 1),
                validation_start=date(2009, 1, 1),
                validation_end=date(2015, 1, 1),
                test_start=date(2015, 1, 2),
                test_end=date(2020, 1, 1),
            )

    def test_rejects_overlapping_validation_and_test(self):
        with pytest.raises(ConfigError):
            SplitConfig(
                train_start=date(2000, 1, 1),
                train_end=date(2010, 1, 1),
                validation_start=date(2010, 1, 2),
                validation_end=date(2015, 1, 1),
                test_start=date(2014, 1, 1),
                test_end=date(2020, 1, 1),
            )

    def test_rejects_out_of_order_bounds(self):
        with pytest.raises(ConfigError):
            SplitConfig(
                train_start=date(2010, 1, 1),
                train_end=date(2000, 1, 1),
                validation_start=date(2011, 1, 1),
                validation_end=date(2015, 1, 1),
                test_start=date(2015, 1, 2),
                test_end=date(2020, 1, 1),
            )

    def test_accepts_strictly_ordered_disjoint_windows(self):
        cfg = SplitConfig(
            train_start=date(2000, 1, 1),
            train_end=date(2010, 1, 1),
            validation_start=date(2010, 1, 2),
            validation_end=date(2015, 1, 1),
            test_start=date(2015, 1, 2),
            test_end=date(2020, 1, 1),
        )
        assert cfg.test_end > cfg.test_start

    def test_contains_routes_a_date_to_its_window(self):
        cfg = SplitConfig(
            train_start=date(2000, 1, 1),
            train_end=date(2010, 1, 1),
            validation_start=date(2010, 1, 2),
            validation_end=date(2015, 1, 1),
            test_start=date(2015, 1, 2),
            test_end=date(2020, 1, 1),
        )
        assert cfg.window_for(date(2005, 1, 1)) == "train"
        assert cfg.window_for(date(2012, 1, 1)) == "validation"
        assert cfg.window_for(date(2018, 1, 1)) == "test"
        assert cfg.window_for(date(1999, 1, 1)) is None


class TestResearchConfig:
    def test_loads_the_shipped_file(self):
        cfg = load_research()
        assert cfg.starting_cash == D("100000")
        assert cfg.paper_only is True
        assert len(cfg.universe) > 0

    def test_paper_only_cannot_be_disabled_by_config(self):
        # The safety default is not a tunable. Config cannot turn it off.
        with pytest.raises(ConfigError):
            ResearchConfig(paper_only=False)

    def test_rejects_non_positive_starting_cash(self):
        with pytest.raises(ConfigError):
            ResearchConfig(starting_cash=D("0"))

    def test_rejects_duplicate_symbols_in_the_universe(self):
        with pytest.raises(ConfigError):
            ResearchConfig(universe=["AAPL", "AAPL"])

    def test_universe_symbols_are_normalised_to_upper_case(self):
        cfg = ResearchConfig(universe=["aapl", "msft"])
        assert cfg.universe == ("AAPL", "MSFT")


class TestDataSourcesConfig:
    def test_loads_the_shipped_file(self):
        cfg = load_data_sources_shipped()
        assert "fred" in cfg.providers
        fred = cfg.providers["fred"]
        assert fred.requires_key is False
        assert fred.enabled is True

    def test_every_provider_records_its_licence_and_terms_url(self):
        cfg = load_data_sources_shipped()
        for name, provider in cfg.providers.items():
            assert provider.licence_note, f"{name} has no licence note"
            assert provider.terms_url, f"{name} has no terms_url"

    def test_a_provider_needing_a_key_names_the_env_var_not_the_secret(self):
        cfg = load_data_sources_shipped()
        for name, provider in cfg.providers.items():
            if provider.requires_key:
                assert provider.api_key_env, f"{name} requires a key but names no env var"
                assert not provider.api_key_env.startswith("sk-"), "looks like a literal secret"

    def test_rejected_providers_are_recorded_with_a_reason(self):
        cfg = load_data_sources_shipped()
        rejected = {n: p for n, p in cfg.providers.items() if p.status == "rejected"}
        assert rejected, "the rejected-source decisions must stay in the config as a record"
        for name, provider in rejected.items():
            assert provider.rejection_reason, f"{name} rejected with no reason"
            assert provider.enabled is False


def load_data_sources_shipped() -> DataSourcesConfig:
    from signalforge.config.models import load_data_sources

    return load_data_sources()


class TestLoadAll:
    def test_returns_every_section_together(self):
        bundle = load_all()
        assert isinstance(bundle.research, ResearchConfig)
        assert isinstance(bundle.risk, RiskConfig)
        assert isinstance(bundle.data_sources, DataSourcesConfig)

    def test_reports_a_missing_file_by_path(self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            load_risk(tmp_path / "nope.yaml")
        assert "nope.yaml" in str(excinfo.value)
