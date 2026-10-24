These are small excerpts of successful LiteAPI sandbox responses captured on
2026-10-19 for Denver, Colorado (2026-11-18 through 2026-11-21).

Unneeded hotel fields and provider booking/offer tokens have been removed.
Review names, IDs, countries, dates and sources have been removed. Review text
has been replaced with anonymous test phrases; numeric scores are preserved.
The fixtures contain no API keys, authentication headers or guest identities.

`rates.json` is a two-adult, one-room request. `multiroom_rates.json` is four
adults in two rooms. The cheapest two-room offer is USD 1220.82 for all three
nights; its two individual stay totals are USD 610.41 each. This verifies that
nightly costs must be calculated from stay totals, and multi-room offers must
remain grouped. Quotes were captured separately and can differ between calls.

Unit tests load these files and mock the transport. They never call LiteAPI.
