"""Opt-in daily data collection; one cycle unless --continuous is explicit."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from signalforge.data.collector import (
    CollectionFailure, Collector, CollectorBusy, CollectorConfig, Snapshot, utcnow,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/collection.json"))
    parser.add_argument("--db", type=Path, default=Path("data/warehouse/signalforge.duckdb"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cycles", type=int, default=1)
    mode.add_argument("--continuous", action="store_true")
    parser.add_argument("--archive-dir", type=Path)
    args = parser.parse_args(argv)
    if args.cycles < 1:
        parser.error("--cycles must be positive")
    return args


def load_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("collection configuration must be an object")
    return CollectorConfig(**payload)


def build_task(config, db, *, archive_dir=None, clock=utcnow, session_factory=None):
    # These dependencies are needed only for actual collection, never for --help
    # or the dependency-free scheduling/lock tests. No environment file is read.
    from signalforge.config.models import load_data_sources
    from signalforge.core.errors import TransientProviderError
    from signalforge.data.ingestion import ingest
    from signalforge.data.providers import (
        AAPL_FILE, MARKETPARQUET_FILE, SERIES_FILES, AlphaVantageProvider,
        FredSeriesProvider, LocalArchiveProvider, TiingoProvider,
    )
    from signalforge.data.storage import Warehouse

    sources = load_data_sources()
    policy = sources.providers[config.provider]
    if not policy.enabled or policy.status != "active":
        raise ValueError("provider must be explicitly active and enabled in data_sources.yaml")
    spacing = 60 / policy.rate_limit_per_minute if policy.rate_limit_per_minute else 0
    config = replace(config, retry_seconds=max(config.retry_seconds, spacing),
                     max_retry_seconds=max(config.max_retry_seconds, spacing))
    provider_types = {"fred": FredSeriesProvider, "tiingo": TiingoProvider,
                      "alphavantage": AlphaVantageProvider}
    archive_sources = {AAPL_FILE: "plotly_aapl_sample", MARKETPARQUET_FILE: "marketparquet",
                       **{name: "fred" for name in SERIES_FILES}}
    if config.provider == "local_archive":
        if any(name not in archive_sources for name in config.datasets):
            raise ValueError("unknown archive dataset")
        selected = sorted({archive_sources[name] for name in config.datasets}) if config.datasets else sorted(set(archive_sources.values()))
    else:
        selected = [config.provider]

    def read_latest(warehouse):
        latest = {}
        placeholders = ",".join("?" for _ in selected)
        for item in config.items:
            table, key, day = ("series", "series_id", "observation_date") if item in config.series_ids else ("bars", "symbol", "trade_date")
            latest[item] = warehouse.con.execute(
                f"SELECT max({day}) FROM {table} WHERE {key} = ? AND source IN ({placeholders})",
                [item, *selected],
            ).fetchall()[0][0]
        return latest

    class SafeProvider:
        name = config.provider

        def __init__(self, provider):
            self.provider = provider

        def fetch(self, **kwargs):
            try:
                return self.provider.fetch(**kwargs)
            except Exception as exc:
                # The ingestion failure log must not persist a vendor response,
                # exception URL, or credential-bearing request detail.
                raise CollectionFailure(retryable=isinstance(exc, TransientProviderError)) from None

    def task():
        now = clock()
        provider = None
        warehouse = None
        try:
            warehouse = Warehouse(db)
            if config.provider == "local_archive":
                provider = LocalArchiveProvider(archive_dir) if archive_dir else LocalArchiveProvider()
                kwargs = {"symbols": list(config.items)}
                if config.datasets:
                    kwargs["datasets"] = list(config.datasets)
            else:
                extra = {"session": session_factory()} if session_factory else {}
                provider = provider_types[config.provider](policy, user_agent=sources.user_agent, clock=clock, **extra)
                prior = [day for day in read_latest(warehouse).values() if day is not None]
                start = min([now.date(), *prior]) - timedelta(days=config.lookback_days)
                kwargs = {"start": start, "end": now.date()}
                kwargs["series_ids" if config.provider == "fred" else "symbols"] = list(config.items)
            result = ingest(SafeProvider(provider), warehouse, clock=clock, as_of=now.date(), **kwargs)
            return Snapshot(latest=read_latest(warehouse), rows_accepted=result.rows_accepted,
                            rows_quarantined=result.rows_quarantined, inserted=result.inserted,
                            revised=result.revised, unchanged=result.unchanged)
        finally:
            if warehouse is not None:
                warehouse.close()
            session = getattr(provider, "session", None)
            if session is not None:
                session.close()

    return config, task


def main(argv=None):
    args = parse_args(argv)
    collector = None
    try:
        config = load_config(args.config)
        config, task = build_task(config, args.db, archive_dir=args.archive_dir)
        collector = Collector(config, args.db, task, emit=lambda event: print(json.dumps(event, sort_keys=True)))
        result = collector.run(max_cycles=None if args.continuous else args.cycles)
        return 0 if result and result["status"] == "ok" else 2
    except KeyboardInterrupt:
        if collector is not None:
            collector.stop.set()
        return 130
    except CollectorBusy:
        print("collection refused: another collector holds this warehouse lock")
        return 3
    except Exception as exc:
        # Print the class only. Config/provider exception details may contain
        # externally supplied text. Detailed credentials never enter logs.
        print(f"collection failed ({type(exc).__name__}); inspect configuration and safe collector status")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
