"""Shared HTTP client: every LiteAPI call must use _request() for usage control."""

from datetime import date
from threading import Lock
from typing import Any, Optional

import httpx

from core.config import settings


class LiteAPIError(Exception):
    """A LiteAPI request failed or could not be attempted."""


# Per-process only: restarting resets usage; multiple workers do not share it.
_usage = {"day": None, "count": 0}
_usage_lock = Lock()


def _count_call() -> None:
    """Reserve one attempt; reset at midnight in the server's local timezone."""
    with _usage_lock:
        today = date.today()
        if _usage["day"] != today:
            _usage.update(day=today, count=0)
        if _usage["count"] >= settings.LITEAPI_DAILY_LIMIT:
            raise LiteAPIError("LiteAPI daily call limit reached")
        _usage["count"] += 1


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json: Optional[dict] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Return a LiteAPI JSON object; timeout is in seconds, failures raise LiteAPIError.

    Each invocation attempts at most one HTTP request. Failed attempts count
    toward the daily limit, but missing credentials do not.
    """
    if not settings.LITEAPI_API_KEY or not settings.LITEAPI_API_KEY.strip():
        raise LiteAPIError("LITEAPI_API_KEY is not set in backend/.env")

    url = f"{settings.LITEAPI_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "X-API-Key": settings.LITEAPI_API_KEY,
        "accept": "application/json",
    }
    # Never echo query strings, credentials, raw errors, or response bodies.
    label = f"LiteAPI {method} {path.split('?', 1)[0]}"
    label = label.replace(settings.LITEAPI_API_KEY, "[redacted]")
    _count_call()
    try:
        response = httpx.request(
            method, url, headers=headers, params=params, json=json, timeout=timeout
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise LiteAPIError(f"{label} returned HTTP {exc.response.status_code}") from None
    except httpx.TimeoutException:
        raise LiteAPIError(f"{label} timed out") from None
    except httpx.ConnectError:
        raise LiteAPIError(f"{label} could not connect") from None
    except httpx.HTTPError:
        raise LiteAPIError(f"{label} request failed") from None

    try:
        body = response.json()
    except ValueError:
        raise LiteAPIError(f"{label} returned invalid JSON") from None
    if not isinstance(body, dict):
        raise LiteAPIError(f"{label} returned an unexpected JSON type")
    return body
