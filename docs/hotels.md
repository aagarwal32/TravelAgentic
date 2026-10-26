# Hotel search (LiteAPI)

Hotel data and prices come from [LiteAPI](https://www.liteapi.travel/). The
functions live in `backend/services/hotel_service.py`, and every request goes through
the guarded client in `backend/services/liteapi_client.py`. They are plain Python
functions; connecting them to the LangGraph agent is separate work, and the app
never books anything.

## Setup

Create a LiteAPI account and add your **sandbox** key to your own `backend/.env`.
Never commit that file.

```env
LITEAPI_API_KEY=<your sandbox key>
```

The sandbox returns real hotels and real market prices, but they can't be booked.

The key is optional when the app starts; hotel functions raise `LiteAPIError` if it
is missing. Optional settings:

| Setting | Default | Meaning |
|---|---|---|
| `LITEAPI_BASE_URL` | `https://api.liteapi.travel/v3.0` | API address |
| `LITEAPI_DAILY_LIMIT` | `200` | Requests allowed per process per day |

The daily counter is thread-safe but kept in memory: it resets when the server
restarts and isn't shared between processes, so it is not the account's real quota.
Requests have timeouts and are never retried automatically.

## Try it

From `backend/`, with the virtual environment active, in a Python shell
(`python`). Use dates in the future:

```python
from datetime import date
from services.hotel_service import search_hotels_with_prices

hotels = search_hotels_with_prices(
    "Denver", date(2026, 11, 18), date(2026, 11, 21),
    adults=2, rooms=1, radius_m=5000, max_total=600,
)
print(hotels[:3])
```

A search looks up the US city's center, then finds hotels nearby (`/data/hotels`),
then prices them (`/hotels/min-rates`).

## What the results mean

- **Prices** are USD for the whole stay and every requested room. `per_night` is the
  average total per night across those rooms. `max_total` is the budget for the
  whole stay.
- **Quotes** carry a `sandbox` flag and a UTC `fetched_at` time, and can change
  between requests.
- **Empty results:** an empty search returns `[]`, a city that isn't found returns
  `None`, missing details return `{}`, and an empty review summary has an unknown
  average and zero sampled reviews.
- **Distances** are straight-line kilometres. LiteAPI's minimum search radius is
  1000 m, so Python applies the requested radius itself, including smaller ones.
- **Not every hotel:** a search considers at most 50 hotels from LiteAPI, so it
  isn't a complete list of a city's hotels.
- **Room offers:** `get_room_offers` keeps multi-room offers grouped and returns USD
  stay totals. Refundability is marked unknown when cancellation information is
  missing, and a refundable rate can still have deadlines or penalties.
- **Amenities:** `find_facility_ids` matches exact facility names, ignoring case and
  punctuation, plus a few aliases such as WiFi, pool, and gym. Each name resolves to
  one facility, so "pool" never accidentally requires a pool bar.
- **Reviews:** averages and common phrases describe only the reviews requested, and
  no reviewer identities are returned. `total_reviews` is LiteAPI's full count.

## Tests

The hotel tests mock the API and use cleaned-up sandbox responses in
`backend/tests/fixtures/liteapi/`. They never call the real API. Run them with the
rest of the tests (see [Run the tests](../README.md#6-run-the-tests)).
