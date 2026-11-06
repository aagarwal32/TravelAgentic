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
(1-6 more LiteAPI calls). The graph includes a `hotel_search` stub that calculates
search inputs; connecting it to the real LiteAPI search is still separate work.

## Agent stay dates

The agent's `stay_dates()` helper in `services/agent/nodes/hotel_search.py` uses
the cheapest shortlisted flight's dates first, then the first fare-trends date
candidate. When neither provides dates, it falls back to the trip constraints:

| Known trip details | Check-in | Check-out |
|---|---|---|
| Exact departure and return | Departure date | Return date |
| Departure window, return, and nights | Return minus nights | Return date |
| Departure window and return, no nights | Window start | Return date |
| Departure window and nights, no return | Window start | Check-in plus nights |

For example, a March window, a March 15 return, and 2 nights produce a March 13–15
stay, not March 1–15. Intake validates that the implied departure fits the window.
The helper returns no stay when checkout is missing or is not after check-in.

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
