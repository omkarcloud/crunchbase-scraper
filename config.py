"""Configuration for the Crunchbase Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT              port the API listens on (default 8000)
    CRUNCHBASE_PROXY  proxy URL for every request, e.g. http://user:pass@host:port
                      (default: none — direct). Crunchbase sits behind
                      Cloudflare, so the scraper opens it once in a real
                      Chrome (patchright), then carries on over plain HTTP
                      with that session. A clean home IP clears in a few
                      seconds. Set a proxy only if your IP gets blocked.

Everything else below is a plain constant with a working default — edit it
here if you need to.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

# Retry policy for transport errors and blocks (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, multiplied by the attempt number

CRUNCHBASE_PROXY = os.environ.get("CRUNCHBASE_PROXY") or None

# Session pool (crunchbase/fetch.py). Crunchbase allows ~11 company-profile
# requests per Cloudflare session, so a fresh session is opened in Chrome
# after CRUNCHBASE_ORG_QUOTA of them. Everything else has no such limit.
CRUNCHBASE_PROXY_COUNTRY = "proxy" if CRUNCHBASE_PROXY else None   # set = every session uses CRUNCHBASE_PROXY
CRUNCHBASE_FALLBACK_PROXY_COUNTRY = None   # no fallback exit: the proxy comes from CRUNCHBASE_PROXY
CRUNCHBASE_ORG_QUOTA = 10                  # company-profile requests per session
CRUNCHBASE_IDENTITY_TTL = 1500             # seconds a session is trusted
CRUNCHBASE_HARVEST_ATTEMPTS = 3            # tries to open a session through the proxy
CRUNCHBASE_HARVEST_TIMEOUT = 60            # seconds for Cloudflare to clear
CRUNCHBASE_HARVEST_COOLDOWN = 180          # seconds to wait after a failed attempt
CRUNCHBASE_RATE_LIMIT_COOLDOWN = 150       # seconds to rest after a Cloudflare rate limit
CRUNCHBASE_BROWSER_IDLE = 900              # close the Chrome after this many idle seconds


def crunchbase_proxy(country=None):
    """The proxy every request goes through (None = direct)."""
    return os.environ.get("CRUNCHBASE_PROXY") or None


# ---- added by the sync: defaults for a direct run ----
CRUNCHBASE_IDENTITIES = 3
CRUNCHBASE_PARALLEL_MINTS = 3
INPAGE_BROWSER_SLOTS = 1
def burst_slots(share):
    return share


def exit_geo(proxy, timeout=12):
    """Geo of a proxy's exit IP, looked up THROUGH the proxy (ip-api.com, free
    tier, keyed per exit IP so the quota is never shared). Returns the dict
    {countryCode, regionName, city, timezone, query} or None on any failure —
    callers keep their configured/system timezone then."""
    try:
        import requests
        r = requests.get(
            "http://ip-api.com/json/?fields=status,countryCode,regionName,city,timezone,query",
            proxies={"http": proxy, "https": proxy}, timeout=timeout)
        d = r.json()
        if d.get("status") == "success" and d.get("timezone"):
            return d
    except Exception:
        pass
    return None

