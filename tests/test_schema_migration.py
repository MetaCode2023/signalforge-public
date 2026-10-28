"""Opening an older warehouse must not fail, and must never lose data.

Adding the `volume_known` column broke every pre-existing database:
`CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so the new
column was absent and `load_bars` raised
`Binder Error: Referenced column "volume_known" not found`.

The migration is deliberately additive only. It adds a missing column with a
default and does nothing else: no drops, no renames, no type changes, no
backfill that could rewrite a stored value. A migration that can lose data is
worse than a schema mismatch, because the mismatch is loud.
"""

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from signalforge.data.storage import Warehouse

D = Decimal

OLD_BARS = """
CREATE TABLE bars (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open DECIMAL(18,6),
    high DECIMAL(18,6),
    low DECIMAL(18,6),
    close DECIMAL(18,6),
    adj_close DECIMAL(18,6),
    volume BIGINT NOT NULL,
    source VARCHAR NOT NULL,
    retrieved_at TIMESTAMP,
    adjustment_method VARCHAR,
    quality_status VARCHAR,
    source_reference VARCHAR,
    PRIMARY KEY (symbol, trade_date, source)
);
"""


@pytest.fixture
def legacy_db(tmp_path):
    path = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(path))
    con.execute(OLD_BARS)
    con.execute(
        "INSERT INTO bars VALUES "
        "('AAA','2020-01-02',10,11,9,10.5,10.5,1234,'s',NULL,NULL,'OK',NULL)"
    )
    con.close()
    return path


class TestOpeningALegacyWarehouse:
    def test_it_opens_without_raising(self, legacy_db):
        wh = Warehouse(str(legacy_db))
        wh.close()

    def test_the_missing_column_is_added(self, legacy_db):
        wh = Warehouse(str(legacy_db))
        try:
            names = {
                r[0]
                for r in wh.con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'bars'"
                ).fetchall()
            }
            assert "volume_known" in names
        finally:
            wh.close()

    def test_existing_rows_are_preserved_exactly(self, legacy_db):
        wh = Warehouse(str(legacy_db))
        try:
            bars = wh.load_bars(symbols=["AAA"])
            assert len(bars) == 1
            bar = bars[0]
            assert bar.symbol == "AAA"
            assert bar.trade_date == date(2020, 1, 2)
            assert bar.close == D("10.5")
            assert bar.volume == 1234
            assert bar.source == "s"
        finally:
            wh.close()

    def test_an_existing_row_defaults_to_volume_known(self, legacy_db):
        # A row written before the column existed came from a provider that
        # did publish volume, so True is the correct default.
        wh = Warehouse(str(legacy_db))
        try:
            assert wh.load_bars(symbols=["AAA"])[0].volume_known is True
        finally:
            wh.close()

    def test_the_migration_is_idempotent(self, legacy_db):
        for _ in range(3):
            wh = Warehouse(str(legacy_db))
            wh.close()
        wh = Warehouse(str(legacy_db))
        try:
            assert len(wh.load_bars(symbols=["AAA"])) == 1
        finally:
            wh.close()

    def test_a_fresh_database_is_unaffected(self, tmp_path):
        wh = Warehouse(str(tmp_path / "fresh.duckdb"))
        try:
            names = {
                r[0]
                for r in wh.con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'bars'"
                ).fetchall()
            }
            assert "volume_known" in names
        finally:
            wh.close()

    def test_writing_after_migration_works(self, legacy_db):
        from tests.conftest import make_bar

        wh = Warehouse(str(legacy_db))
        try:
            wh.upsert_bars(
                [make_bar("BBB", date(2020, 1, 3), D("20"), volume=99, source="s")]
            )
            assert len(wh.load_bars(symbols=["BBB"])) == 1
            assert len(wh.load_bars(symbols=["AAA"])) == 1
        finally:
            wh.close()

    def test_the_migration_never_drops_a_column(self, legacy_db):
        before = {
            r[0]
            for r in duckdb.connect(str(legacy_db))
            .execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'bars'"
            )
            .fetchall()
        }
        wh = Warehouse(str(legacy_db))
        try:
            after = {
                r[0]
                for r in wh.con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'bars'"
                ).fetchall()
            }
        finally:
            wh.close()
        assert before <= after, f"columns disappeared: {before - after}"
