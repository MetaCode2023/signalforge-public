"""Fixture-only integration with the real providers, ingestion, and warehouse."""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from signalforge.collector_cli import build_task
from signalforge.data.collector import Collector, CollectorConfig
from signalforge.data.storage import Warehouse


NOW = datetime(2024, 1, 5, tzinfo=timezone.utc)


class Response:
    status_code = 200
    text = (Path(__file__).parent / "fixtures" / "fred_sample.csv").read_text()


class Session:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.urls = []

    def get(self, *args, **kwargs):
        self.urls.append(args[0])
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


def test_real_ingestion_is_timestamped_deduplicated_and_reconnected(tmp_path):
    sessions = [Session(requests.ConnectionError("private-network-detail")), Session(Response()), Session(Response())]
    unused = list(sessions)
    config = CollectorConfig(series_ids=("DGS10", "DGS2"))
    db = tmp_path / "fixture.duckdb"
    config, task = build_task(config, db, clock=lambda: NOW, session_factory=lambda: unused.pop(0))
    collector = Collector(config, db, task, clock=lambda: NOW, wait=lambda _: False, jitter=lambda: 1)
    assert collector.run()["status"] == "ok"
    first = Warehouse(db)
    counts = first.table_counts()
    assert first.con.execute("SELECT count(*) FROM series WHERE retrieved_at IS NOT NULL").fetchone()[0] == 5
    first.close()
    assert collector.run()["status"] == "ok"
    second = Warehouse(db)
    assert second.count("series") == counts["series"] == 5
    assert second.count("raw_records") == counts["raw_records"] == 5
    assert second.con.execute("SELECT count(*) FROM ingest_runs WHERE status='failed'").fetchone()[0] == 1
    assert "private-network-detail" not in str(second.con.execute("SELECT error FROM ingest_runs").fetchall())
    second.close()
    assert all(session.closed for session in sessions)


def test_disabled_provider_refused_without_fetching(tmp_path):
    config = CollectorConfig(provider="alphavantage", series_ids=(), symbols=("AAPL",))
    with pytest.raises(ValueError, match="active and enabled"):
        build_task(config, tmp_path / "unused.duckdb")
    assert not (tmp_path / "unused.duckdb").exists()


def test_authentication_failure_does_not_retry(tmp_path):
    class Refused(Response):
        status_code = 401
    sessions = []
    def factory():
        sessions.append(Session(Refused()))
        return sessions[-1]
    config, task = build_task(CollectorConfig(series_ids=("DGS10",)), tmp_path / "auth.duckdb",
                              clock=lambda: NOW, session_factory=factory)
    result = Collector(config, tmp_path / "auth.duckdb", task, clock=lambda: NOW).run()
    assert result["status"] == "failed"
    assert len(sessions) == 1


def test_reconnect_catches_up_from_oldest_persisted_observation(tmp_path):
    db = tmp_path / "catchup.duckdb"
    warehouse = Warehouse(db)
    warehouse.upsert_series([{"series_id": "DGS10", "observation_date": date(2023, 8, 1),
                              "value": Decimal("4.0"), "source": "fred", "retrieved_at": None}])
    warehouse.close()
    session = Session(Response())
    config, task = build_task(CollectorConfig(series_ids=("DGS10",)), db,
                              clock=lambda: NOW, session_factory=lambda: session)
    task()
    assert "cosd=2023-07-18" in session.urls[0]
    persisted = Warehouse(db)
    try:
        assert persisted.load_series("DGS10", source="fred") == [
            (date(2023, 8, 1), Decimal("4.0")),
            (date(2024, 1, 2), Decimal("3.95")),
            (date(2024, 1, 4), Decimal("4.02")),
        ]
        assert persisted.con.execute(
            "SELECT count(*) FROM series WHERE observation_date >= DATE '2024-01-01' "
            "AND retrieved_at IS NOT NULL"
        ).fetchall()[0][0] == 5
    finally:
        persisted.close()


def test_batched_tiingo_rotation_fetches_stalest_symbols_first(tmp_path, monkeypatch):
    """Each run requests only the batch: never-fetched names first, priority first among ties."""
    from signalforge.config import models

    real = models.load_data_sources

    def unpaced():
        # The real policy spaces requests 2 s apart; pacing is tested in test_data_providers.
        sources = real()
        sources.providers["tiingo"] = sources.providers["tiingo"].model_copy(update={"rate_limit_per_minute": None})
        return sources

    monkeypatch.setattr(models, "load_data_sources", unpaced)
    monkeypatch.setenv("TIINGO_API_KEY", "test-key")

    class Prices(Response):
        text = (Path(__file__).parent / "fixtures" / "tiingo_aapl_prices.json").read_text()

    sessions = []

    def factory():
        sessions.append(Session(Prices()))
        return sessions[-1]

    when = datetime(2020, 10, 1, tzinfo=timezone.utc)
    config = CollectorConfig(provider="tiingo", series_ids=(), symbols=("AAA", "BBB", "CCC"),
                             priority_symbols=("CCC",), batch_size=2, requests_per_day=10)
    db = tmp_path / "rotation.duckdb"
    config, task = build_task(config, db, clock=lambda: when, session_factory=factory)

    def requested(session):
        return [url.split("/daily/")[1].split("/")[0] for url in session.urls]

    task()
    assert requested(sessions[0]) == ["CCC", "AAA"]
    task()
    assert requested(sessions[1]) == ["BBB", "CCC"]
    warehouse = Warehouse(db)
    try:
        symbols = {row[0] for row in warehouse.con.execute("SELECT DISTINCT symbol FROM bars").fetchall()}
    finally:
        warehouse.close()
    assert symbols == {"AAA", "BBB", "CCC"}
