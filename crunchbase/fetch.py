"""Crunchbase transport (validated 2026-09-21 from direct Indian egress and
residential US proxy exits).

www.crunchbase.com is an Angular app whose every page is server-rendered
from ONE internal JSON API, and that API is what this module talks to:

  GET  /v4/data/autocompletes?query=&collection_ids=&limit=
  GET  /v4/data/entities/<collection>/<slug|uuid>?field_ids=[..]&card_ids=[..]
       &layout_id=<entity>&layout_mode=view_v3      (all cards of the layout)
  GET  /v4/data/entities/<collection>/<id>/cards/<card>?card_id=<card>&after_id=<uuid>
       (10 rows per call, cursor on the last row's uuid)
  POST /v4/data/searches/<collection_id>?source=collection_advanced_search
       body {field_ids, order, query:[predicates], field_aggregators, collection_id, limit}

Cloudflare fronts everything:
  * every path answers a managed challenge (403 + cf-mitigated: challenge)
    to plain curl on every egress; a HEADED patchright Chrome clears it in
    2-8 s on direct egress (no click) and after a Turnstile click or two on
    residential exits; headless never clears ("Attention Required").
  * the browser's cf_clearance + __cf_bm and its user-agent replay over
    curl_cffi from the same exit, for GET pages, GET /v4/data JSON and the
    POST searches — the POSTs only with the FULL Chrome header set (sec-ch-ua,
    sec-fetch-*, priority, origin, x-requested-with, x-cb-client-app-instance-id),
    otherwise a WAF rule returns a plain 403 block page.
  * ORGANIZATION quota: /organization/<slug> pages and everything under
    /v4/data/entities/organizations/ (cards included) are rate-limited PER
    COOKIE — the 12th request answers 429 + a challenge page and that cookie
    stays blocked for many minutes, while a freshly minted cookie on the same
    IP gets its own 11. Cookies minted back to back are independent, so they
    are pooled: an identity is retired from organization work after
    config.CRUNCHBASE_ORG_QUOTA uses and a spare is minted in the background.
    Other entity types and the searches are not under that rule.
  * GENERAL limit: ~150-200 requests per ~10 minutes per IP, then a JSON 429
    "error 1015" for a minute or two. On it the egress cools for
    config.CRUNCHBASE_RATE_LIMIT_COOLDOWN and work moves to an identity on
    another exit (a fresh residential exit is minted when none is ready).
  * a cookie dies in under two hours; identities are retired at
    config.CRUNCHBASE_IDENTITY_TTL.

Anonymous entitlement (what a logged-out visitor may read): searches return
at most 15 rows and refuse after_id paging and aggregations; the values
Crunchbase sells (founded / announced dates, money amounts, valuations,
article URLs, employee-count-adjacent counters) come back as
`preview_properties` placeholders instead of values, in entity cards AND
search rows — the parsers return null for those. Everything else (names,
permalinks, categories, locations, ranks, growth / heat scores, round
types, investor / people / acquisition relationships, signals, tech stack,
predictions, press titles) is in the clear.

Identities are minted by ONE shared headed Chrome (launched on demand,
closed after config.CRUNCHBASE_BROWSER_IDLE seconds idle): clear the
browser's cookies, load the seed page (the discover page — never an
organization page, which would spend quota), wait for the real title, read
the cookie jar. A mint whose curl replay is still challenged is served by
in-page fetch() from the tab instead (some residential exits issue no
cf_clearance at all, like Envato).

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  CrunchbaseUpstreamError  transport failure / 5xx                — retried
  CrunchbaseBlocked        challenge / rate limit that survived rotation
  CrunchbaseBadRequest     upstream rejected the params (MD101/MD403/CS109)
  CrunchbaseNotFound       CS404 / unknown entity                  — never retried
"""
import json
import os
import sys
import threading
import time
import uuid as uuidlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError

SITE = "https://www.crunchbase.com"
NEWS = "https://news.crunchbase.com"
IMPERSONATE = "chrome"
TIMEOUT = 40
FANOUT_WORKERS = 6
SEED_PATH = "/discover/organization.companies"
SEARCH_LIMIT = 15          # anonymous maximum rows per search call
CARD_PAGE = 10             # rows per /cards/<card> call

CF_MARKERS = ("just a moment", "one moment, please", "cf-chl", "challenge-platform",
              "attention required! | cloudflare", "access denied | www.crunchbase.com")
HARVEST_POLL = 0.5
HARVEST_SETTLE = 1.0
CLICK_EVERY = 4
MAX_CLICKS = 4

# The Chrome fingerprint the challenge was solved with; sec-ch-ua must match
# the user-agent's major version (patchright real Chrome, currently 140).
_CH_UA = '"Chromium";v="{v}", "Not=A?Brand";v="24", "Google Chrome";v="{v}"'


def _ua_major(ua):
    import re
    m = re.search(r"Chrome/(\d+)", ua or "")
    return m.group(1) if m else "140"


def _headers(ua, kind, referer=SEED_PATH):
    """The header set the SPA sends (kind = json | post | html)."""
    base = {
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": _CH_UA.format(v=_ua_major(ua)),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"' if "Macintosh" in (ua or "") else '"Linux"',
        "user-agent": ua,
    }
    if kind == "html":
        base.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-dest": "document", "sec-fetch-mode": "navigate", "sec-fetch-site": "none",
            "upgrade-insecure-requests": "1",
        })
        return base
    base.update({
        "accept": "application/json, text/plain, */*",
        "priority": "u=1, i",
        "referer": SITE + referer,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest",
        "x-cb-client-app-instance-id": str(uuidlib.uuid4()),
    })
    if kind == "post":
        base.update({"content-type": "application/json", "origin": SITE})
    return base


class CrunchbaseUpstreamError(UpstreamError):
    """Transport failure or 5xx — retryable."""


class CrunchbaseBlocked(CrunchbaseUpstreamError, Blocked):
    """Cloudflare challenge / rate limit that rotation could not get past."""


class CrunchbaseBadRequest(BadRequest):
    """Upstream rejected the params — never retried."""


class CrunchbaseNotFound(NotFound):
    """Unknown entity / card — never retried."""


def dump_debug(name, text):
    """Write a raw response to $CRUNCHBASE_DEBUG_DIR/<name>.txt."""
    dbg = os.environ.get("CRUNCHBASE_DEBUG_DIR", "")
    if dbg and text:
        try:
            os.makedirs(dbg, exist_ok=True)
            with open(os.path.join(dbg, name + ".txt"), "w") as f:
                f.write(text)
        except OSError:
            pass


# ---- response classification -----------------------------------------------------

def _is_json(resp):
    return "json" in (resp.headers.get("content-type") or "").lower()


def is_rate_limited(resp):
    """Cloudflare error 1015 (per-IP request budget): JSON 429."""
    if resp.status_code != 429:
        return False
    text = (resp.text or "")[:600]
    return "1015" in text or "rate limited" in text.lower() or "rate-limited" in text.lower()


def is_challenged(resp):
    """A Cloudflare interstitial / block page rather than a real answer
    (the organization quota answers 429 + challenge; a dead cookie 403 +
    challenge; a WAF rule a plain 403 block page)."""
    if is_rate_limited(resp):
        return False
    if any(k.lower() == "cf-mitigated" for k in resp.headers.keys()):
        return True
    if resp.status_code not in (403, 429, 503):
        return False
    low = (resp.text or "")[:6000].lower()
    return any(m in low for m in CF_MARKERS) or "sorry, you have been blocked" in low


def is_org_path(url):
    return "/v4/data/entities/organizations/" in url or url.startswith(SITE + "/organization/")


# ---- curl sessions -----------------------------------------------------------------

def _new_session(proxy):
    from curl_cffi import requests as curl_requests
    sess = curl_requests.Session(impersonate=IMPERSONATE)
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    return sess


def _close(sess):
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


_local = threading.local()


def _session_for(ident):
    """Per-thread curl session bound to one identity (its exit)."""
    sessions = getattr(_local, "sessions", None)
    if sessions is None:
        sessions = _local.sessions = {}
    entry = sessions.get(ident.key)
    if entry is None:
        for other in list(sessions):
            if sessions[other][0].retired:
                _close(sessions.pop(other)[1])
        entry = sessions[ident.key] = (ident, _new_session(ident.proxy))
    return entry[1]


def _drop_session(ident):
    sessions = getattr(_local, "sessions", None) or {}
    entry = sessions.pop(ident.key, None)
    if entry is not None:
        _close(entry[1])


# ---- identities ----------------------------------------------------------------------

class Identity:
    """One Cloudflare clearance: cookies + user-agent bound to one exit IP."""
    __slots__ = ("key", "cookies", "ua", "proxy", "mode", "born", "org_uses", "retired", "lock", "generation")

    def __init__(self, cookies, ua, proxy, mode, generation):
        self.key = uuidlib.uuid4().hex
        self.cookies, self.ua, self.proxy, self.mode = cookies, ua, proxy, mode
        self.born = time.time()
        self.org_uses = 0
        self.retired = False
        self.lock = threading.Lock()
        self.generation = generation

    @property
    def egress(self):
        return self.proxy or "direct"

    def expired(self):
        return time.time() - self.born > config.CRUNCHBASE_IDENTITY_TTL

    def org_left(self):
        return config.CRUNCHBASE_ORG_QUOTA - self.org_uses

    def take_org(self):
        """Reserve one organization request; False when the quota is spent."""
        with self.lock:
            if self.org_uses >= config.CRUNCHBASE_ORG_QUOTA:
                return False
            self.org_uses += 1
            return True


class _Resp:
    """curl-response lookalike for an in-page fetch result."""

    def __init__(self, status_code, text, headers, url):
        self.status_code, self.text, self.headers, self.url = status_code, text, headers or {}, url

    def json(self):
        return json.loads(self.text)


def _click_turnstile(page):
    """Real mouse click on the Turnstile checkbox: the widget iframe's
    bounding box, x at 1/9 of the width (the checkbox), y at the middle.
    Returns True when a challenge frame was found and clicked."""
    import random
    for frame in page.frames:
        if not frame.url.startswith("https://challenges.cloudflare.com"):
            continue
        try:
            box = frame.frame_element().bounding_box()
        except Exception:
            box = None
        if not box or box.get("width", 0) < 50:
            continue
        x = box["x"] + box["width"] / 9 + random.uniform(-5, 5)
        y = box["y"] + box["height"] / 2 + random.uniform(-5, 5)
        page.mouse.move(x, y, steps=random.randint(10, 25))
        page.wait_for_timeout(random.randint(100, 300))
        page.mouse.down()
        page.wait_for_timeout(random.randint(50, 150))
        page.mouse.up()
        return True
    return False


class _Browser:
    """The shared headed Chrome that mints identities (and serves in-page
    fetches for identities whose cookie does not replay over curl). Every
    playwright call runs on the driver's owner thread through driver.call()."""

    def __init__(self):
        self.lock = threading.RLock()
        self.driver = None
        self.proxy = None
        self.generation = 0
        self.last_used = 0.0
        self.last_clearance = None
        self._reaper = None

    def _launch(self, proxy):
        from chrome_manager import create_scope
        from patchright_driver import PatchrightDriver
        self._close_locked()
        print(f"crunchbase: launching the identity browser "
              f"(egress={'direct' if not proxy else proxy.rsplit('@', 1)[-1]})")
        with create_scope():
            self.driver = PatchrightDriver(proxy_url=proxy, headless=False)
        self.proxy = proxy
        self.generation += 1
        self.last_clearance = None
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap, name="crunchbase-browser-reaper", daemon=True)
            self._reaper.start()

    def _close_locked(self):
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass
        _retire_browser_identities()

    def close(self):
        with self.lock:
            self._close_locked()

    def _reap(self):
        while True:
            time.sleep(30)
            with self.lock:
                if self.driver is not None and time.time() - self.last_used > config.CRUNCHBASE_BROWSER_IDLE:
                    print("crunchbase: closing the idle identity browser")
                    self._close_locked()

    def mint(self, proxy):
        """Clear cookies, load the seed page until the real title shows
        (clicking Turnstile when it appears) -> (cookies, ua, generation).
        Relaunches when the exit differs or the clearance did not change."""
        with self.lock:
            if self.driver is None or self.proxy != proxy:
                self._launch(proxy)
            self.last_used = time.time()
            driver, generation = self.driver, self.generation

            def clear(page):
                page.context.clear_cookies()

            def goto(page):
                page.goto(SITE + SEED_PATH, referer="https://www.google.com/",
                          wait_until="domcontentloaded", timeout=60000)

            def state(page):
                try:
                    return page.title() or ""
                except Exception:
                    return ""

            started = time.monotonic()
            driver.call(clear)
            driver.call(goto)
            clicks, last_click = 0, None
            while True:
                low = driver.call(state).lower()
                if low and not any(m in low for m in CF_MARKERS):
                    time.sleep(HARVEST_SETTLE)
                    break
                if "attention required" in low or "access denied" in low:
                    raise RuntimeError(f"Cloudflare refuses this exit: {low[:60]}")
                elapsed = time.monotonic() - started
                if elapsed > config.CRUNCHBASE_HARVEST_TIMEOUT:
                    raise RuntimeError(f"Cloudflare challenge did not clear (clicks={clicks})")
                due = (last_click is None and elapsed >= 3) or \
                      (last_click is not None and time.monotonic() - last_click >= CLICK_EVERY)
                if due and clicks < MAX_CLICKS and driver.call(_click_turnstile):
                    clicks += 1
                    last_click = time.monotonic()
                time.sleep(HARVEST_POLL)
            ua = driver.evaluate("navigator.userAgent")
            cookies = {c["name"]: c["value"] for c in driver.call(lambda page: page.context.cookies(SITE))}
            clearance = cookies.get("cf_clearance")
            if clearance and clearance == self.last_clearance:
                # the context kept its clearance: only a relaunch yields a new one
                self._launch(proxy)
                return self.mint(proxy)
            self.last_clearance = clearance
            print(f"crunchbase: identity minted in {time.monotonic() - started:.0f}s "
                  f"(clicks={clicks}, cf_clearance={'yes' if clearance else 'no'}, "
                  f"egress={'direct' if not proxy else proxy.rsplit('@', 1)[-1]})")
            return cookies, ua, generation

    def fetch(self, method, url, headers, body, generation):
        """In-page fetch() from the tab -> _Resp; raises _StaleBrowser when
        the browser that minted the identity is gone."""
        from patchright_driver import _FETCH_JS, FETCH_TIMEOUT_MS
        with self.lock:
            if self.driver is None or self.generation != generation:
                raise _StaleBrowser()
            driver = self.driver
            self.last_used = time.time()
            res = driver.call(lambda page: page.evaluate(_FETCH_JS, {
                "url": url, "method": method, "headers": headers, "body": body,
                "referrer": SITE + SEED_PATH, "timeoutMs": FETCH_TIMEOUT_MS}))
        if not res or not res.get("ok"):
            raise CrunchbaseUpstreamError(f"in-page fetch failed: {(res or {}).get('error')}")
        return _Resp(int(res.get("status") or 0), res.get("body") or "", res.get("headers"), res.get("url") or url)


class _StaleBrowser(Exception):
    pass


_browser = _Browser()
_pool_lock = threading.Lock()
_identities = deque()                # live identities, oldest first
_mint_lock = threading.Lock()        # one mint at a time
_egress_cooling = {}                 # egress -> epoch when usable again
_failed_until = 0.0                  # every mint attempt failed recently
_spare_thread = {"t": None}


def _retire_browser_identities():
    with _pool_lock:
        for ident in _identities:
            if ident.mode == "browser":
                ident.retired = True


def egress_cooling(egress):
    return time.time() < _egress_cooling.get(egress, 0)


def _cool_egress(egress, seconds):
    _egress_cooling[egress] = max(_egress_cooling.get(egress, 0), time.time() + seconds)
    print(f"crunchbase: egress {egress if egress == 'direct' else egress.rsplit('@', 1)[-1]} "
          f"cooling for {seconds}s")


def _candidate_exits():
    """Egresses to mint on, in order: direct (unless cooling / disabled),
    then fresh residential exits."""
    exits = []
    if not config.CRUNCHBASE_PROXY_COUNTRY and not egress_cooling("direct"):
        exits.append(None)
    country = config.CRUNCHBASE_PROXY_COUNTRY or config.CRUNCHBASE_FALLBACK_PROXY_COUNTRY
    if country:
        exits += [config.crunchbase_proxy(country) for _ in range(config.CRUNCHBASE_HARVEST_ATTEMPTS)]
    if not exits:
        exits.append(None)
    return exits


def _curl_validates(cookies, ua, proxy):
    sess = _new_session(proxy)
    try:
        resp = sess.get(f"{SITE}/v4/data/autocompletes?query=stripe&collection_ids=organizations&limit=1",
                        headers=_headers(ua, "json"), cookies=cookies, timeout=TIMEOUT)
    except Exception as e:
        print(f"crunchbase: curl validation failed: {type(e).__name__}: {e}")
        return False
    finally:
        _close(sess)
    return resp.status_code == 200 and _is_json(resp)


def _mint():
    """Mint one identity on the first exit that works."""
    global _failed_until
    last = None
    for proxy in _candidate_exits():
        try:
            cookies, ua, generation = _browser.mint(proxy)
        except Exception as e:
            last = e
            print(f"crunchbase: mint on {'direct' if not proxy else proxy.rsplit('@', 1)[-1]} failed: "
                  f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")
            if proxy is None:
                _cool_egress("direct", config.CRUNCHBASE_RATE_LIMIT_COOLDOWN)
            continue
        mode = "curl" if _curl_validates(cookies, ua, proxy) else "browser"
        if mode == "browser":
            print("crunchbase: the minted cookie does not replay over curl; serving it in-page")
        ident = Identity(cookies, ua, proxy, mode, generation)
        with _pool_lock:
            _identities.append(ident)
        return ident
    _failed_until = time.time() + config.CRUNCHBASE_HARVEST_COOLDOWN
    raise CrunchbaseBlocked(f"Cloudflare would not clear on any exit ({last})")


def _prune():
    with _pool_lock:
        for ident in list(_identities):
            if ident.retired or ident.expired():
                ident.retired = True
                _identities.remove(ident)


def _pick(need_org):
    """A live identity on a non-cooling egress with organization quota left
    (when need_org), preferring the one with the most quota so cookies are
    spent evenly and the freshest survives longest."""
    with _pool_lock:
        best = None
        for ident in _identities:
            if ident.retired or ident.expired() or egress_cooling(ident.egress):
                continue
            if need_org and ident.org_left() <= 0:
                continue
            if best is None or (need_org and ident.org_left() > best.org_left()):
                best = ident
        return best


def _ensure_spare():
    """Mint a fresh identity in the background when every live one has
    little organization quota left, so the next profile does not wait."""
    with _pool_lock:
        left = sum(i.org_left() for i in _identities if not i.retired and not i.expired())
        alive = _spare_thread["t"] is not None and _spare_thread["t"].is_alive()
    if left > config.CRUNCHBASE_ORG_QUOTA // 2 or alive or time.time() < _failed_until:
        return

    def run():
        with _mint_lock:
            if _pick(True) is not None and sum(i.org_left() for i in _identities if not i.retired) > config.CRUNCHBASE_ORG_QUOTA // 2:
                return
            try:
                _mint()
            except Exception as e:
                print(f"crunchbase: background mint failed: {e}")
    t = threading.Thread(target=run, name="crunchbase-spare-mint", daemon=True)
    _spare_thread["t"] = t
    t.start()


def acquire(need_org):
    """An identity ready for one request (mints when none qualifies)."""
    _prune()
    ident = _pick(need_org)
    if ident is not None:
        return ident
    if time.time() < _failed_until:
        raise CrunchbaseBlocked("Cloudflare would not clear on any exit recently; retrying after the cooldown")
    with _mint_lock:
        ident = _pick(need_org)
        if ident is not None:
            return ident
        return _mint()


def retire(ident, reason):
    if not ident.retired:
        ident.retired = True
        print(f"crunchbase: identity retired ({reason}; org uses {ident.org_uses}, "
              f"age {time.time() - ident.born:.0f}s)")
    _prune()


# ---- one request -----------------------------------------------------------------------

def _once(method, url, body, kind):
    """One request on a suitable identity -> response. Handles the two
    Cloudflare verdicts by rotating: a challenge retires the identity (a
    spent organization quota or a dead cookie); error 1015 cools the egress."""
    need_org = is_org_path(url)
    for attempt in range(4):
        ident = acquire(need_org)
        if need_org and not ident.take_org():
            continue
        headers = _headers(ident.ua, kind)
        try:
            if ident.mode == "browser":
                resp = _browser.fetch(method, url, {k: v for k, v in headers.items()
                                                    if k in ("accept", "content-type", "x-requested-with",
                                                             "x-cb-client-app-instance-id")},
                                      body, ident.generation)
            else:
                sess = _session_for(ident)
                if method == "POST":
                    resp = sess.post(url, headers=headers, cookies=ident.cookies, data=body, timeout=TIMEOUT)
                else:
                    resp = sess.get(url, headers=headers, cookies=ident.cookies, timeout=TIMEOUT)
        except _StaleBrowser:
            retire(ident, "browser gone")
            continue
        except CrunchbaseUpstreamError:
            raise
        except Exception as e:
            _drop_session(ident)
            raise CrunchbaseUpstreamError(f"request failed: {type(e).__name__}: {e}")
        if need_org and ident.org_left() <= 1:
            _ensure_spare()
        if is_rate_limited(resp):
            _cool_egress(ident.egress, config.CRUNCHBASE_RATE_LIMIT_COOLDOWN)
            continue
        if is_challenged(resp):
            dump_debug("challenged", resp.text)
            retire(ident, "quota spent" if need_org else "cookie challenged")
            _drop_session(ident)
            continue
        return resp
    raise CrunchbaseBlocked("Crunchbase kept challenging / rate-limiting every identity")


def _raise_for_api_error(resp, label):
    """The v4 API's error shapes: a list of {message, code, field_id, …}
    (MD101 invalid field, MD403 entitlement) or {error, code: CS404}."""
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("code") == "CS404":
        raise CrunchbaseNotFound(f"{label}: not found on Crunchbase")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        msgs = "; ".join(f"{e.get('message')} ({e.get('field_id') or e.get('card_id') or e.get('collection_id') or ''})"
                         for e in data[:4])
        raise CrunchbaseBadRequest(f"{label}: {msgs}")
    if isinstance(data, dict) and data.get("error"):
        raise CrunchbaseBadRequest(f"{label}: {data.get('error')}")
    raise CrunchbaseBadRequest(f"{label}: HTTP {resp.status_code}")


def request(method, url, *, body=None, kind="json", label=None, missing=None):
    """A v4 API call -> parsed JSON. 404 -> CrunchbaseNotFound; 400 ->
    CrunchbaseBadRequest; 5xx / transport -> retried then CrunchbaseUpstreamError."""
    label = label or url
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = _once(method, url, body, kind)
        except CrunchbaseBlocked:
            raise
        except CrunchbaseUpstreamError as e:
            last = e
        else:
            if resp.status_code == 404:
                raise CrunchbaseNotFound(missing or f"{label}: not found on Crunchbase")
            if resp.status_code in (400, 401, 403, 409, 414, 422):
                if resp.status_code == 414:
                    raise CrunchbaseBadRequest(f"{label}: request URL too long for Crunchbase")
                _raise_for_api_error(resp, label)
            if resp.status_code < 400:
                if kind == "html":
                    return resp.text
                try:
                    return resp.json()
                except Exception:
                    dump_debug("nonjson", resp.text)
                    raise CrunchbaseUpstreamError(f"{label} returned a non-JSON body")
            last = CrunchbaseUpstreamError(f"HTTP {resp.status_code} on {label}")
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF * attempt)
    raise last


# ---- the API surface -------------------------------------------------------------------

def _q(value):
    return quote(json.dumps(value, separators=(",", ":")), safe="")


def autocomplete(query, collection_ids, limit=10):
    url = (f"{SITE}/v4/data/autocompletes?query={quote(query)}&collection_ids={quote(collection_ids)}"
           f"&limit={int(limit)}&source=topSearch")
    return request("GET", url, label=f"autocomplete {query!r}")


def entity(collection, ident, field_ids, card_ids=None, layout=None, missing=None):
    """One entity with its fields (+ every card of `layout`, or just
    `card_ids`). Organizations spend one unit of the cookie quota."""
    url = f"{SITE}/v4/data/entities/{collection}/{quote(str(ident), safe='')}?field_ids={_q(list(field_ids))}"
    if card_ids:
        url += f"&card_ids={_q(list(card_ids))}"
    if layout:
        url += f"&layout_id={layout}&layout_mode=view_v3"
    return request("GET", url, label=f"{collection}/{ident}", missing=missing)


def card(collection, ident, card_id, after_id=None):
    """One page (10 rows) of a list card; `after_id` = last row's uuid."""
    url = f"{SITE}/v4/data/entities/{collection}/{quote(str(ident), safe='')}/cards/{card_id}?card_id={card_id}"
    if after_id:
        url += f"&after_id={after_id}"
    data = request("GET", url, label=f"{collection}/{ident} {card_id}")
    rows = ((data or {}).get("cards") or {}).get(card_id) or []
    if isinstance(rows, dict):          # timeline cards: {count, entities}
        rows = rows.get("entities") or []
    return rows if isinstance(rows, list) else []


def card_pages(collection, ident, card_id, page, first_rows=None):
    """Rows of list-card page `page` (1-based). Page 1 can come from an
    entity fetch already in hand (`first_rows`); deeper pages walk the
    cursor from there, one call per hop. The entity embeds image-list cards
    shorter than a cards-endpoint page (8 of a company's 82 employees), so
    only an EMPTY hop — or one shorter than a full cards page — ends the walk;
    a short embedded first page does not."""
    rows = first_rows if first_rows is not None else card(collection, ident, card_id)
    for hop in range(page - 1):
        if not rows or ((hop > 0 or first_rows is None) and len(rows) < CARD_PAGE):
            return []
        last = (rows[-1].get("identifier") or {}).get("uuid") or rows[-1].get("uuid")
        if not last:
            return []
        rows = card(collection, ident, card_id, after_id=last)
    return rows


def search(collection_id, field_ids, query=None, order=None, limit=SEARCH_LIMIT, after_id=None):
    """POST /v4/data/searches/<collection_id> -> {count, entities}."""
    body = {
        "field_ids": list(field_ids),
        "order": order or [],
        "query": query or [],
        "field_aggregators": [],
        "collection_id": collection_id,
        "limit": min(int(limit), SEARCH_LIMIT),
    }
    if after_id:
        body["after_id"] = after_id
    url = f"{SITE}/v4/data/searches/{collection_id}?source=collection_advanced_search"
    label = f"search {collection_id}"
    for _ in range(4):
        try:
            return request("POST", url, body=json.dumps(body), kind="post", label=label)
        except CrunchbaseBadRequest as e:
            # The entitlement drifts: a field that was selectable yesterday may
            # answer MD403 today. Drop the fields the error names and retry
            # once per offender rather than failing the whole listing.
            bad = _rejected_fields(e)
            if not bad or not any(f in body["field_ids"] for f in bad):
                raise
            print(f"crunchbase: {label} dropped fields {sorted(bad)}")
            body["field_ids"] = [f for f in body["field_ids"] if f not in bad]
            if not body["field_ids"]:
                raise
    raise CrunchbaseBadRequest(f"{label}: Crunchbase kept rejecting the field list")


def _rejected_fields(error):
    """Field ids named in a MD101 / MD403 select-permission error."""
    import re
    text = str(error)
    if "predicate" in text or "sort field" in text or "search field" in text or "paginate" in text:
        return set()
    return {a or b for a, b in re.findall(r"to (?:select|read) field ([a-z0-9_]+)|invalid field_id \(([a-z0-9_]+)\)", text)}


def predicate(field_id, operator_id, values):
    return {"type": "predicate", "field_id": field_id, "operator_id": operator_id,
            "values": values if isinstance(values, list) else [values]}


# ---- Crunchbase News (open WordPress JSON, no Cloudflare gate) -----------------------------

def news_json(path, params=None):
    from curl_cffi import requests as curl_requests
    url = NEWS + path
    if params:
        url += ("&" if "?" in url else "?") + "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v not in (None, ""))
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = curl_requests.get(url, impersonate=IMPERSONATE, timeout=TIMEOUT,
                                     headers={"accept": "application/json", "accept-language": "en-US,en;q=0.9"})
        except Exception as e:
            last = CrunchbaseUpstreamError(f"news request failed: {type(e).__name__}: {e}")
        else:
            if resp.status_code == 400:
                raise CrunchbaseBadRequest(f"Crunchbase News rejected {path}")
            if resp.status_code == 404:
                raise CrunchbaseNotFound(f"{path}: not found on Crunchbase News")
            if resp.status_code < 400:
                try:
                    return resp.json(), resp.headers
                except Exception:
                    raise CrunchbaseUpstreamError("Crunchbase News returned a non-JSON body")
            last = CrunchbaseUpstreamError(f"HTTP {resp.status_code} on news {path}")
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF * attempt)
    raise last


# ---- parallel helpers ------------------------------------------------------------------

def run_parallel(fns, workers=FANOUT_WORKERS):
    """Run zero-arg callables concurrently, results in order; exceptions propagate."""
    if not fns:
        return []
    if len(fns) == 1:
        return [fns[0]()]
    with ThreadPoolExecutor(max_workers=min(workers, len(fns))) as pool:
        return list(pool.map(lambda f: f(), fns))


def run_parallel_quiet(fns, workers=FANOUT_WORKERS):
    """Like run_parallel but a failed call yields None instead of raising."""
    def safe(fn):
        def call():
            try:
                return fn()
            except Exception as e:
                print(f"crunchbase: parallel call failed: {type(e).__name__}: {e}")
                return None
        return call
    return run_parallel([safe(f) for f in fns], workers)


def pool_status():
    """Diagnostics for the health route / tests."""
    with _pool_lock:
        return [{"egress": i.egress if i.egress == "direct" else i.egress.rsplit("@", 1)[-1],
                 "mode": i.mode, "org_uses": i.org_uses, "age_s": int(time.time() - i.born),
                 "retired": i.retired} for i in _identities]
