"""Cache TTL per /crunchbase/* endpoint (cache.py, keyed on the validated
params — marshmallow fills the defaults, so `?page=1` and no `page` share
a row).

Company profiles are the expensive surface (11 organization requests per
minted Cloudflare cookie), so every company sub-endpoint caches for a day;
they also share one in-process entity cache (companies.py) so the fourteen
views of one company cost one upstream fetch. Searches and the deal
listings are cheap (no cookie quota) and move daily, so they cache for
hours; reference data (categories, locations, hubs) for days.
"""
from datetime import timedelta

AUTOCOMPLETE_CACHE = timedelta(days=2)
COMPANY_SEARCH_CACHE = timedelta(hours=6)
INVESTOR_SEARCH_CACHE = timedelta(hours=12)
PEOPLE_SEARCH_CACHE = timedelta(hours=12)
REFERENCE_CACHE = timedelta(days=7)        # categories, locations
HUB_SEARCH_CACHE = timedelta(days=1)
HUB_CACHE = timedelta(hours=12)

COMPANY_CACHE = timedelta(days=1)          # every /companies/* view
COMPANY_NEWS_CACHE = timedelta(hours=6)

PERSON_CACHE = timedelta(days=1)
PERSON_NEWS_CACHE = timedelta(hours=6)

ROUND_SEARCH_CACHE = timedelta(hours=6)
ROUND_CACHE = timedelta(days=1)
ACQUISITION_SEARCH_CACHE = timedelta(hours=6)
ACQUISITION_CACHE = timedelta(days=1)
INVESTMENT_SEARCH_CACHE = timedelta(hours=12)
IPO_SEARCH_CACHE = timedelta(hours=12)
IPO_CACHE = timedelta(days=1)
FUND_SEARCH_CACHE = timedelta(hours=12)
FUND_CACHE = timedelta(days=1)

EVENT_SEARCH_CACHE = timedelta(hours=12)
EVENT_CACHE = timedelta(days=1)
PRESS_SEARCH_CACHE = timedelta(hours=3)
NEWS_CACHE = timedelta(hours=1)
