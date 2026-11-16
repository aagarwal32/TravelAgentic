"""Offline tests for the guarded Duffel client; every HTTP call is mocked."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
import traceback
import unittest
from unittest.mock import patch

import httpx

with patch.dict(os.environ, {
    "SECRET_KEY": "test-only", "DATABASE_URL": "sqlite://", "DEBUG": "false",
}):
    from services import duffel_client as client


class DuffelClientTests(unittest.TestCase):
    def setUp(self):
        for target, name, value in (
            (client.settings, "DUFFEL_API_KEY", "duffel_test_secret"),
            (client.settings, "DUFFEL_DAILY_SEARCH_LIMIT", 50),
            (client, "_usage", {"day": None, "count": 0}),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(client.httpx, "request")
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = self.response({"data": {"offers": []}})

    def response(self, body, status=200):
        return httpx.Response(
            status, json=body,
            request=httpx.Request("POST", "https://api.duffel.com/air/offer_requests"),
        )

    def test_missing_key_does_not_send_or_count(self):
        for key in (None, "", "   "):
            with self.subTest(key=key), patch.object(client.settings, "DUFFEL_API_KEY", key):
                with self.assertRaisesRegex(client.DuffelError, "DUFFEL_API_KEY is not set"):
                    client._request("POST", "/air/offer_requests")
        self.request.assert_not_called()
        self.assertEqual(client._usage, {"day": None, "count": 0})

    def test_post_sends_headers_and_body_and_returns_json(self):
        payload = {"data": {"slices": [], "passengers": [{"type": "adult"}]}}
        body = {"data": {"offers": []}}
        self.assertEqual(client._request("POST", "/air/offer_requests", json=payload), body)
        self.request.assert_called_once_with(
            "POST", "https://api.duffel.com/air/offer_requests",
            headers={
                "Authorization": "Bearer duffel_test_secret",
                "Duffel-Version": "v2",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            params=None, json=payload, timeout=30.0,
        )
        self.assertEqual(client._usage["count"], 1)

    def test_get_passes_params_and_timeout_and_counts_refetch(self):
        body = {"data": {"id": "off_example", "total_amount": "420.00"}}
        self.request.return_value = self.response(body)
        result = client._request(
            "GET", "air/offers/off_example",
            params={"return_available_services": False}, timeout=8.0,
        )
        self.assertEqual(result, body)
        args, kwargs = self.request.call_args
        self.assertEqual(args, ("GET", "https://api.duffel.com/air/offers/off_example"))
        self.assertEqual(kwargs["params"], {"return_available_services": False})
        self.assertEqual(kwargs["timeout"], 8.0)
        self.assertIsNone(kwargs["json"])
        self.assertEqual(client._usage["count"], 1)

    def test_attempt_is_counted_before_sending(self):
        def send(*args, **kwargs):
            self.assertEqual(client._usage["count"], 1)
            return self.response({"data": {"offers": []}})

        self.request.side_effect = send
        client._request("POST", "/air/offer_requests")
        self.request.assert_called_once()

    def test_last_allowed_request_succeeds_then_limit_blocks(self):
        client._usage.update(day=date.today(), count=49)
        client._request("POST", "/air/offer_requests")
        with self.assertRaisesRegex(client.DuffelError, "daily search limit"):
            client._request("POST", "/air/offer_requests")
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(client._usage["count"], 50)

    def test_zero_limit_blocks_all_requests(self):
        with patch.object(client.settings, "DUFFEL_DAILY_SEARCH_LIMIT", 0):
            with self.assertRaisesRegex(client.DuffelError, "daily search limit"):
                client._request("POST", "/air/offer_requests")
        self.request.assert_not_called()
        self.assertEqual(client._usage["count"], 0)

    def test_new_day_resets_limit(self):
        client._usage.update(day=date(2026, 1, 1), count=50)
        with patch.object(client, "date") as clock:
            clock.today.return_value = date(2026, 1, 2)
            client._request("POST", "/air/offer_requests")
        self.assertEqual(client._usage, {"day": date(2026, 1, 2), "count": 1})
        self.request.assert_called_once()

    def test_concurrent_requests_cannot_exceed_limit(self):
        def attempt(_):
            try:
                client._request("POST", "/air/offer_requests")
                return True
            except client.DuffelError:
                return False

        with patch.object(client.settings, "DUFFEL_DAILY_SEARCH_LIMIT", 10):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(attempt, range(40)))
        self.assertEqual(sum(results), 10)
        self.assertEqual(self.request.call_count, 10)
        self.assertEqual(client._usage["count"], 10)

    def test_http_failures_are_counted_and_sanitized(self):
        payload = {"data": "private-request"}
        for status in (401, 422, 429, 500):
            with self.subTest(status=status):
                self.request.return_value = self.response({
                    "error": "private-response duffel_test_secret",
                }, status)
                try:
                    client._request("POST", "/air/offer_requests", json=payload)
                except client.DuffelError as error:
                    self.assertEqual(
                        str(error), f"Duffel POST /air/offer_requests returned HTTP {status}",
                    )
                    formatted = traceback.format_exc()
                    self.assertNotIn("private-request", formatted)
                    self.assertNotIn("private-response", formatted)
                    self.assertNotIn(client.settings.DUFFEL_API_KEY, formatted)
                else:
                    self.fail("Expected DuffelError")
        self.assertEqual(client._usage["count"], 4)
        self.assertEqual(self.request.call_count, 4)

    def test_transport_failures_hide_raw_errors_and_query_strings(self):
        for error, message in (
            (httpx.ReadTimeout, "timed out"),
            (httpx.ConnectError, "could not connect"),
            (httpx.RemoteProtocolError, "request failed"),
        ):
            with self.subTest(error=error):
                self.request.side_effect = error("private-response duffel_test_secret")
                path = "/air/offer_requests?secret=" + client.settings.DUFFEL_API_KEY
                try:
                    client._request("POST", path)
                except client.DuffelError as caught:
                    self.assertEqual(str(caught), f"Duffel POST /air/offer_requests {message}")
                    formatted = traceback.format_exc()
                    self.assertNotIn("private-response", formatted)
                    self.assertNotIn(client.settings.DUFFEL_API_KEY, formatted)
                    self.assertNotIn("?secret=", str(caught))
                else:
                    self.fail("Expected DuffelError")
        self.assertEqual(client._usage["count"], 3)
        self.assertEqual(self.request.call_count, 3)

    def test_caller_retry_counts_without_automatic_retries(self):
        self.request.side_effect = [
            httpx.ReadTimeout("sensitive error"), self.response({"data": {"offers": []}}),
        ]
        with patch.object(client.settings, "DUFFEL_DAILY_SEARCH_LIMIT", 2):
            with self.assertRaises(client.DuffelError):
                client._request("POST", "/air/offer_requests")
            self.assertEqual(self.request.call_count, 1)
            client._request("POST", "/air/offer_requests")
            with self.assertRaisesRegex(client.DuffelError, "daily search limit"):
                client._request("POST", "/air/offer_requests")
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(client._usage["count"], 2)

    def test_token_in_error_label_is_redacted(self):
        self.request.return_value = self.response({}, 404)
        with self.assertRaises(client.DuffelError) as caught:
            client._request("GET", "/air/offers/" + client.settings.DUFFEL_API_KEY)
        self.assertEqual(
            str(caught.exception), "Duffel GET /air/offers/[redacted] returned HTTP 404",
        )

    def test_invalid_json_is_counted_and_sanitized(self):
        self.request.return_value = httpx.Response(
            200, content=b"invalid private-response duffel_test_secret",
            request=httpx.Request("POST", "https://api.duffel.com/air/offer_requests"),
        )
        try:
            client._request("POST", "/air/offer_requests")
        except client.DuffelError as error:
            self.assertIn("invalid JSON", str(error))
            formatted = traceback.format_exc()
            self.assertNotIn("private-response", formatted)
            self.assertNotIn(client.settings.DUFFEL_API_KEY, formatted)
        else:
            self.fail("Expected DuffelError")
        self.assertEqual(client._usage["count"], 1)

    def test_non_object_json_is_counted_and_rejected(self):
        for body in ([], "text", None, 42, True):
            with self.subTest(body=body):
                self.request.return_value = self.response(body)
                if body is None:
                    self.request.return_value = httpx.Response(
                        200, content=b"null",
                        request=httpx.Request("GET", "https://api.duffel.com/air/offers/off_example"),
                    )
                with self.assertRaisesRegex(client.DuffelError, "unexpected JSON type"):
                    client._request("GET", "/air/offers/off_example")
        self.assertEqual(client._usage["count"], 5)
