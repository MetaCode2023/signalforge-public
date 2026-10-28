from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from signalforge.core.errors import ProviderError
from signalforge.core.history import PriceHistory
from signalforge.data.ingestion import IngestResult, ingest
from signalforge.data.providers import (
    AAPL_FILE,
    DEFAULT_ARCHIVE_DIR,
    MARKETPARQUET_FILE,
    SERIES_FILES,
    LocalArchiveProvider,
)
from signalforge.data.storage import Warehouse
from signalforge.data.validation import validate_bar_records, validate_series_records

BAR_DATASETS = (AAPL_FILE, MARKETPARQUET_FILE)


def ingest_archive(warehouse: Warehouse, archive_dir: str | Path = DEFAULT_ARCHIVE_DIR, *,
                   datasets=None, **kwargs) -> IngestResult:
    provider = LocalArchiveProvider(archive_dir)
    return ingest(provider, warehouse, datasets=datasets, **kwargs)


def load_archive_history(symbols=None, archive_dir: str | Path = DEFAULT_ARCHIVE_DIR, *,
                         datasets=BAR_DATASETS, as_of: date | None = None) -> PriceHistory:
    provider = LocalArchiveProvider(archive_dir)
    bars = []
    for result in provider.fetch(datasets=list(datasets), symbols=symbols):
        report = validate_bar_records(result.records, as_of=as_of)
        bars.extend(report.accepted)
    return PriceHistory.from_bars(bars)


def load_benchmark_series(series_id: str, archive_dir: str | Path = DEFAULT_ARCHIVE_DIR, *,
                          as_of: date | None = None) -> list[tuple[date, Decimal]]:
    provider = LocalArchiveProvider(archive_dir)
    for result in provider.fetch(datasets=list(SERIES_FILES), symbols=[series_id]):
        if result.records:
            report = validate_series_records(result.records, as_of=as_of)
            return sorted((r["observation_date"], r["value"]) for r in report.accepted)
    raise ProviderError(f"series {series_id!r} not found in the local archive")
