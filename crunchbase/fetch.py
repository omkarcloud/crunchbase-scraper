"""Crunchbase transport (validated 2026-09-21 from direct Indian egress and
residential US proxy exits).

www.crunchbase.com is an Angular app whose every page is server-rendered
from ONE internal JSON API, and that API is what this module talks to:

  GET  /v4/data/autocompletes?query=&collection_ids=&limit=
       (NOT called since 2026-11-12: challenged for every curl client; the
       typeahead is served by the searches instead, see autocomplete())
  GET  /v4/data/entities/<collection>/<slug|uuid>?field_ids=[..]&card_ids=[..]
       &layout_id=<entity>&layout_mode=view_v3      (all cards of the layout)
  GET  /v4/data/entities/<collection>/<id>/cards/<card>?card_id=<card>&after_id=<uuid>
       (10 rows per call, cursor on the last row's uuid)
  POST /v4/data/searches/<collection_id>?source=collection_advanced_search
       body {field_ids, order, query:[predicates], field_aggregators, collection_id, limit}

Cloudflare fronts everything:
  * every path answers a managed challenge (403 + cf-mitigated: challenge)
    to plain curl on every egress; a HEADED Chrome clears it in 2-8 s on
    direct egress (no click) and after a Turnstile click or two on
    residential exits; headless never clears ("Attention Required"). Since
    2026-10-22 that Chrome runs on the browser box (browser_client.mint ->
    recipes/crunchbase.py), through the same exit this pod replays on.
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
  * 2026-11-12: the rules differ per path now. The searches and the entity
    endpoint (cards included) answer curl on a clean exit even without a
    cookie; HTML pages and /v4/data/autocompletes answer a managed challenge
    to curl WITH a fresh cf_clearance (four curl_cffi Chrome profiles, the
    browser's own client hints). An identity is therefore validated on a
    search (_curl_validates), never on a path of the second kind.

Anonymous entitlement (what a logged-out visitor may read): searches return
at most 15 rows and refuse after_id paging and aggregations; the values
Crunchbase sells (founded / announced dates, money amounts, valuations,
article URLs, employee-count-adjacent counters) come back as
`preview_properties` placeholders instead of values, in entity cards AND
search rows — the parsers return null for those. Everything else (names,
permalinks, categories, locations, ranks, growth / heat scores, round
types, investor / people / acquisition relationships, signals, tech stack,
predictions, press titles) is in the clear.

Identities are minted on the browser box, one throwaway headed Chrome per
mint through the exit this pod picked (direct, then fresh residential
ports): load the seed page (the discover page — never an organization
page, which would spend quota), wait for the real title, read the cookie
jar. The pool, the organization quota, the TTL, the cooldowns and the curl
replay validation all stay here. A mint whose curl replay is still
challenged is served in BROWSER mode instead (some residential exits issue
no cf_clearance at all, like Envato): every call is an in-page fetch from
a kept browser on the box (browser_client.fetch_any over the identity's
burst sessions -> recipes/
crunchbase_fetch.py), one browser per identity on that identity's exit,
closed by the box after 300 s idle. No browser runs on GKE.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  CrunchbaseUpstreamError  transport failure / 5xx                — retried
  CrunchbaseBlocked        challenge / rate limit that survived rotation
  CrunchbaseBadRequest     upstream rejected the params (MD101/MD403/CS109)
  CrunchbaseNotFound       CS404 / unknown entity                  — never retried
"""
import json
import fastjson
import os
import sys
import threading
import time
import uuid as uuidlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_client
import config
import egress
from browser_client import BoxRefused, BoxUnavailable
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from scraper_utils import debug, fanout, sessions as sessions_utils

SITE = "https://www.crunchbase.com"
NEWS = "https://news.crunchbase.com"
IMPERSONATE = "chrome"
TIMEOUT = 40
FANOUT_WORKERS = 6
SEED_PATH = "/discover/organization.companies"
SEARCH_LIMIT = 15          # anonymous maximum rows per search call
CARD_PAGE = 10             # rows per /cards/<card> call
BOX_SITE = "crunchbase"            # recipes/crunchbase.py (the mint)
BOX_FETCH = "crunchbase-fetch"     # recipes/crunchbase_fetch.py (browser mode)

CF_MARKERS = ("just a moment", "one moment, please", "cf-chl", "challenge-platform",
              "attention required! | cloudflare", "access denied | www.crunchbase.com")

# The Chrome fingerprint the challenge was solved with; sec-ch-ua must match
# the user-agent's major version (the box's real Chrome).
_CH_UA = '"Chromium";v="{v}", "Not=A?Brand";v="24", "Google Chrome";v="{v}"'


def _ua_major(ua):
    import re
    m = re.search(r"Chrome/(\d+)", ua or "")
    return m.group(1) if m else "140"


def _headers(ua, kind, referer=SEED_PATH):
    """The header set the SPA sends (kind = json | post | html)."""
    base = {
        "accept-language": egress.site_accept_language("crunchbase"),
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
    debug.dump("CRUNCHBASE_DEBUG_DIR", name, text)


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
    return sessions_utils.curl_session(IMPERSONATE, proxy)


def _close(sess):
    sessions_utils.close_quietly(sess)


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
    """One Cloudflare clearance: cookies + user-agent bound to one exit IP.
    `key` is also the box session of a browser-mode identity's browser."""
    __slots__ = ("key", "cookies", "ua", "proxy", "mode", "born", "org_uses", "retired", "lock", "last_used")

    def __init__(self, cookies, ua, proxy, mode):
        self.key = uuidlib.uuid4().hex
        self.cookies, self.ua, self.proxy, self.mode = cookies, ua, proxy, mode
        self.born = time.time()
        self.org_uses = 0
        self.retired = False
        self.lock = threading.Lock()
        self.last_used = 0.0           # monotonic; general work goes to the least recently used identity

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
        return fastjson.loads(self.text)


def _egress(proxy):
    return "direct" if not proxy else proxy.rsplit("@", 1)[-1]


def _box_mint(proxy):
    """One mint on the browser box through `proxy` (recipes/crunchbase.py:
    a throwaway headed Chrome loads the discover page until the real title
    shows) -> (cookies, ua). RuntimeError when the box could not clear this
    exit (the caller moves to the next one); CrunchbaseUpstreamError when
    the box itself is down (not a block: no exit cooling, no cooldown)."""
    try:
        ident = browser_client.mint(BOX_SITE, proxy=proxy)
    except BoxRefused as e:
        raise RuntimeError(f"the box could not clear this exit: {e}")
    except BoxUnavailable as e:
        raise CrunchbaseUpstreamError(f"browser box unavailable: {e}")
    cookies = {c["name"]: c["value"] for c in ident.get("cookies") or []}
    ua = ident.get("user_agent")
    if not ua:
        raise RuntimeError("the box answered without a user-agent")
    print(f"crunchbase: identity minted on the box (cf_clearance="
          f"{'yes' if 'cf_clearance' in cookies else 'no'}, egress={_egress(proxy)})")
    return cookies, ua


def _box_candidates(ident):
    """The kept browsers the box may serve this identity's call from:
    INPAGE_BROWSER_SLOTS x BROWSER_BURST_FACTOR sessions "<identity key>#i"
    on the identity's exit (one browser per identity until 2026-11-04). The
    key in the session keeps a retired identity from ever reusing a browser
    whose cookie spent its organization quota; the box picks an idle one
    or opens another while it has headroom."""
    return [(ident.proxy, f"{ident.key}#{i}")
            for i in range(config.burst_slots(config.INPAGE_BROWSER_SLOTS))]


def _box_fetch(ident, method, url, headers, body):
    """In-page fetch from one of the identity's kept browsers on the box
    (recipes/crunchbase_fetch.py, _box_candidates) -> _Resp. A BoxRefused
    propagates: _once reads its verdict (rate-limited / challenged) the way
    it reads a curl reply's."""
    try:
        _i, res = browser_client.fetch_any(BOX_FETCH, _box_candidates(ident), url=url, method=method,
                                           headers=headers, body=body)
    except BoxUnavailable as e:
        raise CrunchbaseUpstreamError(f"in-page fetch failed: {e}")
    return _Resp(int(res.get("status") or 0), res.get("body") or "", res.get("headers"),
                 res.get("final_url") or url)


_pool_lock = threading.Lock()
_identities = deque()                # live identities, oldest first
_mint_slots = threading.BoundedSemaphore(config.CRUNCHBASE_PARALLEL_MINTS)   # mints in flight at once
_egress_cooling = {}                 # egress -> epoch when usable again
_failed_until = 0.0                  # every mint attempt failed recently
_spare_thread = {"t": None}


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
    """Does this identity's exit answer the API over curl? Probed with the
    call the identity is minted for, a one-row company search. Until
    2026-11-12 the probe was an autocomplete GET, which Cloudflare now
    challenges for every curl client whatever the cookie (see autocomplete):
    every mint failed it, every identity fell into browser mode, and the
    in-page searches there were refused more often than not."""
    body = json.dumps({"field_ids": ["identifier"], "order": [], "query": [], "field_aggregators": [],
                       "collection_id": "organization.companies", "limit": 1})
    sess = _new_session(proxy)
    try:
        resp = sess.post(f"{SITE}/v4/data/searches/organization.companies?source=collection_advanced_search",
                         headers=_headers(ua, "post"), cookies=cookies, data=body, timeout=TIMEOUT)
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
            cookies, ua = _box_mint(proxy)
        except CrunchbaseUpstreamError:
            raise                        # the box is down: retried by request(), no exit is blamed
        except Exception as e:
            last = e
            print(f"crunchbase: mint on {_egress(proxy)} failed: "
                  f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")
            if proxy is None:
                _cool_egress("direct", config.CRUNCHBASE_RATE_LIMIT_COOLDOWN)
            continue
        mode = "curl" if _curl_validates(cookies, ua, proxy) else "browser"
        if mode == "browser":
            print("crunchbase: the minted cookie does not replay over curl; serving it in-page on the box")
        ident = Identity(cookies, ua, proxy, mode)
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
    """A live identity on a non-cooling egress, ready for one request.

    Organization work: the identity with the most quota left (cookies are
    spent evenly, the freshest survives longest), with its unit RESERVED
    here under the pool lock: until 2026-11-04 the reservation came after
    the pick, so concurrent callers raced for a cookie's last units and
    every loser burned one of its four attempts. General work (searches,
    people, events...): the least recently used identity, so it spreads
    over every live cookie and exit instead of landing on the oldest one
    and its ~150-200 / 10 min per-IP limit."""
    with _pool_lock:
        live = [i for i in _identities if not i.retired and not i.expired() and not egress_cooling(i.egress)]
        if need_org:
            best = None
            for ident in live:
                if ident.org_left() > 0 and (best is None or ident.org_left() > best.org_left()):
                    best = ident
            if best is not None and not best.take_org():
                return None
        else:
            best = min(live, key=lambda i: i.last_used, default=None)
        if best is not None:
            best.last_used = time.monotonic()
        return best


def _org_left_total():
    with _pool_lock:
        return sum(i.org_left() for i in _identities if not i.retired and not i.expired())


def _live_count():
    with _pool_lock:
        return sum(1 for i in _identities if not i.retired and not i.expired() and not egress_cooling(i.egress))


def _background_mint(reason, still_needed):
    """One mint on a background thread (never two at once) unless the pool
    is cooling after a failed round; `still_needed()` is re-checked once
    the thread holds a mint slot, in case a foreground mint landed first."""
    def run():
        with _mint_slots:
            if not still_needed():
                return
            try:
                _mint()
            except Exception as e:
                print(f"crunchbase: background mint ({reason}) failed: {e}")

    # the alive check and the registration are one step under the lock:
    # two callers seeing "not alive" at once would both start a thread
    with _pool_lock:
        alive = _spare_thread["t"] is not None and _spare_thread["t"].is_alive()
        if alive or time.time() < _failed_until:
            return
        t = threading.Thread(target=run, name="crunchbase-spare-mint", daemon=True)
        _spare_thread["t"] = t
    t.start()


def _ensure_spare():
    """Mint a fresh identity in the background when every live one has
    little organization quota left, so the next profile does not wait."""
    half = config.CRUNCHBASE_ORG_QUOTA // 2
    if _org_left_total() > half:
        return
    _background_mint("organization quota low", lambda: _org_left_total() <= half)


def _grow_pool():
    """Under traffic, keep CRUNCHBASE_IDENTITIES live identities (minted
    one at a time in the background, never while idle) so general work
    has that many cookies and exits to spread over."""
    if _live_count() >= config.CRUNCHBASE_IDENTITIES:
        return
    _background_mint("pool below CRUNCHBASE_IDENTITIES", lambda: _live_count() < config.CRUNCHBASE_IDENTITIES)


def acquire(need_org):
    """An identity ready for one request (its organization unit reserved
    when need_org); mints when none qualifies, up to
    CRUNCHBASE_PARALLEL_MINTS mints at once, callers past that waiting for
    a slot and re-checking the pool first (a mint that just landed usually
    serves them without one of their own)."""
    _prune()
    ident = _pick(need_org)
    if ident is not None:
        _grow_pool()
        return ident
    if time.time() < _failed_until:
        raise CrunchbaseBlocked("Cloudflare would not clear on any exit recently; retrying after the cooldown")
    with _mint_slots:
        ident = _pick(need_org)
        if ident is not None:
            return ident
        ident = _mint()
        if need_org:
            ident.take_org()
        ident.last_used = time.monotonic()
        return ident


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
        ident = acquire(need_org)        # its organization unit already reserved (when need_org)
        headers = _headers(ident.ua, kind)
        try:
            if ident.mode == "browser":
                resp = _box_fetch(ident, method, url, {k: v for k, v in headers.items()
                                                       if k in ("accept", "content-type", "x-requested-with",
                                                                "x-cb-client-app-instance-id")}, body)
            else:
                sess = _session_for(ident)
                if method == "POST":
                    resp = sess.post(url, headers=headers, cookies=ident.cookies, data=body, timeout=TIMEOUT)
                else:
                    resp = sess.get(url, headers=headers, cookies=ident.cookies, timeout=TIMEOUT)
        except BoxRefused as e:
            # The box dropped that identity's browser on a 403/429. Its
            # message carries the verdict (recipes/crunchbase_fetch.py):
            # error 1015 cools the egress, anything else is a dead cookie /
            # spent quota, exactly as the curl replies below.
            if "rate-limited" in str(e):
                _cool_egress(ident.egress, config.CRUNCHBASE_RATE_LIMIT_COOLDOWN)
            else:
                retire(ident, "quota spent" if need_org else "cookie challenged")
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


# Typeahead over the search endpoint: the rank each collection sorts on
# (best first). Collections not listed have no rank and sort by name, which
# puts the rows named exactly like the query first.
_TYPEAHEAD_RANK = {
    "organizations": "rank_org",
    "organization.companies": "rank_org_company",
    "principal.investors": "rank_principal_investor",
    "organization.schools": "rank_org_school",
    "people": "rank_person",
    "funding_rounds": "rank_funding_round",
    "acquisitions": "rank_acquisition",
    "ipos": "rank_ipo",
    "events": "rank_event",
    "hubs": "rank_hub",
}
# Collections whose search selects nothing but the identifier.
_TYPEAHEAD_BARE = ("categories", "category_groups", "products")


def _typeahead(collection, query):
    """Rows of one collection whose name matches `query` -> (rows in the
    autocomplete shape, match count). Two searches side by side: `starts`
    matches a prefix of the whole name ("fin" -> FinTech), `contains` a
    whole word anywhere in it ("altman" -> Sam Altman). A ranked collection
    comes back best rank first; an unranked one exact names first, then
    prefixes, then the rest. A location query may carry qualifiers after
    commas ("San Francisco, California"): each one must appear in the
    location's description ("San Francisco, California, United States,
    North America")."""
    rank = _TYPEAHEAD_RANK.get(collection)
    fields = ["identifier"] if collection in _TYPEAHEAD_BARE else ["identifier", "short_description", "facet_ids"]
    if rank:
        fields.append(rank)
    order = [{"field_id": rank or "identifier", "sort": "asc"}]
    name, extra = query.strip(), []
    if collection.startswith("location"):
        name, *qualifiers = [part.strip() for part in query.split(",")]
        extra = [predicate("short_description", "contains", [q]) for q in qualifiers if q]
    if not name:
        return [], 0
    pages = run_parallel([lambda op=op: search(collection, fields, [predicate("identifier", op, [name])] + extra, order)
                          for op in ("starts", "contains")])
    rows, seen = [], set()
    for data in pages:
        for row in data.get("entities") or []:
            props = row.get("properties") or {}
            ident = props.get("identifier")
            if not isinstance(ident, dict):
                continue
            uuid = row.get("uuid") or ident.get("uuid")
            if uuid:                         # a row without a uuid is never a duplicate of another
                if uuid in seen:
                    continue
                seen.add(uuid)
            rows.append(props)
    low = name.lower()

    def position(indexed):
        index, props = indexed
        if rank:
            value = props.get(rank)
            return value if isinstance(value, (int, float)) else float("inf"), index
        value = (props["identifier"].get("value") or "").lower()
        return 0 if value == low else 1 if value.startswith(low) else 2, index
    return ([props for _i, props in sorted(enumerate(rows), key=position)],
            max(data.get("count") or 0 for data in pages))


def autocomplete(query, collection_ids, limit=10):
    """Typeahead: rows of `collection_ids` (comma-separated) whose name
    matches `query` -> {count, entities: [{identifier, short_description,
    facet_ids}]}, the shape of GET /v4/data/autocompletes.

    That endpoint is no longer usable (2026-11-12): Cloudflare answers it
    with a managed challenge to every curl client, with or without a minted
    cf_clearance and whatever the TLS profile, and to an in-page fetch from
    any browser that was not itself challenged first (box probes: 0 of 10 on
    Ubuntu, 2 of 10 on Windows), while the searches and the entity endpoint
    answer plain curl. So the rows come from the search endpoint, two
    searches per collection (_typeahead), several collections interleaved.
    What it costs: no relevance score (rank order instead; locations,
    categories, funds and products have no rank and come back by name), and
    15 rows per search at most (the anonymous cap), so a name shared by more
    places than that needs a qualifier to surface the one meant."""
    collections = [c.strip() for c in collection_ids.split(",") if c.strip()]
    results = run_parallel([lambda c=c: _typeahead(c, query) for c in collections])
    queues = [list(rows) for rows, _total in results]
    merged = []
    while any(queues):                     # round robin, so no collection crowds the others out
        for queue in queues:
            if queue:
                merged.append(queue.pop(0))
    return {"count": sum(total for _rows, total in results), "entities": merged[:int(limit)]}


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
                                     headers={"accept": "application/json", "accept-language": egress.site_accept_language("crunchbase")})
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
    return fanout.per_call_map(fns, workers)


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
