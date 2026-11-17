"""Shared base for the "parked page, in-page fetch" fallback recipes (Google
News / Trends / Finance, Shine, Similarweb). Not a recipe itself (leading
underscore: registry_runtime skips it).

Shape on GKE before 2026-10-22: every curl attempt blocked -> one headed
Chrome on the site's fallback exit, parked on a home page, replaying the
call as an in-page fetch() so it carries the page's cookies and TLS; kept
open, closed after 300 s idle, dropped on a 403/429 so the next call starts
on a fresh exit. Here the box keeps the browser per proxy session (GKE
rotates the exit after a refusal), the worker's reaper does the idle close,
and a refused answer fails the lease so that browser is closed.

max_browsers (the site's guaranteed share) is 2 since 2026-11-04: the one
browser per key was the old in-pod pattern, not an anti-bot rule. GKE
offers config.INPAGE_BROWSER_SLOTS x BROWSER_BURST_FACTOR sticky exits per
key and the box opens more than the share only while it has headroom."""
from browser_worker.types import PageFetch, RecipeError

# headers a page-level fetch() must not carry (the browser sets its own)
_DROP_PREFIXES = ("sec-", "origin", "referer", "accept-language", "upgrade-", "host", "content-length",
                  "user-agent", "accept-encoding", "connection")


def clean_headers(headers):
    return {k: v for k, v in (headers or {}).items() if not k.lower().startswith(_DROP_PREFIXES)}


class ParkedInPage(PageFetch):
    """Subclasses set name, variants {"": HOME}, challenge_markers, and
    optionally refuse_statuses / park_url."""
    headless = False
    mode = "in_page"
    idle_seconds = 300
    max_browsers = 2                  # the share (= config.INPAGE_BROWSER_SLOTS)
    refuse_statuses = (403, 429)
    sorry_markers = ("unusual traffic",)          # Google's /sorry/ page body

    @property
    def home(self):
        return self.variants[""]

    def warm(self, driver, variant):
        driver.nav(self.seed_url(variant), referer="https://www.google.com/", mode="blocked",
                   challenge_markers=getattr(self, "challenge_markers", ()))

    def referrer_for(self, url) -> str:
        """The referrer an in-page fetch of `url` carries (the home page by
        default; multi-host recipes derive it from the URL's own host)."""
        return self.home

    def fetch_one(self, driver, url, method, headers, body):
        method = (method or "GET").upper()
        headers = clean_headers(headers)
        referrer = self.referrer_for(url)
        r = driver.get(url, headers=headers, referrer=referrer) if method == "GET" \
            else driver.request(method, url, headers=headers, body=body, referrer=referrer)
        if not r.status_code and r.error:
            raise RecipeError(f"{self.name}: in-page fetch failed: {r.error}")
        out = {"status": r.status_code, "headers": dict(r.headers or {}), "body": r.text,
               "final_url": r.url or url}
        if out["status"] in self.refuse_statuses or self.refused(out):
            # the exit is burned for this site: fail the lease so the box closes
            # this browser; GKE rotates its proxy and the next call relaunches
            raise RecipeError(self.refusal(out))
        return out

    def refusal(self, out) -> str:
        """The error text GKE sees for a refused answer (BoxRefused)."""
        return f"{self.name}: refused in the browser (HTTP {out['status']}); session dropped"

    def refused(self, out) -> bool:
        head = (out.get("body") or "")[:8000].lower()
        return "/sorry/" in (out.get("final_url") or "") or any(m in head for m in self.sorry_markers)
