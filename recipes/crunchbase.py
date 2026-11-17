"""Crunchbase identity mint (crunchbase/fetch.py): Cloudflare fronts
www.crunchbase.com's /v4/data API; the pod keeps a pool of identities
(cf_clearance + __cf_bm + UA, each bound to its exit IP, retired after
CRUNCHBASE_ORG_QUOTA organization requests or CRUNCHBASE_IDENTITY_TTL).
The mint loads the discover page (never an organization page, which would
spend quota) through the exit GKE sends - direct first, then fresh US
residential ports - and returns once the real title shows. Some
residential exits are refused outright ("Attention Required" / "Access
denied"): that fails fast so GKE moves to the next exit."""
from recipes._cloudflare import CloudflareMint

SITE = "https://www.crunchbase.com"
SEED_PATH = "/discover/organization.companies"


class Crunchbase(CloudflareMint):
    name = "crunchbase"
    variants = {"": SITE + SEED_PATH}
    timeout = 60                      # CRUNCHBASE_HARVEST_TIMEOUT before 2026-10-22
    ttl_seconds = 1500                # config.CRUNCHBASE_IDENTITY_TTL (GKE enforces its own)


RECIPE = Crunchbase
