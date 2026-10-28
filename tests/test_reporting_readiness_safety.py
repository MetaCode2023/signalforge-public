"""Regressions from the reporting-readiness reliability and safety audit."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import requests

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ResearchConfig, RiskConfig, SplitConfig, load_data_sources
from signalforge.core.errors import ConfigError, ProviderError, TransientProviderError
from signalforge.data.collector import CollectionFailure, Collector, CollectorConfig, Snapshot
from signalforge.data.providers import AlphaVantageProvider, FredSeriesProvider
from signalforge.data.storage import Warehouse
from signalforge.learning.experiments import run_experiment
from signalforge.learning.registry import ExperimentRegistry, StrategySpec
from signalforge.paper.broker import PaperBroker
from signalforge.paper.runner import PaperRunner
from signalforge.strategies.benchmark import BuyAndHold


D = Decimal
NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


@pytest.mark.parametrize("factory", [BacktestEngine, PaperBroker])
def test_execution_refuses_margin_even_when_config_is_internally_consistent(factory):
    with pytest.raises(ConfigError, match="cash-only"):
        factory(ResearchConfig(), RiskConfig(allow_margin=True, max_gross_leverage=D("2")))


@pytest.mark.parametrize("factory", [BacktestEngine, PaperBroker])
def test_execution_refuses_averaging_down_override(factory):
    with pytest.raises(ConfigError, match="averaging down"):
        factory(ResearchConfig(), RiskConfig(allow_averaging_down=True))


@pytest.mark.parametrize("override", [
    {"fill_delay_days": 0}, {"slippage_bps": D("-1")},
    {"slippage_bps": D("NaN")}, {"max_participation_of_volume": D("2")},
    {"unexpected_field": True},
])
def test_execution_overrides_are_revalidated(flat_history, override):
    engine = BacktestEngine(ResearchConfig(), RiskConfig())
    with pytest.raises((ConfigError, ValueError)):
        engine.run(BuyAndHold(("FLAT",), D("0.05")), flat_history, execution=override)


@pytest.mark.parametrize("factory", [BacktestEngine, PaperBroker])
def test_model_copy_cannot_disable_paper_mode(factory):
    unsafe = ResearchConfig().model_copy(update={"paper_only": False})
    with pytest.raises(ConfigError):
        factory(unsafe, RiskConfig())


class Session:
    def __init__(self, status=200, text="observation_date,X\n2026-10-26,1\n", error=None):
        self.status, self.text, self.error = status, text, error
        self.kwargs = None

    def get(self, *args, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return type("Response", (), {"status_code": self.status, "text": self.text})()


def test_provider_does_not_follow_credential_carrying_redirects():
    session = Session(status=302)
    provider = FredSeriesProvider(load_data_sources().providers["fred"], session=session)
    with pytest.raises(ProviderError, match="302"):
        provider.fetch(["X"])
    assert session.kwargs["allow_redirects"] is False


def test_tls_errors_are_permanent_and_redacted():
    session = Session(error=requests.exceptions.SSLError("private-request-detail"))
    provider = FredSeriesProvider(load_data_sources().providers["fred"], session=session)
    with pytest.raises(ProviderError, match="TLS verification failed") as caught:
        provider.fetch(["X"])
    assert not isinstance(caught.value, TransientProviderError)
    assert "private-request-detail" not in str(caught.value)


@pytest.mark.parametrize("field", ["Error Message", "Note", "Information"])
def test_provider_notices_cannot_echo_credentials(field):
    marker = "private-fixture-value"
    provider = AlphaVantageProvider(
        load_data_sources().providers["alphavantage"],
        env={"ALPHAVANTAGE_API_KEY": marker},
        session=Session(text=json.dumps({field: marker})),
    )
    with pytest.raises(ProviderError) as caught:
        provider.fetch(["MSFT"])
    assert marker not in str(caught.value)


def test_corrupt_request_budget_replaces_stale_healthy_status(tmp_path):
    collector = Collector(CollectorConfig(), tmp_path / "health.duckdb",
                          lambda: Snapshot({k: NOW.date() for k in CollectorConfig().items}), clock=lambda: NOW)
    assert collector.run()["status"] == "ok"
    collector.budget_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError):
        collector.run()
    event = json.loads(collector.status_path.read_text())
    assert event["status"] == "failed"
    assert event["reason"] == "request_budget_unavailable"


def test_failed_cycle_returns_the_same_complete_event_as_the_log(tmp_path):
    def task():
        raise CollectionFailure(retryable=False)
    collector = Collector(CollectorConfig(), tmp_path / "failure.duckdb", task, clock=lambda: NOW)
    event = collector.run()
    assert event == json.loads(collector.status_path.read_text())
    assert event["run_id"] == collector.run_id
    assert event["checked_at"] == NOW.isoformat()


def test_restarting_restores_the_book_instead_of_starting_from_fresh_cash(tmp_path, uptrend_history):
    warehouse = Warehouse(tmp_path / "paper.duckdb")
    kwargs = dict(warehouse=warehouse, research=ResearchConfig(universe=("UP",)), risk=RiskConfig(),
                  override_strategy=("buy_and_hold", {"symbols": ("UP",), "target_weight": D("0.05")}),
                  override_reason="bounded regression fixture", start=uptrend_history.trading_days[0])
    try:
        original = PaperRunner(**kwargs)
        days = uptrend_history.trading_days
        original.run(uptrend_history, through=days[20])
        assert original.broker.portfolio().quantity_of("UP") > 0
        restarted = PaperRunner(**kwargs)
        resumed = restarted.run(uptrend_history, through=days[25])
        assert resumed.restored_sessions == 21
        assert resumed.sessions == 5
        assert restarted.broker.portfolio().quantity_of("UP") > 0
        assert restarted.broker.portfolio().cash < original.broker.starting_cash
        continued = original.run(uptrend_history, through=days[25])
        assert continued.sessions == 0
        assert continued.first_session is None
        assert restarted.broker.statements == original.broker.statements
    finally:
        warehouse.close()


def test_every_failed_experiment_is_persisted_without_exception_text(tmp_path, uptrend_history, monkeypatch):
    path = tmp_path / "failures.duckdb"
    warehouse = Warehouse(path)
    registry = ExperimentRegistry(warehouse)
    days = uptrend_history.trading_days
    config = ResearchConfig(splits=SplitConfig(
        train_start=days[0], train_end=days[99], validation_start=days[100],
        validation_end=days[199], test_start=days[200], test_end=days[-1],
    ))
    engine = BacktestEngine(config, RiskConfig())
    def broken(*args, **kwargs):
        raise RuntimeError("private-error-detail")
    monkeypatch.setattr(engine, "run", broken)
    spec = StrategySpec("buy_and_hold", {"symbols": ("UP",), "target_weight": D("0.05")})
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError):
                run_experiment(engine, spec, uptrend_history, config, registry,
                               hypothesis="failed fixture", window="train")
        assert registry.count() == 0
    finally:
        warehouse.close()
    reopened = Warehouse(path)
    try:
        rows = reopened.con.execute("SELECT * FROM experiment_failures").fetchall()
        assert len(rows) == 2
        assert len({r[0] for r in rows}) == 2
        assert all(r[-2] == "RuntimeError" and r[5] for r in rows)
        assert "private-error-detail" not in str(rows)
    finally:
        reopened.close()
