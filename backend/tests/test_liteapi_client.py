"""Offline client tests; no real credentials or network requests are needed."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
import traceback
import unittest
from unittest.mock import patch

import httpx

# Supply required application settings when importing from the repository root.
with patch.dict(os.environ, {
    "SECRET_KEY": "test-only", "DATABASE_URL": "sqlite://", "DEBUG": "false",
}):
    from services import liteapi_client as client


class LiteAPIClientTests(unittest.TestCase):
    def setUp(self):
        original_usage = client._usage.copy()
        self.addCleanup(client._usage.update, original_usage)
        client._usage.update(day=None, count=0)
        for name, value in {
            "LITEAPI_API_KEY": "test-secret",
            "LITEAPI_BASE_URL": "https://example.test/v3.0",
            "LITEAPI_DAILY_LIMIT": 200,
        }.items():
            patcher = patch.object(client.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(client.httpx, "request")
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = self.response({"data": []})

    def response(self, body, status=200):
        return httpx.Response(status, json=body,
                              request=httpx.Request("GET", "https://example.test"))

    def test_missing_or_blank_key_does_not_count(self):
        for key in (None, "", "   "):
            with self.subTest(key=key), patch.object(client.settings, "LITEAPI_API_KEY", key):
                with self.assertRaisesRegex(client.LiteAPIError, "not set"):
                    client._request("GET", "/data/hotels")
        self.request.assert_not_called()
        self.assertEqual(client._usage["count"], 0)

    def test_get_returns_body_and_passes_options(self):
        body = {"data": [{"id": "hotel-1"}], "sandbox": True}
        self.request.return_value = self.response(body)
        self.assertEqual(client._request("GET", "/data/hotels", params={"limit": 2}), body)
        self.request.assert_called_once_with(
            "GET", "https://example.test/v3.0/data/hotels",
            headers={"X-API-Key": "test-secret", "accept": "application/json"},
            params={"limit": 2}, json=None, timeout=15.0,
        )
        self.assertEqual(client._usage["count"], 1)

    def test_post_body_and_custom_timeout(self):
        body = {"hotelIds": ["hotel-1"]}
        client._request("POST", "/hotels/min-rates", json=body, timeout=8.0)
        self.assertEqual(self.request.call_args.args[0], "POST")
        self.assertEqual(self.request.call_args.kwargs["json"], body)
        self.assertEqual(self.request.call_args.kwargs["timeout"], 8.0)

    def test_url_slashes(self):
        with patch.object(client.settings, "LITEAPI_BASE_URL", "https://example.test/v3.0/"):
            client._request("GET", "data/hotels")
        self.assertEqual(self.request.call_args.args[1], "https://example.test/v3.0/data/hotels")

    def test_limit_blocks_before_network(self):
        client._usage.update(day=date.today(), count=199)
        client._request("GET", "/data/hotels")
        with self.assertRaisesRegex(client.LiteAPIError, "daily call limit"):
            client._request("GET", "/data/hotels")
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(client._usage["count"], 200)

    def test_new_day_resets_counter(self):
        client._usage.update(day=date(2026, 1, 1), count=200)
        with patch.object(client, "date") as mock_date:
            mock_date.today.return_value = date(2026, 1, 2)
            client._count_call()
        self.assertEqual(client._usage, {"day": date(2026, 1, 2), "count": 1})

    def test_concurrent_reservations_respect_limit(self):
        def reserve(_):
            try:
                client._count_call()
                return True
            except client.LiteAPIError:
                return False
        with patch.object(client.settings, "LITEAPI_DAILY_LIMIT", 10):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(reserve, range(40)))
        self.assertEqual(sum(results), 10)
        self.assertEqual(client._usage["count"], 10)

    def test_http_errors_are_normalized_and_counted(self):
        for status in (401, 429, 500):
            with self.subTest(status=status):
                self.request.return_value = self.response({"error": "test-secret"}, status)
                with self.assertRaisesRegex(client.LiteAPIError, f"HTTP {status}") as caught:
                    client._request("GET", "/data/hotels")
                self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(client._usage["count"], 3)
        self.assertEqual(self.request.call_count, 3)  # No retries.

    def test_transport_errors_hide_original_exception(self):
        for error, message in (
            (httpx.ReadTimeout, "timed out"),
            (httpx.ConnectError, "could not connect"),
            (httpx.RemoteProtocolError, "request failed"),
        ):
            with self.subTest(error=error):
                self.request.side_effect = error("sensitive test-secret")
                path = "/data/hotels?apiKey=" + client.settings.LITEAPI_API_KEY
                try:
                    client._request("GET", path)
                except client.LiteAPIError as exc:
                    self.assertIn(message, str(exc))
                    self.assertNotIn("test-secret", traceback.format_exc())
                else:
                    self.fail("Expected LiteAPIError")
        self.assertEqual(client._usage["count"], 3)

    def test_invalid_json(self):
        self.request.return_value = httpx.Response(
            200, content=b"not json test-secret",
            request=httpx.Request("GET", "https://example.test"),
        )
        with self.assertRaisesRegex(client.LiteAPIError, "invalid JSON"):
            client._request("GET", "/data/hotels")

    def test_non_object_json(self):
        for body in ([], "text", None, 42):
            with self.subTest(body=body):
                self.request.return_value = self.response(body)
                # Explicit null because httpx's json=None means no body.
                if body is None:
                    self.request.return_value = httpx.Response(
                        200, content=b"null", request=httpx.Request("GET", "https://example.test")
                    )
                with self.assertRaisesRegex(client.LiteAPIError, "unexpected JSON type"):
                    client._request("GET", "/data/hotels")


if __name__ == "__main__":
    unittest.main()
