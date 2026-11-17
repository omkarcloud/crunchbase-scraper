"""Shared bases for the Cloudflare-fronted sites of phase 3 group 4
(boomplay, crunchbase, envato Market). Not recipes themselves (leading
underscore: registry_runtime skips the module).

    CloudflareMint    open the seed page through the proxy GKE sends, let the
                      driver's canonical challenge settle (nav mode "blocked"
                      clicks the Turnstile checkbox), then poll until the REAL
                      page title shows; return the identity with whatever
                      Cloudflare cookies the exit issued. An unchallenged exit
                      shows the real page straight away and issues no
                      cf_clearance (verified on envato and crunchbase
                      residential exits, 2026-09-21): that is still a
                      successful mint here, because GKE decides curl-mode vs
                      browser-mode by replaying the cookies over curl on the
                      same sticky port, exactly as the in-pod mint did.
    CloudflareInPage  the browser-mode fallback: a kept browser per proxy
                      session (or caller session), warmed on the seed page,
                      serving the site's pages as in-page fetches with the
                      page's own clearance and TLS. A 403/429 or an
                      interstitial body fails the lease (the exit is burned).
"""
from browser_worker.types import CookieMint, RecipeError
from recipes._inpage import ParkedInPage

# Interstitial tells in a page TITLE (the mints poll titles) ...
TITLE_MARKERS = ("just a moment", "one moment, please", "attention required", "access denied",
                 "performing security verification")
# ... and in a response BODY (the in-page recipes inspect bodies).
BODY_MARKERS = ("just a moment", "cf-chl", "challenge-platform", "cf_chl_opt",
                "performing security verification", "attention required! | cloudflare",
                "sorry, you have been blocked")
# Titles that mean Cloudflare refuses this exit outright: no amount of waiting helps.
REFUSED_TITLES = ("attention required", "access denied")
COOKIES = ("cf_clearance", "__cf_bm")


class CloudflareMint(CookieMint):
    """Subclasses set name, variants and timeout; `real_title_marker` when
    the interstitial's title is indistinguishable from the site's own
    (boomplay titles its interstitial "www.boomplay.com")."""
    headless = False                     # headless never clears a managed challenge
    challenge_markers = BODY_MARKERS
    wait_for_cookie = "cf_clearance"
    require_challenge = False            # unchallenged exit = real page, no cookie: GKE's curl replay decides
    real_title_marker = None

    def title(self, driver):
        """The page title lowered, or None while Cloudflare is reloading the
        page (the evaluate dies mid-navigation; the next poll reads it)."""
        try:
            return (driver.title() or "").lower()
        except Exception:
            return None

    def passed(self, cookies, driver):
        title = self.title(driver)
        if title is None:
            return False
        if any(m in title for m in REFUSED_TITLES):
            raise RecipeError(f"{self.name}: Cloudflare refuses this exit ({title[:60]!r})")
        if not title or any(m in title for m in TITLE_MARKERS):
            return False
        if self.real_title_marker and self.real_title_marker not in title:
            return False
        return True

    def tokens(self, cookies, driver):
        return {k: cookies[k] for k in COOKIES if k in cookies}


class CloudflareInPage(ParkedInPage):
    """Subclasses set name, variants, home (the referrer), park_url."""
    challenge_markers = BODY_MARKERS
    refuse_statuses = (403, 429)
    sorry_markers = BODY_MARKERS

    def warm(self, driver, variant):
        res = driver.nav(self.seed_url(variant), referer="https://www.google.com/", mode="blocked",
                         challenge_markers=self.challenge_markers) or {}
        if res.get("challenge") in ("timeout", "refused"):
            raise RecipeError(f"{self.name}: Cloudflare challenge {res['challenge']} on "
                              f"{self.seed_url(variant)} ({(res.get('challenge_title') or '')[:60]!r})")
