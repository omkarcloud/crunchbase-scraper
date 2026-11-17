"""The four recipe types. A site recipe subclasses one, sets a few class
attributes, and (rarely) overrides a hook. Everything about *how* to get
past a site lives here or in the recipe; nothing about *what* to fetch.

    cookie_mint   open a seed page, wait for a cookie/JS value, return cookies
                  + UA + client hints; close in the same call. Never kept.
    token_mint    a parked page; per request run the site's token call
                  (BotGuard, reCAPTCHA) and return the token. Kept, idle-closed.
    page_fetch    a parked page; per request navigate or in-page fetch the
                  caller's URL and return the raw response. Kept, idle-closed.
    session_fetch like page_fetch, but the browser is pinned to a caller
                  `session` id and batches of URLs run on it (in-page
                  get_many). Kept, idle-closed per session.

Common attributes (defaults in browser_worker.settings):
    name            site key = URL prefix (/<name>/mint ...)
    box             "windows" | "ubuntu" (registry.py decides; this is the declared home)
    engine          "patchright" (real Chrome, the default) | "camoufox"
                    (Firefox, camoufox_driver): ONLY for the two sites that
                    punish the Chrome engine itself (aliexpress product, until 2026-10-29) or
                    accept the captcha solver's answer only from Firefox on a desktop
                    (google-search) - owner decision 2026-10-23. Camoufox is
                    imported lazily inside launch(), so a box without the
                    camoufox stack (Ubuntu) still loads the recipe and only
                    fails when asked to serve it.
    extensions      camoufox only: extracted Firefox extension dirs to load
    locale          browser locale ("ko-KR"); None = derived from the exit
                    (patchright: egress.exit_profile -> Chrome --lang +
                    --accept-lang on every OS; camoufox: geoip). Set it only
                    when the site needs a language that differs from its exit.
    block_images    camoufox only: Firefox image-loading pref (True = off)
    screen          camoufox only: (min_w, max_w, min_h, max_h) browserforge
                    Screen constraint; None = from the real display (a small
                    VM console matches no fingerprint, so recipes set it)
    headless        default False (headed Chrome: on Windows a real desktop,
                    on Ubuntu under Xvfb)
    block_resources ("image", "font", "media") etc., or None
    block_hosts     trackers to abort, or None
    direct_hosts    hosts that bypass the proxy (static CDNs), or None
    geoip_timezone  True: set Chrome's timezone from the proxy exit
    timezone        a fixed Chrome timezone ("Europe/London"); None = the box's TZ
                    (reaches Chrome as TZ in its process environment on Linux
                    and macOS; Windows Chrome ignores TZ, so only there it is
                    the Playwright timezone_id emulation)
    variants        {variant: seed_url}; "" = default
    park_url        where a kept page idles (None = stay on the last page)
    park            False for recipes whose token needs the live page
    idle_seconds, max_uses, max_age, max_rss_mb, max_browsers
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from . import settings
from .lifecycle import REGISTRY, Lease


from launch_lock import LAUNCH_LOCK as _LAUNCH_LOCK   # one lock for both drivers' spawn windows

# Per request (server.py sets and clears it): `candidates`, the browsers the
# caller can use interchangeably [{"proxy", "session"}, ...], and `chosen`,
# the index of the one the box used — set before a launch starts, so a
# refusal during warm-up still tells GKE which exit to rotate.
REQUEST = threading.local()


def request_candidates():
    return getattr(REQUEST, "candidates", None) or None


def chosen_candidate():
    return getattr(REQUEST, "chosen", None)


class RecipeError(Exception):
    """Recipe failed to pass the site (challenge not cleared, token absent)."""


# The last language/exit check per site, for /status: {site: {market, exit,
# source, locale, mismatch, at}}. A localized recipe (market_country) whose
# browser leaves through an exit in another country is logged as a MISMATCH,
# never refused: the owner decides per site whether to move its exit
# (2026-10-29: tripadvisor / trustpilot / indeed stay as they are, flagged).
LOCALE_CHECKS: dict = {}


class Recipe:
    name: str = ""
    box: str = "ubuntu"
    engine: str = "patchright"       # | "camoufox"
    extensions: tuple = ()           # camoufox: extracted Firefox extension dirs
    locale: Optional[str] = None     # None = derived from the exit country (both engines)
    block_images: bool = True        # camoufox: Firefox pref (block_resources "image" is Chrome-only)
    screen: Optional[tuple] = None   # camoufox: (min_w, max_w, min_h, max_h)
    headless: bool = False
    block_resources = None
    block_hosts = None
    direct_hosts = None
    geoip_timezone = True
    # Fixed Intl timezone for sites whose pod used to pin TZ (etsy, zoopla);
    # geoip_timezone wins when both are set (patchright_driver resolves it).
    timezone: Optional[str] = None
    browserforge: bool = False
    chrome_args: tuple = ()          # extra Chrome switches (see patchright_driver extra_args)
    channel: Optional[str] = None    # patchright browser: "msedge" | "chrome"; None = the process default (real Chrome)
    variants: dict = {"": None}
    park_url: Optional[str] = None
    park: bool = True
    park_in_background: bool = False # park after the reply is sent (lifecycle.release)
    idle_seconds: int = settings.IDLE_SECONDS
    max_uses: int = settings.MAX_USES
    max_age: int = settings.MAX_AGE_SECONDS
    max_rss_mb: int = settings.MAX_RSS_MB
    max_browsers: int = settings.MAX_BROWSERS_PER_RECIPE
    actions: tuple = ()

    # ---- language / exit consistency --------------------------------------------

    def market_country(self, variant: str, url: Optional[str] = None) -> Optional[str]:
        """The country of the storefront this request is for (ISO code), for
        recipes whose variant / host picks a localized site; None = the site
        is not localized. Used only for the locale-check log and /status."""
        return None

    def _locale_check(self, driver, variant: str, url: Optional[str] = None) -> None:
        try:
            market = (self.market_country(variant, url) or "").lower() or None
        except Exception as e:                      # a log aid, never a failure
            print(f"locale-check: site={self.name} market_country failed: {e}")
            market = None
        exit = getattr(driver, "_exit", None)
        exit_cc = getattr(exit, "country", None)
        rec = {"market": market, "exit": exit_cc, "source": getattr(exit, "source", None),
               "locale": getattr(driver, "_locale", None), "variant": variant or "",
               "overrides": sorted(getattr(driver, "_overrides", None) or ()),
               "mismatch": bool(market and market != (exit_cc or "")), "at": time.time()}
        LOCALE_CHECKS[self.name] = rec
        if rec["mismatch"]:
            print(f"locale-check: site={self.name} variant={variant or '-'} market={market} "
                  f"exit={exit_cc or 'none'}({rec['source'] or '-'}) locale={rec['locale'] or '-'} MISMATCH")

    # ---- driver construction (shared) ------------------------------------------

    def launch(self, proxy: Optional[str], headless: Optional[bool] = None):
        # Both drivers record a browser's processes by pid-diff around the
        # spawn, so spawns are serialized by launch_lock.LAUNCH_LOCK.
        # PatchrightDriver takes it itself around its spawn window only, so
        # its exit geo lookup and native probe overlap other launches (until
        # 2026-11-04 the whole constructor was under the lock here and a cold
        # burst launched one at a time); CamoufoxDriver does its geoip inside
        # the constructor, so that one is still taken here.
        headless = self.headless if headless is None else headless
        if self.engine == "camoufox":
            with _LAUNCH_LOCK:
                return self._launch_camoufox(proxy, headless)
        if self.engine != "patchright":
            raise RecipeError(f"{self.name}: unknown engine {self.engine!r}")
        from patchright_driver import PatchrightDriver
        return PatchrightDriver(
            proxy_url=proxy, headless=headless,
            block_resources=self.block_resources, block_hosts=self.block_hosts,
            direct_hosts=self.direct_hosts, browserforge=self.browserforge,
            timezone=self.timezone, extra_args=self.chrome_args,
            geoip_timezone=bool(proxy) and self.geoip_timezone,
            locale=self.locale, channel=self.channel)

    def _launch_camoufox(self, proxy: Optional[str], headless: bool):
        """A CamoufoxDriver (Firefox) for the recipe: geoip (timezone /
        locale / geolocation from the exit) is always on in that driver;
        the recipe's extensions, locale, image pref and screen constraint
        are passed through. Imported here, not at module top: the Ubuntu
        box has no camoufox stack and must still load every recipe."""
        try:
            from camoufox_driver import CamoufoxDriver
        except ImportError as e:
            raise RecipeError(f"{self.name}: the camoufox engine is not installed on this box ({e})")
        screen = None
        if self.screen:
            from browserforge.fingerprints import Screen
            min_w, max_w, min_h, max_h = self.screen
            screen = Screen(min_width=min_w, max_width=max_w, min_height=min_h, max_height=max_h)
        return CamoufoxDriver(
            proxy_url=proxy, headless=headless, locale=self.locale,
            block_images=self.block_images, addons=list(self.extensions), screen=screen,
            block_resources=self.block_resources, block_hosts=self.block_hosts)

    def seed_url(self, variant: str) -> str:
        try:
            url = self.variants[variant or ""]
        except KeyError:
            raise RecipeError(f"{self.name}: unknown variant {variant!r}; known: {sorted(self.variants)}")
        if not url:
            raise RecipeError(f"{self.name}: no seed page for variant {variant!r}")
        return url

    def park_page(self, driver, url: str) -> None:
        driver.call(lambda page: page.goto(url, wait_until="domcontentloaded", timeout=30_000))

    @staticmethod
    def identity(driver, origin: str) -> dict:
        """cookies + UA + client hints + Chrome major, read from the live
        page, plus the language and clock the browser announced
        (`languages` = navigator.languages, `accept_language` = the header
        string of its exit profile, `timezone`, `exit_country`), so a curl
        replay on GKE sends the same headers (browser_client.headers_for)."""
        cookies = driver.call(lambda page: page.context.cookies(origin))
        ua = driver.evaluate("navigator.userAgent", main_world=True)
        hints = driver.evaluate(
            "navigator.userAgentData ? navigator.userAgentData.toJSON() : null", main_world=True) or {}
        major = None
        for b in (hints or {}).get("brands", []):
            if b.get("brand") in ("Google Chrome", "Chromium"):
                try:
                    major = int(b.get("version"))
                except (TypeError, ValueError):
                    pass
        try:
            langs = driver.evaluate("Array.from(navigator.languages)", main_world=True)
        except Exception:
            langs = None
        exit = getattr(driver, "_exit", None)
        return {"cookies": [{k: c[k] for k in ("name", "value", "domain", "path") if k in c}
                            for c in cookies],
                "user_agent": ua, "client_hints": hints, "chrome_major": major,
                "languages": langs if isinstance(langs, list) else None,
                "accept_language": getattr(driver, "_accept_language", None) or getattr(exit, "accept_language", None),
                "timezone": getattr(driver, "_timezone", None),
                "exit_country": getattr(exit, "country", None)}


# ---- cookie_mint ------------------------------------------------------------------

class CookieMint(Recipe):
    """Open the seed page through `proxy`, wait until `passed(cookies, driver)`
    is true, return the identity, close. Never kept."""
    actions = ("mint",)
    wait_for_cookie: Optional[str] = None     # the cookie that means "passed"
    timeout: int = settings.MINT_TIMEOUT
    poll: float = settings.MINT_POLL
    referer: Optional[str] = "https://www.google.com/"
    challenge_markers: tuple = ()
    ttl_seconds: int = 3 * 24 * 3600          # how long GKE may cache the identity

    # A challenge cookie only exists on an exit the WAF actually challenges.
    # When the seed page comes back with none of the challenge markers, there
    # is nothing to mint on this exit: fail fast ("unchallenged") instead of
    # polling for the timeout. GKE then mints on the exit whose curl session
    # was challenged (the same sticky port), where the token will appear.
    require_challenge: bool = True
    unchallenged_grace: float = 5.0

    # nav_settle False: load the seed only to domcontentloaded and start
    # polling for the cookie at once, instead of nav()'s settle (networkidle
    # up to 2 s + a content-stability poll). For a challenge page whose token
    # appears ~1 s after load (similarweb, 2026-10-24: 2.5 s saved per mint).
    nav_settle: bool = True

    def challenged(self, driver) -> bool:
        if not self.challenge_markers:
            return True
        try:
            frames = driver.content_frames()
            html = (frames[0] if frames else "") or ""
        except Exception:
            return True
        low = html[:20000].lower()
        return any(m.lower() in low for m in self.challenge_markers)

    def passed(self, cookies: dict, driver) -> bool:
        return bool(self.wait_for_cookie) and self.wait_for_cookie in cookies

    def tokens(self, cookies: dict, driver) -> dict:
        """Named tokens to return beside the cookies (default: the wait cookie)."""
        return {self.wait_for_cookie: cookies[self.wait_for_cookie]} if self.wait_for_cookie else {}

    def mint(self, proxy: Optional[str], variant: str = "") -> dict:
        seed = self.seed_url(variant)
        origin = _origin(seed)
        driver = self.launch(proxy)
        self._locale_check(driver, variant, seed)
        try:
            if self.nav_settle:
                driver.nav(seed, referer=self.referer, mode="blocked",
                           challenge_markers=self.challenge_markers)
            else:
                driver.call(lambda page: page.goto(seed, referer=self.referer, wait_until="domcontentloaded",
                                                   timeout=self.timeout * 1000))
            deadline = time.monotonic() + self.timeout
            unchallenged_deadline = time.monotonic() + self.unchallenged_grace
            while True:
                raw = driver.call(lambda page: page.context.cookies(origin))
                cookies = {c["name"]: c["value"] for c in raw}
                if (self.require_challenge and time.monotonic() >= unchallenged_deadline
                        and not self.passed(cookies, driver) and not self.challenged(driver)):
                    raise RecipeError(f"{self.name}: unchallenged on this exit, no token to mint "
                                      f"(cookies: {sorted(cookies)})")
                if self.passed(cookies, driver):
                    ident = self.identity(driver, origin)
                    ident["tokens"] = self.tokens(cookies, driver)
                    ident["exit_ip"] = _exit_ip(proxy)
                    ident["expires_at"] = time.time() + self.ttl_seconds
                    ident["proxy_port"] = _port(proxy)
                    return ident
                if time.monotonic() >= deadline:
                    raise RecipeError(f"{self.name}: challenge not cleared in {self.timeout}s "
                                      f"(cookies: {sorted(cookies)})")
                time.sleep(self.poll)
        finally:
            driver.close()


# ---- kept-browser base --------------------------------------------------------------

class _Kept(Recipe):
    """token_mint / page_fetch / session_fetch: a parked browser reused across
    requests, keyed by proxy session (or caller session id)."""

    def key_for(self, proxy: Optional[str], session: Optional[str]) -> str:
        return session or (f"p{_port(proxy)}" if proxy else "direct")

    def warm(self, driver, variant: str) -> None:
        """Runs once per launch: load the seed page and settle the challenge."""
        seed = self.seed_url(variant)
        driver.nav(seed, referer="https://www.google.com/", mode="blocked",
                   challenge_markers=getattr(self, "challenge_markers", ()))

    def _lease(self, proxy, variant, session=None, url=None) -> Lease:
        """The browser for this request: the one keyed by (proxy, session),
        or — when the caller sent candidates — whichever of them the box
        picks (REGISTRY.acquire_any). The single proxy/session a recipe
        derives is ignored then: each candidate carries its own."""
        self.seed_url(variant)      # unknown variant -> RecipeError before any launch
        candidates = request_candidates()
        if not candidates:
            lease = REGISTRY.acquire(self, self.key_for(proxy, session), proxy,
                                     launch=lambda: self.launch(proxy),
                                     warm=lambda d: self.warm(d, variant))
            self._locale_check(lease.driver, variant, url)
            return lease

        def launcher(i, cand_proxy):
            def launch():
                REQUEST.chosen = i
                return self.launch(cand_proxy)
            return launch
        choices = []
        for i, cand in enumerate(candidates):
            cand_proxy = cand.get("proxy") or None
            choices.append((self.key_for(cand_proxy, cand.get("session") or None), cand_proxy,
                            launcher(i, cand_proxy)))
        lease, i = REGISTRY.acquire_any(self, choices, warm=lambda d: self.warm(d, variant))
        REQUEST.chosen = i
        self._locale_check(lease.driver, variant, url)
        return lease


class TokenMint(_Kept):
    """Per request: run the site's token call on the parked page."""
    actions = ("token",)
    ttl_seconds: int = 300

    def mint_token(self, driver, payload) -> str:
        raise NotImplementedError

    def token(self, proxy: Optional[str], variant: str = "", input=None) -> dict:
        lease = self._lease(proxy, variant)
        failed = False
        try:
            tok = self.mint_token(lease.driver, input)
            if not tok:
                raise RecipeError(f"{self.name}: empty token")
            return {"token": tok, "expires_at": time.time() + self.ttl_seconds}
        except Exception:
            failed = True
            raise
        finally:
            REGISTRY.release(lease, self, failed=failed)


class PageFetch(_Kept):
    """Per request: fetch the caller's URL on the parked page. `mode`
    "navigate" loads it as a page and returns the DOM; "in_page" runs a
    same-origin fetch() inside the page and returns the raw body."""
    actions = ("fetch",)
    mode: str = "in_page"
    nav_mode: str = "blocked"

    def fetch_one(self, driver, url: str, method: str, headers: dict, body) -> dict:
        if self.mode == "navigate":
            res = driver.nav(url, referer=headers.get("referer") if headers else None,
                             mode=self.nav_mode, challenge_markers=getattr(self, "challenge_markers", ())) or {}
            return {"status": res.get("status") or 200, "headers": {},
                    "body": res.get("html") or driver.content(), "final_url": res.get("url") or url}
        r = driver.get(url, headers=headers or None) if (method or "GET").upper() == "GET" \
            else driver.request(method, url, headers=headers or None, body=body)
        return {"status": r.status_code, "headers": dict(r.headers or {}), "body": r.text,
                "final_url": r.url or url}

    def spent(self, driver) -> bool:
        """True when the browser must not serve another call although this
        one answered (the recipe got its answer from a page that burned the
        session). Default: never."""
        return False

    def fetch(self, proxy, variant="", session=None, url=None, urls=None,
              method="GET", headers=None, body=None) -> dict | list:
        lease = self._lease(proxy, variant, session, url=url or (urls[0] if urls else None))
        failed = False
        try:
            if urls:
                out = [self.fetch_one(lease.driver, u, method, headers, body) for u in urls]
            else:
                out = self.fetch_one(lease.driver, url, method, headers, body)
            failed = self.spent(lease.driver)
            return out
        except Exception:
            failed = True
            raise
        finally:
            REGISTRY.release(lease, self, failed=failed)


class SessionFetch(PageFetch):
    """page_fetch pinned to a caller session id; batches run as one get_many."""
    actions = ("fetch",)
    max_browsers: int = 4

    def fetch(self, proxy, variant="", session=None, url=None, urls=None,
              method="GET", headers=None, body=None):
        if urls and (method or "GET").upper() == "GET" and self.mode != "navigate":
            lease = self._lease(proxy, variant, session, url=urls[0])
            failed = False
            try:
                rs = lease.driver.get_many(urls, headers=headers or None)
                return [{"status": r.status_code, "headers": dict(r.headers or {}),
                         "body": r.text, "final_url": r.url or u} for u, r in zip(urls, rs)]
            except Exception:
                failed = True
                raise
            finally:
                REGISTRY.release(lease, self, failed=failed)
        return super().fetch(proxy, variant, session, url, urls, method, headers, body)


# ---- helpers ----------------------------------------------------------------------

def _origin(url: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _port(proxy: Optional[str]) -> Optional[int]:
    if not proxy:
        return None
    from urllib.parse import urlsplit
    return urlsplit(proxy).port


def _exit_ip(proxy: Optional[str]) -> Optional[str]:
    try:
        import requests
        kw = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
        return requests.get("https://api.ipify.org", timeout=8, **kw).text.strip()
    except Exception:
        return None
