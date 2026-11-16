from datetime import date
from threading import Lock

import httpx

from core.config import settings


class DuffelError(Exception):
    """A Duffel request failed or could not be attempted."""


class DuffelSearchBlocked(DuffelError):
    """Further searches cannot succeed with the current credentials or allowance."""


# Per-process only: restarts reset usage; workers do not share this counter.
_usage = {"day": None, "count": 0}
_usage_lock = Lock()


def _count_call() -> None:
    """Reserve one attempt; reset daily in the server's local timezone."""
    with _usage_lock:
        today = date.today()
        if _usage["day"] != today:
            _usage.update(day=today, count=0)
        if _usage["count"] >= settings.DUFFEL_DAILY_SEARCH_LIMIT:
            raise DuffelSearchBlocked("Duffel daily search limit reached")
        _usage["count"] += 1


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    """Send one counted request; return a JSON object or raise DuffelError.

    No automatic retries. Failed attempts and caller retries count toward the
    daily limit; missing credentials do not.
    """
    if not settings.DUFFEL_API_KEY or not settings.DUFFEL_API_KEY.strip():
        raise DuffelSearchBlocked("DUFFEL_API_KEY is not set in backend/.env")

    url = f"https://api.duffel.com/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {settings.DUFFEL_API_KEY}",
        # https://duffel.com/docs/api/overview/versioning
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    # Exclude query strings, credentials, raw errors, and request/response bodies.
    label = f"Duffel {method} /{path.lstrip('/').split('?', 1)[0]}"
    label = label.replace(settings.DUFFEL_API_KEY, "[redacted]")
    _count_call()
    try:
        response = httpx.request(
            method, url, headers=headers, params=params, json=json, timeout=timeout
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        error = DuffelSearchBlocked if status in (401, 403, 429) else DuffelError
        raise error(f"{label} returned HTTP {status}") from None
    except httpx.TimeoutException:
        raise DuffelError(f"{label} timed out") from None
    except httpx.ConnectError:
        raise DuffelError(f"{label} could not connect") from None
    except httpx.HTTPError:
        raise DuffelError(f"{label} request failed") from None

    try:
        body = response.json()
    except ValueError:
        raise DuffelError(f"{label} returned invalid JSON") from None
    if not isinstance(body, dict):
        raise DuffelError(f"{label} returned an unexpected JSON type")
    return body
