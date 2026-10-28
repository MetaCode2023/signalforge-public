"""Provider error classification without pytest, DuckDB, or outbound requests."""

import unittest
from datetime import datetime, timezone
from pathlib import Path

import requests

from signalforge.config.models import load_data_sources
from signalforge.core.errors import ProviderError, TransientProviderError
from signalforge.data.providers import FredSeriesProvider


class Response:
    def __init__(self, status):
        self.status_code = status
        self.text = (Path(__file__).parent / "fixtures" / "fred_sample.csv").read_text()


class Session:
    def __init__(self, result):
        self.result = result

    def get(self, *args, **kwargs):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ProviderCollectionTests(unittest.TestCase):
    def provider(self, result):
        return FredSeriesProvider(load_data_sources().providers["fred"],
                                  session=Session(result), sleep=lambda _: None,
                                  clock=lambda: datetime(2024, 1, 5, tzinfo=timezone.utc))

    def test_throttle_and_server_failures_are_retryable(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status), self.assertRaises(TransientProviderError):
                self.provider(Response(status)).fetch(["DGS10"])

    def test_auth_and_invalid_requests_are_not_retryable(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                try:
                    self.provider(Response(status)).fetch(["DGS10"])
                except ProviderError as exc:
                    self.assertNotIsInstance(exc, TransientProviderError)
                else:
                    self.fail("request should have been refused")

    def test_connection_and_timeout_errors_hide_transport_details(self):
        for error in (requests.ConnectionError("private-request-detail"),
                      requests.Timeout("private-request-detail")):
            with self.subTest(kind=type(error).__name__):
                with self.assertRaises(TransientProviderError) as caught:
                    self.provider(error).fetch(["DGS10"])
                self.assertNotIn("private-request-detail", str(caught.exception))

    def test_fixture_fetch_keeps_provider_timestamp(self):
        result, = self.provider(Response(200)).fetch(["DGS10", "DGS2"])
        self.assertEqual(len(result.records), 5)
        self.assertTrue(all(r["retrieved_at"] == result.retrieved_at for r in result.records))


if __name__ == "__main__":
    unittest.main()
