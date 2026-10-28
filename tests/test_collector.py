"""Dependency-free tests: python -m unittest tests.test_collector -v."""

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from signalforge.collector_cli import load_config, parse_args
from signalforge.data.collector import (
    CollectionFailure, Collector, CollectorBusy, CollectorConfig, Snapshot,
    WarehouseLock, next_run,
)


NOW = datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "warehouse.duckdb"
        self.now = NOW
        self.waits = []

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)
        return False

    def collector(self, task=None, config=None, **kw):
        return Collector(config or CollectorConfig(), self.db,
                         task or (lambda: Snapshot({"NASDAQCOM": date(2026, 10, 26),
                                                    "VIXCLS": date(2026, 10, 26)})),
                         clock=lambda: self.now, wait=self.wait, jitter=lambda: 1, **kw)

    def status(self, collector):
        return json.loads(collector.status_path.read_text(encoding="utf-8"))

    def test_default_is_one_bounded_cycle(self):
        calls = []
        c = self.collector(lambda: calls.append(1) or Snapshot({}))
        self.assertEqual(c.run()["status"], "stale")
        self.assertEqual(calls, [1])
        self.assertEqual(self.waits, [])

    def test_fresh_data_persists_utc_timestamp_and_counts(self):
        c = self.collector(lambda: Snapshot({"NASDAQCOM": NOW.date(), "VIXCLS": NOW.date()},
                                            rows_accepted=4, inserted=2, unchanged=2))
        self.assertEqual(c.run()["status"], "ok")
        event = self.status(c)
        self.assertEqual(event["checked_at"], NOW.isoformat())
        self.assertEqual((event["inserted"], event["unchanged"]), (2, 2))
        self.assertTrue(event["paper_only"])
        self.assertEqual(len(c.events_path.read_text().splitlines()), 1)

    def test_old_and_missing_items_are_stale(self):
        c = self.collector(lambda: Snapshot({"NASDAQCOM": date(2020, 1, 1)}))
        result = c.run()
        self.assertEqual(result["status"], "stale")
        self.assertEqual(set(result["stale"]), {"NASDAQCOM", "VIXCLS"})
        self.assertIsNone(result["stale"]["VIXCLS"]["latest"])

    def test_future_observation_is_not_healthy(self):
        c = self.collector(lambda: Snapshot({"NASDAQCOM": NOW.date() + timedelta(days=1)}))
        self.assertEqual(c.run()["stale"]["NASDAQCOM"]["age_days"], -1)

    def test_weekend_lag_is_tolerated(self):
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        c = self.collector(lambda: Snapshot({k: date(2026, 10, 23) for k in CollectorConfig().items}))
        self.assertEqual(c.run()["status"], "ok")

    def test_monthly_series_can_have_its_own_threshold(self):
        config = CollectorConfig(series_ids=("UNRATE",), stale_after_days_by_item={"UNRATE": 45})
        c = self.collector(lambda: Snapshot({"UNRATE": date(2026, 9, 30)}), config)
        self.assertEqual(c.run()["status"], "ok")

    def test_transient_failure_retries_with_exponential_backoff(self):
        calls = []
        def task():
            calls.append(1)
            if len(calls) < 3:
                raise CollectionFailure(retryable=True)
            return Snapshot({k: NOW.date() for k in CollectorConfig().items})
        c = self.collector(task)
        self.assertEqual(c.run()["status"], "ok")
        self.assertEqual(self.waits, [30, 60])
        self.assertEqual([json.loads(v)["status"] for v in c.events_path.read_text().splitlines()],
                         ["retrying", "retrying", "ok"])

    def test_retries_exhaust_and_stop(self):
        def task():
            raise CollectionFailure(retryable=True)
        c = self.collector(task)
        self.assertEqual(c.run()["status"], "failed")
        self.assertEqual(self.waits, [30, 60])
        self.assertEqual(self.status(c)["attempt"], 3)

    def test_permanent_failure_does_not_retry(self):
        def task():
            raise CollectionFailure(retryable=False)
        c = self.collector(task)
        self.assertEqual(c.run()["status"], "failed")
        self.assertEqual(self.waits, [])

    def test_exception_detail_does_not_enter_persistent_logs(self):
        def task():
            raise RuntimeError("private-marker-must-not-be-logged")
        c = self.collector(task)
        with self.assertRaises(RuntimeError):
            c.run()
        self.assertNotIn("private-marker", c.events_path.read_text())
        self.assertEqual(self.status(c)["status"], "failed")
        with WarehouseLock(self.db):
            pass  # failure released the lock

    def test_daily_budget_is_reserved_for_failed_requests_and_survives_restart(self):
        calls = []
        config = CollectorConfig(requests_per_day=1)
        def task():
            calls.append(1)
            raise CollectionFailure(retryable=True)
        c = self.collector(task, config)
        self.assertEqual(c.run()["status"], "budget_exhausted")
        other = self.collector(task, config)
        self.assertEqual(other.run()["status"], "budget_exhausted")
        self.assertEqual(len(calls), 1)
        self.now += timedelta(days=1)
        other.run()
        self.assertEqual(len(calls), 2)

    def test_invalid_budget_fails_closed_without_fetching(self):
        calls = []
        c = self.collector(lambda: calls.append(1))
        c.budget_path.write_text('{"fred":{"day":"2026-10-27","used":-1}}')
        with self.assertRaises(ValueError):
            c.run()
        self.assertEqual(calls, [])

    def test_invalid_or_future_budget_date_cannot_reset_quota(self):
        for day in ("not-a-date", "2027-01-01", None):
            with self.subTest(day=day):
                c = self.collector(lambda: self.fail("must not fetch"))
                c.budget_path.write_text(json.dumps({"fred": {"day": day, "used": 24}}))
                with self.assertRaises(ValueError):
                    c.run()

    def test_local_archive_does_not_consume_network_budget(self):
        config = CollectorConfig(provider="local_archive", series_ids=(), symbols=("AAPL",))
        c = self.collector(lambda: Snapshot({"AAPL": NOW.date()}), config)
        c.run()
        self.assertFalse(c.budget_path.exists())

    def test_daily_schedule_does_not_skip_a_day_after_completion(self):
        c = self.collector()
        c.run(max_cycles=2)
        self.assertEqual(self.waits, [86400])
        self.assertEqual(next_run(CollectorConfig(), NOW + timedelta(seconds=1)), NOW + timedelta(days=1))

    def test_interval_schedule_and_cycle_bound(self):
        config = CollectorConfig(daily_at_utc=None, interval_seconds=3600)
        c = self.collector(config=config)
        c.run(max_cycles=3)
        self.assertEqual(self.waits, [3600, 3600])

    def test_stop_interrupts_retry(self):
        def task():
            raise CollectionFailure(retryable=True)
        c = self.collector(task)
        c.wait = lambda _: True
        self.assertEqual(c.run()["status"], "stopped")

    def test_duplicate_lock_refuses_before_any_fetch(self):
        calls = []
        c = self.collector(lambda: calls.append(1))
        with WarehouseLock(self.db):
            with self.assertRaises(CollectorBusy):
                c.run()
        self.assertEqual(calls, [])

    def test_cross_process_lock_and_recovery(self):
        script = (
            "import sys\nfrom pathlib import Path\n"
            "from signalforge.data.collector import WarehouseLock, CollectorBusy\n"
            "try:\n with WarehouseLock(Path(sys.argv[1])): pass\n"
            "except CollectorBusy: sys.exit(3)\n"
        )
        with WarehouseLock(self.db):
            result = subprocess.run([sys.executable, "-c", script, str(self.db)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 3, result.stderr.decode())
        result = subprocess.run([sys.executable, "-c", script, str(self.db)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_canonical_path_aliases_share_a_lock(self):
        with WarehouseLock(self.db):
            with self.assertRaises(CollectorBusy):
                with WarehouseLock(self.db.parent / "." / self.db.name):
                    pass

    def test_lock_is_released_after_abrupt_process_exit(self):
        script = (
            "import os,sys\nfrom pathlib import Path\n"
            "from signalforge.data.collector import WarehouseLock\n"
            "lock=WarehouseLock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)\n"
        )
        result = subprocess.run([sys.executable, "-c", script, str(self.db)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with WarehouseLock(self.db):
            pass

    def test_cli_is_bounded_unless_explicitly_continuous(self):
        self.assertEqual(parse_args([]).cycles, 1)
        self.assertFalse(parse_args([]).continuous)
        self.assertTrue(parse_args(["--continuous"]).continuous)

    def test_checked_in_configuration_loads_without_optional_dependencies(self):
        self.assertEqual(load_config("config/collection.json").provider, "fred")
        self.assertEqual(load_config("config/collection-archive.json").symbols, ("AAPL",))
        free = load_config("config/collection-tiingo-free.json")
        self.assertEqual(free.provider, "tiingo")
        self.assertEqual(len(free.symbols), 23)
        self.assertEqual(free.requests_per_day, 46)
        self.assertEqual(free.max_attempts, 1)

    def test_invalid_configuration_is_refused(self):
        invalid = [
            {"provider": "unknown"}, {"max_attempts": 0}, {"max_attempts": 11},
            {"interval_seconds": float("nan")}, {"retry_seconds": -1},
            {"stale_after_days": 0}, {"daily_at_utc": "25:00"},
            {"series_ids": ["X", "X"]}, {"series_ids": "X"},
            {"stale_after_days_by_item": {"X": -1}}, {"requests_per_day": 0},
            {"max_retry_seconds": 1}, {"lookback_days": True},
            {"interval_seconds": 60, "daily_at_utc": None},
            {"interval_seconds": 3600},
            {"provider": "tiingo", "symbols": ["AAPL"]},
            {"provider": "local_archive", "symbols": ["NASDAQCOM"]},
            {"provider": "alphavantage", "series_ids": [], "symbols": ["AAPL"], "requests_per_day": 26},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(CollectorConfig(), **changes)


if __name__ == "__main__":
    unittest.main()
