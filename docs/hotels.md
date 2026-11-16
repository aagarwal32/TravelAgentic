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

A search finds hotels near the city (`/data/hotels`), then prices them
(`/hotels/min-rates`). The service accepts the trip's resolved place
(`TripConstraints.destination_place`, see [agent.md](agent.md#places-origin-and-destination))
instead of a name, and its coordinates are used directly. A city name, as above, still
works for testing by hand: its center is looked up first with `get_city_location`
(1-6 more LiteAPI calls). The intake graph currently stops at a planning placeholder;
calling hotel search with the saved place still needs agent integration.

## Search radius and tuning

The defaults are grouped at the top of `backend/services/hotel_service.py`.
For example, set `RADIUS_STEPS_KM["fly"]` to `(10, 15, 20)` to start at 10 km
and widen by 5 km each time. The entries are total distances from the same center,
so list them in increasing order.

| Setting | What it changes |
|---|---|
| `DEFAULT_RADIUS_KM` | One-pass distance when neither a mode nor a radius is given (5 km). |
| `RADIUS_STEPS_KM` | Total radii for flying (10, 20, 40 km) and driving (25, 40 km). |
| `MIN_AFFORDABLE_HOTELS` | Stop widening when this many unique hotels have a quote within budget (5). |
| `HOTEL_LIST_LIMIT`, `WIDENING_HOTEL_LIST_LIMIT` | Maximum listings per pass for one-pass (50) and widening (150) searches. Larger limits can give later passes more candidates. |
| `PRICE_CANDIDATE_LIMIT` | Maximum unquoted candidates to send in a single rates request (50). Check the provider's request cap before increasing it. |

To try widening, pass `mode="fly"` or `mode="drive"` to
`search_hotels_with_prices` without a `radius_m` override. An override such as
`radius_m=15000` forces a single pass. Each pass uses one list call and at most
one rates call. Wider passes skip hotels already sent for quotes and keep the
affordable quotes from earlier passes. The planning node still needs to be
connected to live hotel search.

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
- **Not every hotel:** LiteAPI candidates are capped per pass, so the search
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
