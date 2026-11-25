# Duffel flight search

Set `DUFFEL_API_KEY` in `backend/.env` to a Duffel test-mode key for development.
`DUFFEL_DAILY_SEARCH_LIMIT` defaults to 50. The client uses Duffel v2 and reserves
one slot before sending each HTTP request, including failed requests, explicit
retries and offer refreshes. Missing credentials consume no slot. The counter
resets at midnight in the server's local timezone and each worker and restart
starts with its own counter. Use a live key only for the pre-demo run.

`services/flight_service.py` builds one-way and round-trip offer requests, parses
usable USD offers cheapest first, and calculates connection and baggage metrics.
`refresh_offer` refetches an individual offer through the same daily guard so a
caller can confirm its fare, expiry and baggage before displaying it. Duffel may
change the fare or provide additional details on refresh. [Get a single offer](https://duffel.com/docs/api/v2/offers#get-a-single-offer)

Duffel accepts airport and city IATA codes. The planner uses the airport codes
flight trends lists (or each place's own airport when trends has none): each
market's main airport by Table 1a passengers, plus the place's own airport. IATA
city codes aren't used because they don't match DOT markets (Providence, PVD, is in
DOT's Boston market). A flat offer request
returns all offers in one call, which can be hundreds or thousands across
airlines. Duffel also supports a grouped itineraries response and paginated
listing. The flat response keeps the existing parser and one counted HTTP call
per probe. [Flight concepts](https://duffel.com/docs/api/overview/flights-key-concepts) · [Offer requests](https://duffel.com/docs/api/v2/offer-requests/create-offer-request) · [Response formats](https://duffel.com/docs/guides/choosing-your-search-response-format)

The node spends at most ten search attempts per trip. For flexible dates it
starts with up to three spaced dates on the main home route, with preferences for
departures outside holidays and typical spring-break weeks. The cheapest probe
date is then used for every other route, in priority order: home to home (main
airports, then the place's own airport), alternative to home, alternative to
alternative, near miss to home. Searches left over go to near-tie probe dates (within
5%), the days next to the chosen date on the cheapest route, and the cheapest
alternative to the other home airport. Follow-ups stay inside the departure window
and preserve any fixed return date or stay length. A near miss's offers are kept
only when they beat the home airports' cheapest.

The shortlist holds up to 20 offers. The same flights often come back at several
fares (basic, main, refundable); only the cheapest fare per set of flights is kept,
plus the cheapest fare with a carry-on when the cheapest lacks one. It includes the
eligible cheapest, fastest, best-balanced, cheapest trends alternative and, when
there are alternatives, the cheapest home-airport offer (`home_airport`). Every
searched airport pair keeps its cheapest offer, so a route with fewer or pricier
flights is never crowded out by a busier one. Remaining slots are filled by the
balance score. Price and journey duration each carry twice
the weight of stops and longest layover. An offer with an overnight layover or
airport change is dropped when another offer covers the same airports and travel
dates, costs at most $10 more, takes at most 60 minutes longer and has no extra
stops or either risk. A round trip that comes back to a different airport
(`different_return_airport`: out of BUR, back to LGB) is dropped the same way when a
clean offer on the same dates qualifies, whatever its airports. `red_eye` marks a
flight in the air between 01:00 and 04:00 local time at either end (21:40 LAX →
06:02 BOS is one; 22:29 DEN → 23:58 SAN is not). Hotel search uses the cheapest
shortlisted offer's dates and fare to plan the stay and remaining budget.

## Test-mode smoke run

On November 16, 2026, the node searched Urbana, IL to New York, NY for two
adults, with a March 2027 departure window and a four-night round trip. Duffel
confirmed test mode. The node returned `status: ok`, `searches_made: 6`, and five
shortlisted offers. The cheapest was **$223.34 USD total**, departing March 2 and
returning March 6. This recorded run used the earlier five-offer cap; the current
cap is 20, verified by offline shortlist tests. The backend suite uses mocks,
including a [trimmed response from this run](../backend/tests/fixtures/duffel/round_trip.json).
