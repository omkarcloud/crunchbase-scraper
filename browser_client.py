"""GKE's one client for the browser boxes, and the mint cache in front of it.

    ident = browser_client.identity("zillow", egress)        # cached mint
    tok   = browser_client.token("google-flights", freq)     # per-request token
    resp  = browser_client.fetch("zoopla", url=..., ...)     # raw page/body
    sess  = browser_client.BoxSession("g2", proxy=...)       # driver-shaped get/get_many
                                                             # on one kept browser

Which box serves a site comes from recipes/registry.py (plain data). Box
URLs come from BROWSER_BOX_URLS="windows=http://ip:6010,ubuntu=http://ip:6010".
No failover: each site has exactly one box. A box that is down or busy
raises BoxUnavailable quickly; sites decide what to answer.

Mint cache (owner decision 2026-10-21): one entry per (site, proxy session
or "direct") until its expires_at. One mint at a time per key: the first
thread holding a key's lock mints, every other thread waits on that lock and
reads the result, so a cold burst causes one /mint, never N. In-process:
the scrapers pod is one process (SCRAPERS_THREADS, 64 on relays and
patchright). A per-site re-mint budget
(REMINT_PER_MINUTE) stops a block from burning proxy sessions in a loop.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests
except ImportError:     # only posts to a box need it; with no box configured nothing is posted
    requests = None

from recipes.registry import box_for
import fastjson

TIMEOUT_MINT = 120
TIMEOUT_TOKEN = 60
TIMEOUT_FETCH = 180
# A contact job on the box is up to TWO crawls (own IP, then the residential
# fallback once that one ended blocked), each bounded by the crawler's
# MAX_CRAWL_SECONDS (120) plus what may overrun it: a blocked-mode navigation
# started just inside the deadline (the driver's challenge budget, ~90 s)
# and the tech-detection wait (TECH_WAIT_SECONDS 30). ~250 s per crawl, so
# 600 s covers both with margin; GKE never cuts a job the box is finishing.
TIMEOUT_CONTACT_JOB = 600
REMINT_PER_MINUTE = int(os.environ.get("BROWSER_REMINT_PER_MINUTE", "5"))


class BoxUnavailable(Exception):
    """The box is down, draining or full (503 busy). Fast, not retried here.
    `candidate`: index of the candidate browser it happened on (None when no
    browser was reached); `proxy`: that candidate's exit (set by the caller
    that sent the candidates)."""

    def __init__(self, message="", candidate=None):
        super().__init__(message)
        self.candidate = candidate
        self.proxy = None


class BoxRefused(Exception):
    """The recipe could not pass the site (502 recipe) or rejected the input
    (4xx). `candidate` / `proxy` as on BoxUnavailable: which of the sent
    candidates was refused, so the caller rotates that exit."""

    def __init__(self, message="", candidate=None):
        super().__init__(message)
        self.candidate = candidate
        self.proxy = None


def _box_urls() -> dict:
    raw = os.environ.get("BROWSER_BOX_URLS", "")
    out = {}
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip().rstrip("/")
    return out


def box_url(site: str) -> str:
    box = box_for(site)
    urls = _box_urls()
    try:
        return urls[box]
    except KeyError:
        raise BoxUnavailable(f"no URL for box {box!r} in BROWSER_BOX_URLS") from None


def _post(site: str, action: str, body: dict, timeout: int) -> dict | list:
    return _post_any(site, action, body, timeout)[1]


def _post_any(site: str, action: str, body: dict, timeout: int) -> tuple:
    """POST to the box -> (candidate index the box used or None, JSON body).
    Errors carry the candidate too (see BoxRefused)."""
    base = box_url(site)
    if base == INPROCESS:
        return _act_inprocess(site, action, body)
    url = f"{base}/{site}/{action}"
    try:
        r = requests.post(url, json=body, timeout=timeout)
    except requests.RequestException as e:
        raise BoxUnavailable(f"{site}/{action}: box unreachable ({type(e).__name__}: {e})")
    if r.status_code == 503:
        raise BoxUnavailable(f"{site}/{action}: {_msg(r)}", _candidate_of(r))
    if r.status_code == 502 or 400 <= r.status_code < 500:
        raise BoxRefused(f"{site}/{action}: {r.status_code} {_msg(r)}", _candidate_of(r))
    if r.status_code != 200:
        raise BoxUnavailable(f"{site}/{action}: box answered {r.status_code} {_msg(r)}", _candidate_of(r))
    header = r.headers.get("X-Box-Candidate")
    return (int(header) if header not in (None, "") else None), r.json()


INPROCESS = "inprocess"      # BROWSER_BOX_URLS="ubuntu=inprocess": no box, the recipe runs in this process


def _act_inprocess(site: str, action: str, body: dict) -> tuple:
    """_post_any() without the HTTP hop: the recipe runs here, on this
    machine's own Chrome (browser_worker.inprocess). Statuses map to the same
    errors as a box's answers, and the result takes the same JSON round trip."""
    from browser_worker import inprocess
    status, out, candidate = inprocess.act(site, action, body)
    if status == 200:
        return candidate, fastjson.loads(fastjson.dumps(out))
    message = out.get("error") or ""
    if status == 503:
        raise BoxUnavailable(f"{site}/{action}: {message}", candidate)
    if status == 502 or 400 <= status < 500:
        raise BoxRefused(f"{site}/{action}: {status} {message}", candidate)
    raise BoxUnavailable(f"{site}/{action}: box answered {status} {message}", candidate)


def _candidate_of(r) -> Optional[int]:
    try:
        value = r.json().get("candidate")
        return int(value) if value is not None else None
    except Exception:
        return None


def _msg(r) -> str:
    try:
        return r.json().get("error") or r.text[:200]
    except Exception:
        return r.text[:200]


# ---- actions -------------------------------------------------------------------------

def mint(site: str, proxy: Optional[str] = None, variant: str = "") -> dict:
    """Raw /mint: identity dict from the box. Prefer identity() for caching."""
    body = {"variant": variant}
    if proxy:
        body["proxy"] = proxy
    ident = _post(site, "mint", body, TIMEOUT_MINT)
    if isinstance(ident, dict):
        # the exit the identity was minted on, so accept_language_for() can
        # derive the browser's language from it when the box (an older one)
        # sent no accept_language / languages (identity() sets it as well)
        ident.setdefault("proxy", proxy)
    return ident


def token(site: str, input, proxy: Optional[str] = None, variant: str = "",
          timeout: Optional[int] = None) -> str:
    """`timeout` for recipes whose cold path outlives TIMEOUT_TOKEN (a
    google-flights token = up to 20 s queue + a 120 s warm)."""
    body = {"variant": variant, "input": input}
    if proxy:
        body["proxy"] = proxy
    return _post(site, "token", body, timeout or TIMEOUT_TOKEN)["token"]


def fetch(site: str, url: Optional[str] = None, urls: Optional[list] = None, method: str = "GET",
          headers: Optional[dict] = None, body=None, proxy: Optional[str] = None,
          variant: str = "", session: Optional[str] = None, timeout: Optional[int] = None):
    payload = {"variant": variant, "method": method, "headers": headers or {}, "body": body}
    if url:
        payload["url"] = url
    if urls:
        payload["urls"] = urls
    if proxy:
        payload["proxy"] = proxy
    if session:
        payload["session"] = session
    return _post(site, "fetch", payload, timeout or TIMEOUT_FETCH)


def fetch_any(site: str, candidates: list, url: Optional[str] = None, urls: Optional[list] = None,
              method: str = "GET", headers: Optional[dict] = None, body=None, variant: str = "",
              timeout: Optional[int] = None) -> tuple:
    """fetch() on whichever of `candidates` [(proxy, session), ...] — browsers
    the caller can use interchangeably — the box picks: an idle one (warmest
    first), else a new one within the caps, else the first to free up.
    Returns (index into candidates, result). BoxRefused / BoxUnavailable
    carry `.candidate`, the index it happened on (None if no browser was
    reached), so the caller rotates that exit only."""
    payload = {"variant": variant, "method": method, "headers": headers or {}, "body": body,
               "candidates": [{"proxy": p, "session": s} for p, s in candidates]}
    if url:
        payload["url"] = url
    if urls:
        payload["urls"] = urls
    try:
        index, out = _post_any(site, "fetch", payload, timeout or TIMEOUT_FETCH)
    except (BoxRefused, BoxUnavailable) as e:
        if e.candidate is not None and 0 <= e.candidate < len(candidates):
            e.proxy = candidates[e.candidate][0]
        raise
    if index is None or not 0 <= index < len(candidates):
        raise BoxUnavailable(f"{site}/fetch: the box did not name the candidate it used ({index!r})")
    return index, out


def token_any(site: str, candidates: list, input, variant: str = "", timeout: Optional[int] = None) -> tuple:
    """token() on whichever of `candidates` [(proxy, session), ...] the box
    picks (same rules as fetch_any; the box's token action takes candidates
    since 2026-10-26, this client form since 2026-11-04). Returns (index
    into candidates, token); BoxRefused / BoxUnavailable carry `.candidate`
    and `.proxy` like fetch_any."""
    payload = {"variant": variant, "input": input,
               "candidates": [{"proxy": p, "session": s} for p, s in candidates]}
    try:
        index, out = _post_any(site, "token", payload, timeout or TIMEOUT_TOKEN)
    except (BoxRefused, BoxUnavailable) as e:
        if e.candidate is not None and 0 <= e.candidate < len(candidates):
            e.proxy = candidates[e.candidate][0]
        raise
    if index is None or not 0 <= index < len(candidates):
        raise BoxUnavailable(f"{site}/token: the box did not name the candidate it used ({index!r})")
    return index, out["token"]


def contact_job(query: str, mode: str = "key_pages", fallback_proxy: Optional[str] = None) -> dict:
    """The contact scraper's whole-job hand-over (POST /contact/job on the
    Windows box): the box crawls `query` in `mode` with browser escalation
    enabled, retrying once through `fallback_proxy` (a residential URL; the
    box holds no credentials) if its own IP ends blocked, and returns the
    finished output dict - the same shape contact_scraper.aggregate builds -
    which the caller returns unchanged."""
    return _post("contact", "job",
                 {"query": query, "mode": mode, "fallback_proxy": fallback_proxy},
                 TIMEOUT_CONTACT_JOB)


# ---- sticky exits per site ------------------------------------------------------------
# The box keys a kept browser by the proxy URL it was launched with, so GKE
# must hand the SAME URL to every call that should land on the same browser
# and mint a NEW one only when that browser is burned (BoxRefused). Before
# 2026-10-23 every migrated module carried its own copy of that holder (and
# three of them minted a fresh exit per call by mistake, which the D10 run
# showed as "box full" evictions on zoopla). This is the one holder.

class ExitPool:
    """Sticky proxy exits for one site's box browsers.

    `mint` is the config helper that returns a fresh proxy URL (or None for
    the box's own IP). `size` (int or callable) is the site's guaranteed
    SHARE of box browsers (= its recipe's max_browsers): that many exits are
    kept per key, so concurrent calls spread over that many box browsers;
    `key` separates families (a country, a variant/host).

    `burst=True` offers size x config.BROWSER_BURST_FACTOR exits instead
    (since 2026-11-04): an exit is a proxy URL built locally, and the box
    opens a browser for a candidate above the share only while it has CPU
    and memory headroom (browser_worker/lifecycle.py _admit_locked), so the
    extra candidates cost nothing until the box is idle enough to use them.
    Pools whose ONE identity is a design choice leave it off.

        pool = ExitPool("g2", config.g2_proxy, size=lambda: config.G2_SESSIONS)
        proxy = pool.get()              # sticky, round-robin over `size` slots
        try:
            browser_client.fetch("g2", ..., proxy=proxy)
        except BoxRefused:
            pool.rotate(proxy)          # replaced only if still the one in use

    rotate() of an exit another thread already replaced is a no-op: two
    calls failing on one exit burn one fresh proxy, not two.

    Pools of more than one exit should let the BOX choose instead of get():
    fetch_any() sends every exit of the key as a candidate and the box hands
    back an idle browser (warmest first), opens a new one, or waits for the
    first to free up; get() picks blindly in turn and can queue a call
    behind a busy browser while another sits idle.

        proxy, res = pool.fetch_any("rottentomatoes-shape", url=...)
        ...
        except BoxRefused as e:
            pool.rotate(e.proxy)        # the exit the box was using"""

    def __init__(self, site: str, mint, size=1, burst: bool = False, uses_per_exit=None):
        self.site = site
        self._mint = mint
        self._size = size
        self.burst = burst
        self._uses_per_exit = uses_per_exit   # int or callable: rotate an exit after this many successes
        self._lock = threading.Lock()
        self._slots: dict = {}          # key -> list of proxy URLs (or None)
        self._next: dict = {}           # key -> round-robin cursor
        self._filled: dict = {}         # key -> set of slot indexes already minted
        self._uses: dict = {}           # (key, proxy) -> successful calls on that exit

    def _spend(self, key: str, proxy) -> bool:
        """Count one successful call on `proxy`; rotate it when the exit's
        use budget is spent (a site whose per-IP rule challenges after N
        pages: the pod retires the EXIT, not just the box's browser, so the
        relaunch is not on the same spent IP). True when rotated."""
        budget = self._uses_per_exit() if callable(self._uses_per_exit) else self._uses_per_exit
        if not budget:
            return False
        with self._lock:
            n = self._uses[(key, proxy)] = self._uses.get((key, proxy), 0) + 1
        if n < budget:
            return False
        print(f"{self.site}: exit {proxy} spent after {n} uses; rotating")
        return self.rotate(proxy, key)

    def share(self) -> int:
        """The site's guaranteed share (its `size`), whatever `burst` says."""
        n = self._size() if callable(self._size) else self._size
        return max(1, int(n or 1))

    def _n(self) -> int:
        import config
        return config.burst_slots(self.share()) if self.burst else self.share()

    def _resize_locked(self, key: str, n: int):
        """The slot list of `key` at exactly n entries. A shrink also drops
        the dropped indexes from `_filled`: until 2026-11-04 they stayed, so
        a later grow re-created those slots as None (= the box's own IP) and
        never re-minted them."""
        slots = self._slots.setdefault(key, [])
        filled = self._filled.setdefault(key, set())
        while len(slots) < n:
            slots.append(None)
        if len(slots) > n:
            del slots[n:]
            filled.difference_update(i for i in list(filled) if i >= n)
        return slots, filled

    def _mint_for(self, key: str):
        """A fresh exit for a slot of `key` (called under the lock).
        Subclasses whose exit depends on the key (a country, a variant)
        override this rather than rebinding `_mint` per call."""
        return self._mint()

    def get(self, key: str = ""):
        """The exit for the next call of `key` (minted on first use)."""
        with self._lock:
            n = self._n()
            slots, filled = self._resize_locked(key, n)
            i = self._next.get(key, 0) % n
            self._next[key] = (i + 1) % n
            if i not in filled:
                slots[i] = self._mint_for(key)
                filled.add(i)
            return slots[i]

    def all(self, key: str = "") -> list:
        """Every exit of `key`, slot order (each slot minted on first use)."""
        with self._lock:
            n = self._n()
            slots, filled = self._resize_locked(key, n)
            for i in range(n):
                if i not in filled:
                    slots[i] = self._mint_for(key)
                    filled.add(i)
            return list(slots)

    def fetch_any(self, site: str, key: str = "", sessions=None, **kw) -> tuple:
        """browser_client.fetch_any over every exit of `key` -> (the exit the
        box used, result). `sessions(proxy) -> str` names each candidate's
        browser when the recipe keys by a session id rather than the proxy.
        A BoxRefused / BoxUnavailable carries `.proxy`, the exit it
        happened on (None when no browser was reached)."""
        exits = self.all(key)
        candidates = [(p, sessions(p) if sessions else None) for p in exits]
        try:
            index, out = fetch_any(site, candidates, **kw)
        except (BoxRefused, BoxUnavailable) as e:
            if e.candidate is not None and 0 <= e.candidate < len(exits):
                e.proxy = exits[e.candidate]
            raise
        self._spend(key, exits[index])
        return exits[index], out

    def token_any(self, site: str, key: str = "", sessions=None, **kw) -> tuple:
        """browser_client.token_any over every exit of `key` -> (the exit the
        box used, token). `sessions(proxy) -> str` as on fetch_any."""
        exits = self.all(key)
        candidates = [(p, sessions(p) if sessions else None) for p in exits]
        try:
            index, tok = token_any(site, candidates, **kw)
        except (BoxRefused, BoxUnavailable) as e:
            if e.candidate is not None and 0 <= e.candidate < len(exits):
                e.proxy = exits[e.candidate]
            raise
        self._spend(key, exits[index])
        return exits[index], tok

    def rotate(self, proxy, key: str = "") -> bool:
        """Replace `proxy` with a fresh exit (the box burned its browser).
        False when it was already replaced by another caller."""
        with self._lock:
            slots = self._slots.get(key) or []
            filled = self._filled.setdefault(key, set())
            for i, cur in enumerate(slots):
                if cur == proxy and i in filled:
                    slots[i] = self._mint_for(key)
                    self._uses.pop((key, proxy), None)
                    return True
            return False

    def current(self, key: str = ""):
        """The exits in use for `key` (for logs/tests)."""
        with self._lock:
            return list(self._slots.get(key) or [])

    def reset(self, key=None) -> None:
        with self._lock:
            if key is None:
                self._slots.clear(); self._next.clear(); self._filled.clear(); self._uses.clear()
            else:
                self._slots.pop(key, None); self._next.pop(key, None); self._filled.pop(key, None)
                for k in [k for k in self._uses if k[0] == key]:
                    self._uses.pop(k, None)


# ---- mint cache ---------------------------------------------------------------------

_cache: dict = {}            # (site, key) -> identity dict (with expires_at)
_locks: dict = {}            # (site, key) -> Lock  (single-flight per key)
_locks_guard = threading.Lock()
_remints: dict = {}          # site -> [monotonic timestamps of mints in the last minute]


class RemintBudgetExceeded(Exception):
    pass


def _key(proxy: Optional[str]) -> str:
    if not proxy:
        return "direct"
    from urllib.parse import urlsplit
    return f"p{urlsplit(proxy).port}"


def _lock_for(site, key):
    with _locks_guard:
        return _locks.setdefault((site, key), threading.Lock())


def _charge_budget(site: str) -> None:
    now = time.monotonic()
    ts = [t for t in _remints.get(site, []) if t > now - 60]
    if len(ts) >= REMINT_PER_MINUTE:
        raise RemintBudgetExceeded(f"{site}: {REMINT_PER_MINUTE} mints in the last minute")
    ts.append(now)
    _remints[site] = ts


def identity(site: str, proxy: Optional[str] = None, variant: str = "", force: bool = False) -> dict:
    """The cached identity for (site, proxy session), minting on a miss,
    expiry, or `force` (after a block). Single-flight per key."""
    key = (site, _key(proxy), variant)
    now = time.time()
    if not force:
        ident = _cache.get(key)
        if ident and ident.get("expires_at", 0) > now:
            return ident
    with _lock_for(site, key[1:]):
        ident = _cache.get(key)
        if not force and ident and ident.get("expires_at", 0) > now:
            return ident
        if force and ident and ident.get("minted_at", 0) > now - 5:
            return ident               # a peer just re-minted
        _charge_budget(site)
        ident = mint(site, proxy, variant)
        ident["minted_at"] = time.time()
        ident["proxy"] = proxy
        _cache[key] = ident
        return ident


def invalidate(site: str, proxy: Optional[str] = None, variant: str = "") -> None:
    _cache.pop((site, _key(proxy), variant), None)


def cookie_header(ident: dict) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in ident.get("cookies", []))


def accept_language_for(ident: dict) -> str:
    """The Accept-Language the minting browser announced: the identity's own
    (`accept_language`, boxes since 2026-10-29), else the list of the
    languages it reported, else derived from the proxy it was minted on
    (older cached identities, or a mint with no derived locale)."""
    if ident.get("accept_language"):
        return ident["accept_language"]
    import egress
    langs = ident.get("languages")
    if isinstance(langs, list) and langs:
        return egress.accept_header(langs)      # the q-weighted header Chrome builds from them
    return egress.accept_language(ident.get("proxy"))


def headers_for(ident: dict, **extra) -> dict:
    """Request headers that make a curl replay look like the browser that
    minted `ident`: its User-Agent, its Accept-Language and its client-hint
    brands. `extra` adds or overrides (accept, referer, content-type...);
    the cookie is separate (cookie_header)."""
    out = {"user-agent": ident.get("user_agent") or "",
           "accept-language": accept_language_for(ident)}
    hints = ident.get("client_hints") or {}
    brands = hints.get("brands")
    if brands:
        out["sec-ch-ua"] = ", ".join(f'"{b.get("brand")}";v="{b.get("version")}"' for b in brands)
        out["sec-ch-ua-mobile"] = "?1" if hints.get("mobile") else "?0"
        if hints.get("platform"):
            out["sec-ch-ua-platform"] = f'"{hints["platform"]}"'
    out.update({k.lower(): v for k, v in extra.items() if v is not None})
    return {k: v for k, v in out.items() if v != ""}


def impersonation_for(ident: dict, default: str = "chrome") -> str:
    """The curl_cffi impersonation matching the box's Chrome major, when
    curl_cffi has one; else `default`."""
    major = ident.get("chrome_major")
    if not major:
        return default
    try:
        from curl_cffi import requests as curl
        known = {int(n[len("chrome"):]) for n in curl.BrowserType.__members__ if n.startswith("chrome") and n[len("chrome"):].isdigit()}
    except Exception:
        return default
    if major in known:
        return f"chrome{major}"
    lower = [n for n in known if n <= major]
    return f"chrome{max(lower)}" if lower else default


# ---- session shim (kept-browser sites: g2) ------------------------------------------

class BoxSessionRefused(BoxRefused):
    """The box dropped the kept browser (recipe refusal on warm or on a
    fetch: Cloudflare/DataDome burned the exit). Callers rotate their proxy
    and retry the whole unit."""


@dataclass
class FetchResponse:
    """patchright_driver.FetchResponse's shape (ok, status_code, url, text,
    error, headers) without importing the driver module on GKE: what a
    kept-browser site's fetchers (g2/fetch.fetch_html*) read."""
    ok: bool
    status_code: int
    url: str
    text: str
    error: Optional[str] = None
    headers: dict = field(default_factory=dict)

    def json(self):
        return fastjson.loads(self.text)


class BoxSession:
    """A driver-shaped handle on ONE kept browser of a session_fetch recipe:
    `get` / `get_many` are the box's in-page fetches on the browser the box
    keeps for (site, proxy session or `session` id). `referrer` travels as
    the `referer` header; the recipe turns it back into the fetch's referrer
    (a page-level fetch() cannot set that header). A 502 from the recipe
    (the box closed the browser) raises BoxSessionRefused; a down or busy
    box raises BoxUnavailable."""

    def __init__(self, site: str, proxy: Optional[str] = None, variant: str = "",
                 session: Optional[str] = None, candidates: Optional[list] = None):
        """`candidates` [(proxy, session), ...] instead of one proxy/session:
        the box picks a free one on the first call (fetch_any), and the
        handle stays on that browser for every later call (`chosen` is its
        index, `proxy` / `session` its own)."""
        self.site, self.proxy, self.variant, self.session = site, proxy, variant, session
        self.candidates = list(candidates) if candidates else None
        self.chosen = None

    @staticmethod
    def _headers(headers, referrer):
        out = dict(headers or {})
        if referrer:
            out["referer"] = referrer
        return out

    @staticmethod
    def _response(url: str, out: dict) -> FetchResponse:
        status = int(out.get("status") or 0)
        return FetchResponse(ok=status > 0, status_code=status, url=out.get("final_url") or url,
                             text=out.get("body") or "", error=None if status else "no response",
                             headers=dict(out.get("headers") or {}))

    def _fetch(self, **kw):
        if self.candidates and self.chosen is None:
            try:
                index, out = fetch_any(self.site, self.candidates, variant=self.variant, **kw)
            except BoxRefused as e:
                refused = BoxSessionRefused(str(e), e.candidate)
                refused.proxy = e.proxy
                raise refused from None
            self.chosen = index
            self.proxy, self.session = self.candidates[index]
            return out
        try:
            return fetch(self.site, proxy=self.proxy, variant=self.variant, session=self.session, **kw)
        except BoxRefused as e:
            refused = BoxSessionRefused(str(e), self.chosen)
            refused.proxy = self.proxy
            raise refused from None

    def get(self, url: str, headers: Optional[dict] = None, referrer: Optional[str] = None) -> FetchResponse:
        return self._response(url, self._fetch(url=url, headers=self._headers(headers, referrer)))

    def get_many(self, urls: list, headers: Optional[dict] = None, referrer: Optional[str] = None) -> list:
        if not urls:
            return []
        outs = self._fetch(urls=list(urls), headers=self._headers(headers, referrer))
        if not isinstance(outs, list) or len(outs) != len(urls):
            raise BoxUnavailable(f"{self.site}/fetch: box answered {len(outs) if isinstance(outs, list) else 'one'} "
                                 f"results for {len(urls)} urls")
        return [self._response(u, o) for u, o in zip(urls, outs)]


def _reset_for_tests():
    _cache.clear()
    _remints.clear()
    with _locks_guard:
        _locks.clear()
