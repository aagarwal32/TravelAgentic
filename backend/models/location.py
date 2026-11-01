"""ORM models for the location tables: US places and US airports.

Both tables are loaded by scripts/ingest_locations.py and read by
services/location_service.py, which turns what the user typed ("Greenville, SC")
into one exact place during intake.

Sources (see docs/data.md):
- us_places: Census Bureau 2024 Gazetteer places file joined with the ACS 2020-2024
  5-year population table B01003, with city-center coordinates from the USGS GNIS
  where it has them. Public domain.
- airports: OurAirports airports.csv (public domain), linked to DOT city markets
  with DOT Consumer Airfare Report Table 1a.
"""

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base


class USPlace(Base):
    """One US city, town or census-designated place (CDP)."""

    __tablename__ = "us_places"

    # Choice-card id: "us-sc-greenville"; same-state name collisions append the
    # Census GEOID (e.g. "us-ny-greenville-3630609"), independent of population rank.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    # The Census GEOID (state + place code, e.g. "4530850"): the dataset's own id.
    geoid: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    # Display name; same-state collisions include type and county (or coordinates).
    city: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String(2), nullable=False)  # USPS code, "SC"
    # The city name cleaned up for exact matching ("St. Louis" -> "saint louis"), made
    # by location_service.normalize_name. Re-run ingestion if that function changes.
    name_key: Mapped[str] = mapped_column(String, nullable=False)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    population: Mapped[int] = mapped_column(Integer, nullable=False)  # orders the choice card

    __table_args__ = (
        # find_places looks a city up by its cleaned name, optionally within one state.
        Index("ix_us_places_name_key_state", "name_key", "state"),
    )


class Airport(Base):
    """One US airport with scheduled passenger flights."""

    __tablename__ = "airports"

    iata: Mapped[str] = mapped_column(String(3), primary_key=True)  # "GSP"
    name: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str | None] = mapped_column(String, nullable=True)  # OurAirports "municipality"
    state: Mapped[str] = mapped_column(String(2), nullable=False)    # "US-SC" -> "SC"
    # Indexed: nearby_airports narrows the search to a band of latitude first.
    latitude: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    # The DOT Table 6 city market this airport belongs to (DFW and DAL are both
    # "Dallas/Fort Worth, TX"). None when DOT has no market for it.
    dot_market_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
