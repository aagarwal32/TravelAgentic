"""US places and airports for the location and intake tests.

Coordinates and populations are close to the real Census and OurAirports values, and
the DOT market ids are the real ones, but this is a small stand-in for the real tables:
tests never read datasets/ or database.db. Import it after setting the test database
settings (see the top of each test module). Same-state collisions are loaded from
the small Census/GNIS fixtures to exercise the ingestion-to-intake boundary.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from pathlib import Path

from db.database import Base
from models.location import Airport, USPlace
from scripts.ingest_locations import place_id, read_places, read_population, read_city_centers
from services.location_service import normalize_name

# DOT city market ids (the ones Table 6 uses)
CHICAGO_MARKET = 30977
NEW_YORK_MARKET = 31703          # "New York City, NY (Metropolitan Area)": JFK, LGA, EWR
DALLAS_FORT_WORTH_MARKET = 30194
WASHINGTON_MARKET = 30852        # "Washington, DC (Metropolitan Area)": DCA, IAD, BWI
GREENVILLE_SC_MARKET = 31871     # "Greenville/Spartanburg, SC"
ST_LOUIS_MARKET = 31123
DENVER_MARKET = 30325

# (city, state, latitude, longitude, population)
PLACES = [
    ("Chicago", "IL", 41.8500, -87.6501, 2_711_226),
    ("New York", "NY", 40.7143, -74.0060, 8_483_844),
    ("Denver", "CO", 39.7392, -104.9847, 718_877),
    ("Nashville", "TN", 36.1718, -86.7850, 690_130),
    ("Fort Worth", "TX", 32.7254, -97.3208, 963_194),
    ("Dallas", "TX", 32.7831, -96.8067, 1_307_930),
    # Six hand-made Greenvilles; the two smaller NY places are ingested below.
    ("Greenville", "NC", 35.6127, -77.3664, 92_857),
    ("Greenville", "SC", 34.8526, -82.3940, 72_935),
    ("Greenville", "TX", 33.1384, -96.1108, 31_222),
    ("Greenville", "MS", 33.4101, -91.0618, 28_166),
    ("Greenville", "WI", 44.3003, -88.5365, 12_987),
    ("Greenville", "OH", 40.1028, -84.6330, 12_741),
    # One St. Louis (and one Charlotte) is far bigger than the other: still a card
    ("St. Louis", "MO", 38.6273, -90.1979, 288_512),
    ("St. Louis", "MI", 43.4084, -84.6067, 7_367),
    ("Charlotte", "NC", 35.2271, -80.8431, 903_844),
    ("Charlotte", "MI", 42.5636, -84.8358, 9_277),
    ("Charlottesville", "VA", 38.0293, -78.4767, 45_437),
    # Two Portlands: a card
    ("Portland", "OR", 45.5235, -122.6762, 641_165),
    ("Portland", "ME", 43.6615, -70.2553, 68_854),
    ("Washington", "DC", 38.8951, -77.0364, 681_294),
    ("Washington", "PA", 40.1740, -80.2462, 13_368),
    ("Los Angeles", "CA", 34.0522, -118.2437, 3_857_263),
    ("San Francisco", "CA", 37.7750, -122.4194, 830_235),
    ("Philadelphia", "PA", 39.9523, -75.1638, 1_579_706),
    ("Las Vegas", "NV", 36.1750, -115.1372, 660_400),
    ("New Orleans", "LA", 29.9546, -90.0751, 371_853),
    # A town named like a state: "Florida" alone means the state, not this town
    ("Florida", "NY", 41.3317, -74.3568, 2_768),
    # More than 120 km from every airport below
    ("Jordan", "MT", 47.3208, -106.9101, 321),
    # Issue #21 used "Atlantis" as a city that doesn't exist, but it is a real town in the
    # Census data, so it resolves; the tests use "Narnia" for a city that isn't there
    ("Atlantis", "FL", 26.5909, -80.1009, 2_038),
]

# (iata, name, city, state, latitude, longitude, DOT market id or None)
AIRPORTS = [
    ("ORD", "Chicago O'Hare International Airport", "Chicago", "IL", 41.9786, -87.9048, CHICAGO_MARKET),
    ("MDW", "Chicago Midway International Airport", "Chicago", "IL", 41.7868, -87.7522, CHICAGO_MARKET),
    ("JFK", "John F Kennedy International Airport", "New York", "NY", 40.6398, -73.7789, NEW_YORK_MARKET),
    ("LGA", "LaGuardia Airport", "New York", "NY", 40.7772, -73.8726, NEW_YORK_MARKET),
    ("EWR", "Newark Liberty International Airport", "Newark", "NJ", 40.6925, -74.1687, NEW_YORK_MARKET),
    ("DEN", "Denver International Airport", "Denver", "CO", 39.8617, -104.6731, DENVER_MARKET),
    ("BNA", "Nashville International Airport", "Nashville", "TN", 36.1245, -86.6782, 30693),
    ("DFW", "Dallas Fort Worth International Airport", "Dallas-Fort Worth", "TX", 32.8968, -97.0380, DALLAS_FORT_WORTH_MARKET),
    ("DAL", "Dallas Love Field", "Dallas", "TX", 32.8471, -96.8518, DALLAS_FORT_WORTH_MARKET),
    # Closest to Fort Worth but without a DOT market: Fort Worth's market comes from DFW
    ("FTW", "Fort Worth Meacham International Airport", "Fort Worth", "TX", 32.8198, -97.3624, None),
    ("GSP", "Greenville Spartanburg International Airport", "Greer", "SC", 34.8957, -82.2189, GREENVILLE_SC_MARKET),
    ("PGV", "Pitt-Greenville Airport", "Greenville", "NC", 35.6352, -77.3853, 34092),
    ("GLH", "Mid Delta Regional Airport", "Greenville", "MS", 33.4829, -90.9856, 31931),
    ("STL", "St Louis Lambert International Airport", "St Louis", "MO", 38.7487, -90.3700, ST_LOUIS_MARKET),
    ("CLT", "Charlotte Douglas International Airport", "Charlotte", "NC", 35.2140, -80.9431, 31057),
    ("CHO", "Charlottesville Albemarle Airport", "Charlottesville", "VA", 38.1386, -78.4529, 30990),
    ("PDX", "Portland International Airport", "Portland", "OR", 45.5887, -122.5975, 34057),
    ("PWM", "Portland International Jetport", "Portland", "ME", 43.6462, -70.3093, 34321),
    ("DCA", "Ronald Reagan Washington National Airport", "Washington", "DC", 38.8521, -77.0377, WASHINGTON_MARKET),
    ("IAD", "Washington Dulles International Airport", "Dulles", "VA", 38.9445, -77.4558, WASHINGTON_MARKET),
    ("BWI", "Baltimore/Washington International Airport", "Baltimore", "MD", 39.1754, -76.6683, WASHINGTON_MARKET),
    ("LAX", "Los Angeles International Airport", "Los Angeles", "CA", 33.9425, -118.4081, 32575),
    ("SFO", "San Francisco International Airport", "San Francisco", "CA", 37.6190, -122.3749, 32457),
    ("PHL", "Philadelphia International Airport", "Philadelphia", "PA", 39.8719, -75.2411, 34100),
    ("LAS", "Harry Reid International Airport", "Las Vegas", "NV", 36.0801, -115.1522, 32211),
    ("MSY", "Louis Armstrong New Orleans International Airport", "New Orleans", "LA", 29.9934, -90.2580, 33495),
    # OurAirports' current code for Palm Beach International (DOT still calls it PBI)
    ("DJT", "President Donald J. Trump International Airport", "West Palm Beach", "FL", 26.6832, -80.0956, 34027),
]


def place_rows() -> list[USPlace]:
    handmade = [USPlace(id=place_id(state, city), geoid=f"{index:07d}", city=city, state=state,
                    name_key=normalize_name(city), latitude=lat, longitude=lon, population=population)
            for index, (city, state, lat, lon, population) in enumerate(PLACES)]
    # Exercise the actual ingestion-to-lookup boundary for same-state collisions.
    fixtures = Path(__file__).parent / "fixtures" / "locations"
    rows, _ = read_places(fixtures / "gazetteer_sample.txt",
                         read_population(fixtures / "population_sample.dat"),
                         read_city_centers(fixtures / "gnis_sample.txt"))
    return handmade + [USPlace(**row) for row in rows
                      if row["state"] == "WI" or row["state"] == "NY"]


def airport_rows() -> list[Airport]:
    return [Airport(iata=iata, name=name, city=city, state=state, latitude=lat, longitude=lon,
                    dot_market_id=market)
            for iata, name, city, state, lat, lon, market in AIRPORTS]


def location_engine(filled: bool = True):
    """An in-memory database with the location tables (filled with the rows above by default).

    StaticPool keeps one shared connection, so every session sees the same database.
    """
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[USPlace.__table__, Airport.__table__])
    if filled:
        with sessionmaker(bind=engine)() as db:
            db.add_all(place_rows() + airport_rows())
            db.commit()
    return engine
