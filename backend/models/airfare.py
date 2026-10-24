"""ORM model for the DOT Consumer Airfare Report, Table 6.

One row = one city pair in one quarter of one year. Fares are each-way averages
in US dollars, and a city pair has a single row for both directions (either city
can be city1). Source and column descriptions:
https://data.transportation.gov/d/yj5y-b2ir
"""

from sqlalchemy import BigInteger, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base


class AirfareQuarter(Base):
    """Average fares for one city pair in one quarter (one row of Table 6)."""

    __tablename__ = "dot_table6_fares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # DOT's own row id. BigInteger because it is a long number and PostgreSQL's
    # plain INTEGER (32-bit) would overflow later when we switch databases.
    tbl6pk: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 = Jan-Mar ... 4 = Oct-Dec

    # DOT's ids for each city market: the reliable way to match a route.
    citymarketid_1: Mapped[int] = mapped_column(Integer, nullable=False)
    citymarketid_2: Mapped[int] = mapped_column(Integer, nullable=False)
    city1: Mapped[str] = mapped_column(String, nullable=False, index=True)
    city2: Mapped[str] = mapped_column(String, nullable=False, index=True)

    nsmiles: Mapped[int] = mapped_column(Integer, nullable=False)    # nonstop distance in miles
    passengers: Mapped[float] = mapped_column(Float, nullable=False)  # passengers per day
    fare: Mapped[float] = mapped_column(Float, nullable=False)        # average fare, all airlines

    # Carrier columns: nullable in case DOT leaves one empty.
    carrier_lg: Mapped[str | None] = mapped_column(String, nullable=True)   # largest airline (e.g. "AA")
    large_ms: Mapped[float | None] = mapped_column(Float, nullable=True)    # its market share, 0-1
    fare_lg: Mapped[float | None] = mapped_column(Float, nullable=True)     # its average fare
    carrier_low: Mapped[str | None] = mapped_column(String, nullable=True)  # lowest-fare airline
    lf_ms: Mapped[float | None] = mapped_column(Float, nullable=True)       # its market share, 0-1
    fare_low: Mapped[float | None] = mapped_column(Float, nullable=True)    # its average fare

    __table_args__ = (
        # The query functions look routes up by the two market ids.
        Index("ix_dot_table6_fares_markets", "citymarketid_1", "citymarketid_2"),
    )
