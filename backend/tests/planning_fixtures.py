"""Hand-made trips and contract checks for the planning node tests (no DB, no APIs)."""

PLACE = {"id": "us-xx-test", "city": "Testville", "state": "IL", "latitude": 40.0,
         "longitude": -88.0, "airports": ["CMI"], "dot_market_id": 31067}

# A finished intake: flying round trip with exact dates, 3 travelers, $900 total
TRIP = {"origin": "Testville", "destination": "Testville", "origin_place": PLACE,
        "destination_place": PLACE, "mode": "fly", "trip_type": "round_trip",
        "depart_date": "2099-03-10", "return_date": "2099-03-14", "budget": 900,
        "travelers": 3}

# The same trip with a departure window in March and a 2-night stay
WINDOW_TRIP = {**{k: v for k, v in TRIP.items() if k not in ("depart_date", "return_date")},
               "flexible_start": "2099-03-01", "flexible_end": "2099-03-31", "trip_nights": 2}


def state(constraints: dict, **results) -> dict:
    """A TripState holding these constraints plus any earlier node results."""
    return {"constraints": constraints, **results}


def assert_contract(test, result: dict, contract: type) -> None:
    """The result has a valid status, every required key, and no keys outside the TypedDict."""
    test.assertIn(result["status"], ("ok", "skipped", "empty"))
    test.assertLessEqual(contract.__required_keys__, result.keys())
    test.assertLessEqual(result.keys(), contract.__annotations__.keys())
    if result["status"] != "ok":
        test.assertTrue(result.get("reason"))
