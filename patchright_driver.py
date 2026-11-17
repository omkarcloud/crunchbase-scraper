"""Patchright (patched Playwright Chrome) driver for chrome_manager pools.

Used by the g2 pool: Cloudflare passes patchright's real-Chrome fingerprint
(channel="chrome", headed) where botasaurus Chrome and Camoufox get challenged
— validated by the patchright_test.py / patchright_fetch_test.py probes.

Patchright wraps Playwright's *sync* API, which is greenlet-bound: every object
must be used from the thread that created it. chrome_manager builds drivers on
warmer threads and serves them to cheroot request threads, so each
PatchrightDriver owns a dedicated daemon thread that launches the browser and
executes every page operation; the public methods marshal callables onto that
thread and wait for the result (same design as camoufox_driver).

The browser runs headed by default (Cloudflare flags headless Chrome). On
Linux without a DISPLAY, one process-wide Xvfb virtual display is started
lazily and shared by every driver (pyvirtualdisplay, the same mechanism
botasaurus uses) — it lives for the process lifetime, so a driver restart
never yanks the display out from under live browsers. Pools that face no
anti-bot (the contact-csr renderer) can opt into headless=True and
block_resources=[...] so client-side apps render without images/CSS/fonts/
media: image and font are Chrome flags (never requested at all), stylesheet
and media are aborted through a URL-pattern route (see url_blocking), which
also takes block_hosts=[...] host globs for trackers/CDNs a page can do without.

Each driver gets a throwaway profile dir (fresh identity per driver, matching
its fresh sticky proxy); close() removes it. A wedged page op (call() timeout)
leaves the owner thread stuck inside Playwright, so close() force-kills the
driver's OS process tree (the playwright node driver; Chrome runs under it),
recorded by pid-diff around the launch. kill_orphan_browsers() lets the pool
janitor reap any tree that still slips through — identified by the playwright
run-driver cmdline of the tree root, which botasaurus Chrome and Camoufox
trees never match.
"""
import json
from hostenv import IS_IN_KUBERNETES, IS_DOCKER, get_os
import os
import queue
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

import psutil
from patchright.sync_api import sync_playwright

STARTUP_TIMEOUT = 120    # browser launch + first page
CALL_TIMEOUT = 90        # hard ceiling per marshalled op
GOTO_TIMEOUT_MS = 60000  # residential exits are slow
FETCH_TIMEOUT_MS = 30000  # per in-page fetch (AbortSignal), under CALL_TIMEOUT

# nav() settle budgets. IDLE is the networkidle fast-exit cap (most pages go
# idle well under 1s; chatty analytics/websocket pages exit at the cap).
# BUDGET is the total deadline for the smart paths: the csr empty-shell
# stability poll and the blocked challenge-clear poll.
SETTLE_IDLE_MS = 2000
SETTLE_BUDGET_MS = 8000

# Blocked-mode challenge clearing (nav(mode='blocked')). A headed real Chrome
# usually clears a Cloudflare managed challenge by itself in 2-8 s; when the
# Turnstile checkbox is shown (residential exits, stricter sites) the loop
# clicks it like a person would: first click 3 s in, then every 4 s, at most
# 4 times. nav() is ONE marshalled op under CALL_TIMEOUT, so the loop and the
# csr settle after it are budgeted from what the goto left over.
CHALLENGE_BUDGET_MS = 25000
CHALLENGE_POLL_MS = 500
CHALLENGE_FIRST_CLICK_MS = 3000
CHALLENGE_CLICK_EVERY_MS = 4000
CHALLENGE_MAX_CLICKS = 4
NAV_OP_BUDGET_MS = CALL_TIMEOUT * 1000 - 5000   # 5 s headroom for the snapshot
NAV_SETTLE_RESERVE_MS = 3000                     # keep a sliver of csr settle after a clear
# Cloudflare detection. The challenge loop runs ONLY on a page identified as
# Cloudflare's own: other vendors' interstitials (PerimeterX, DataDome,
# Akamai, Incapsula) and plain 403s keep the plain csr settle. Evidence:
#   * the goto response's `cf-mitigated: challenge` header;
#   * the interstitial's inline orchestration config `_cf_chl_opt` - only
#     challenge pages carry it;
#   * its "Just a moment..." title, but only together with a
#     challenge-platform script (an app's own loading title is not a
#     challenge).
# Deliberately NOT evidence: a `/cdn-cgi/challenge-platform/` path alone
# (ordinary pages behind Cloudflare Bot Management carry its jsd beacon) and
# a challenges.cloudflare.com iframe (a contact form's Turnstile widget).
CF_CHALLENGE_TITLES = ("just a moment", "one moment, please")
CF_CHALLENGE_HTML = ("_cf_chl_opt",)
# Terminal Cloudflare pages: the firewall block ("Attention Required! |
# Cloudflare" / "Sorry, you have been blocked") and access rules ("Access
# denied | <host> used Cloudflare to restrict access"). The title must name
# Cloudflare: Akamai's own "Access Denied" page is not this.
CF_REFUSED_TITLE_WORDS = ("attention required", "access denied", "restrict access")
CF_REFUSED_HTML = ("Sorry, you have been blocked", "You are unable to access")
_now = time.monotonic   # module attribute: tests swap in a fake clock

# In-page fetch run from the loaded page, so it inherits the page's cookies,
# Cloudflare clearance and TLS fingerprint. The browser attaches the forbidden
# headers (sec-ch-ua*, sec-fetch-*, referer via the referrer option) itself.
# Never throws across the evaluate bridge: returns a structured object so the
# Python side gets a clean error taxonomy.
_FETCH_JS = """async ({url, method, headers, body, referrer, timeoutMs}) => {
    try {
        const r = await fetch(url, {
            method: method || 'GET',
            credentials: 'include',
            headers: headers || {},
            body: body || undefined,
            referrer: referrer || undefined,
            referrerPolicy: 'strict-origin-when-cross-origin',
            signal: AbortSignal.timeout(timeoutMs),
        });
        return { ok: true, status: r.status, url: r.url, body: await r.text(),
                 headers: Object.fromEntries(r.headers.entries()) };
    } catch (e) {
        return { ok: false, status: 0, url: '', body: '', headers: {}, error: String(e) };
    }
}"""

# Batched variant of _FETCH_JS: N fetches launched from one evaluate() call,
# each after i*staggerMs (like a page firing its subresource requests), all
# awaited together. Per-request failures are contained — the array always
# comes back with one structured result per input, in input order.
_FETCH_MANY_JS = """async ({requests, timeoutMs, staggerMs}) => {
    const one = async ({url, method, headers, body, referrer}, i) => {
        try {
            if (i && staggerMs)
                await new Promise(res => setTimeout(res, i * staggerMs));
            const r = await fetch(url, {
                method: method || 'GET',
                credentials: 'include',
                headers: headers || {},
                body: body || undefined,
                referrer: referrer || undefined,
                referrerPolicy: 'strict-origin-when-cross-origin',
                signal: AbortSignal.timeout(timeoutMs),
            });
            return { ok: true, status: r.status, url: r.url, body: await r.text(),
                     headers: Object.fromEntries(r.headers.entries()) };
        } catch (e) {
            return { ok: false, status: 0, url: '', body: '', headers: {}, error: String(e) };
        }
    };
    return Promise.all(requests.map(one));
}"""

# Inter-launch spacing inside one get_many batch. Zeroed Aug 2026 after a
# stress run showed no Cloudflare blocks without it (previous value: 200).
FETCH_STAGGER_MS = 0

DEFAULT_FETCH_HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
}

# Live (unclosed) drivers, so kill_orphan_browsers() knows which OS processes
# are legitimately owned. Guarded by _drivers_lock.
_drivers = set()
_drivers_lock = threading.Lock()

# One shared Xvfb for every patchright driver (Linux containers only).
_display = None
_display_lock = threading.Lock()


def _ensure_display():
    """Start the process-wide virtual display once (Linux, no DISPLAY).
    pyvirtualdisplay exports DISPLAY into os.environ, which the Chrome
    processes inherit at launch."""
    global _display
    if not sys.platform.startswith("linux") or os.environ.get("DISPLAY"):
        return
    with _display_lock:
        if _display is not None:
            return
        from pyvirtualdisplay import Display
        _display = Display(visible=False, size=(1920, 1080))
        _display.start()
        print("patchright: started shared Xvfb virtual display")


def _children_since(pre_launch_pids):
    """Direct children of this process spawned since the snapshot — the OS
    processes of the driver being launched (playwright node driver; Chrome and
    its helpers live under it). Valid because chrome_manager serializes
    construction (_create_lock)."""
    kids = []
    for p in psutil.Process().children():
        if p.pid in pre_launch_pids:
            continue
        try:
            if "resource_tracker" in " ".join(p.cmdline()):
                continue
        except psutil.Error:
            pass
        kids.append(p)
    return kids


def _kill_tree(root):
    """SIGKILL a process and all its descendants. psutil checks creation time
    before signalling, so a recycled pid is a no-op, not a stray kill."""
    try:
        procs = [root] + root.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass


def _is_playwright_root(proc):
    """True when proc is a playwright/patchright node driver — the root of a
    patchright browser tree. Botasaurus Chrome (launched directly) never
    matches."""
    try:
        cmd = " ".join(proc.cmdline())
    except psutil.Error:
        return False
    return "run-driver" in cmd and ("playwright" in cmd or "patchright" in cmd)


def _tree_has_camoufox(root):
    """Camoufox browsers ALSO hang off a playwright run-driver node process,
    so a bare cmdline match on the root cannot tell the two stacks apart. When
    camoufox and patchright pools share one process (ONLY_SCRAPER lists both),
    reaping by root match alone SIGKILLs every live camoufox browser mid-fetch
    — the fetch's evaluate then hangs until the 90s op ceiling ("page
    wedged"). Identify camoufox trees by the browser binary in their
    descendants and leave them to camoufox_driver's own janitor."""
    try:
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return False
    for p in procs:
        try:
            if "camoufox" in p.name().lower() or "camoufox" in " ".join(p.cmdline()).lower():
                return True
        except psutil.Error:
            continue
    return False


_chrome_version_cache = None


def _chrome_candidates():
    """The Chrome executables this host may launch, in the order patchright
    is pointed at them (PATCHRIGHT_EXECUTABLE first), existing ones only."""
    import shutil
    candidates = [
        os.environ.get("PATCHRIGHT_EXECUTABLE"),
        shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    return [exe for exe in dict.fromkeys(candidates) if exe and os.path.exists(exe)]


def chrome_version():
    """Version string of the Chrome patchright launches on this host
    ("156.0.8078.48"), without launching a browser: asks the executable
    directly. Cached per process. Used by the browser worker's /health."""
    global _chrome_version_cache
    if _chrome_version_cache:
        return _chrome_version_cache
    import subprocess
    for exe in _chrome_candidates():
        try:
            if os.name == "nt":
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-Item '{exe}').VersionInfo.ProductVersion"], timeout=10).decode().strip()
            else:
                out = subprocess.check_output([exe, "--version"], timeout=10).decode().strip()
                out = out.split()[-1]
            if out:
                _chrome_version_cache = out
                return out
        except Exception:
            continue
    return "unknown"


def kill_orphan_browsers():
    """Janitor safety net: SIGKILL every patchright browser tree no live driver
    owns. MUST be called with chrome_manager's _create_lock held so no launch
    is mid-flight — otherwise a half-launched browser has processes no driver
    owns yet and would be reaped as an orphan."""
    with _drivers_lock:
        owned = {p.pid for d in _drivers for p in d._procs}
    killed = 0
    for child in psutil.Process().children():
        if child.pid in owned or not _is_playwright_root(child):
            continue
        if _tree_has_camoufox(child):
            continue
        try:
            tree_len = len(child.children(recursive=True)) + 1
        except psutil.Error:
            tree_len = 1
        _kill_tree(child)
        killed += 1
        print(f"patchright: killed orphaned browser tree "
              f"(root pid {child.pid}, {tree_len} procs)")
    return killed


class FetchResponse:
    """Minimal requests-like result from an in-page fetch. NOT raised on
    HTTP/network errors — inspect `.ok`, `.status_code`, `.error`.

      ok           False only for transport failures (network error / abort
                   timeout / bridge miss), NOT for HTTP 4xx/5xx.
      status_code  HTTP status (0 when the fetch never completed).
      url          final URL after redirects ('' when the fetch never completed).
      text         response body as text.
      headers      response headers, lowercase keys ({} when the fetch never
                   completed; Set-Cookie and cross-origin headers are withheld
                   by the browser's fetch API).
      error        transport error string when ok is False, else None.
    """

    def __init__(self, ok, status_code, url, text, error=None, headers=None):
        self.ok = ok
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = headers or {}
        self.error = error

    def json(self):
        return json.loads(self.text)

    def __repr__(self):
        return (f"<FetchResponse ok={self.ok} status={self.status_code} "
                f"bytes={len(self.text)}>")


# page.content() serializes the ENTIRE DOM. A runaway page (infinite-scroll
# feed, a JS append loop) makes that a multi-GB string in BOTH the renderer
# and Python - and the csr settle poll re-serializes it every 300ms; observed
# OOM-killing contact pods at 5-12GB anon-rss. Slicing inside the page keeps
# every copy under the cap; 20MB is far above any legitimate page this stack
# parses (callers apply their own tighter caps downstream).
_MAX_CONTENT_CHARS = 20_000_000

_CAPPED_CONTENT_JS = """n => {
    const dt = document.doctype
        ? new XMLSerializer().serializeToString(document.doctype) + '\\n' : '';
    const el = document.documentElement;
    return dt + (el ? el.outerHTML.slice(0, n) : '');
}"""


def _capped_content(page):
    """content()-equivalent snapshot, sliced in-page to _MAX_CONTENT_CHARS.
    Raises like page.content() while a navigation is in flight."""
    return page.evaluate(_CAPPED_CONTENT_JS, _MAX_CONTENT_CHARS)


def _page_content(page):
    """_capped_content that returns None instead of raising while a navigation
    is in flight (Cloudflare's clearance reload destroys the execution
    context mid-poll)."""
    try:
        return _capped_content(page)
    except Exception:
        return None


def _settle_csr(page, budget_ms=SETTLE_BUDGET_MS):
    """networkidle fast-exit, then a content-length stability poll ONLY when
    the page still looks like an unrendered app shell — fast pages pay the
    old <=2s wait, slow SPAs get up to budget_ms (SETTLE_BUDGET_MS) to render.

    Every probe is deadline-checked. A page whose JS never stops working
    (SPA render loop, runaway allocation) makes each individual probe block
    for its own timeout: unchecked, a handful of them serialize past the
    driver's CALL_TIMEOUT and the caller sees a "wedged" driver rather than
    the partial page that was already renderable. The budget is the whole
    settle phase, not per probe."""
    if budget_ms <= 0:
        return   # a playwright timeout of 0 means "wait forever" - never pass it
    start = time.monotonic()

    def left_ms(cap):
        # Remaining settle budget, clamped to `cap`; 0 when spent.
        return max(0, min(cap, int(budget_ms - (time.monotonic() - start) * 1000)))

    try:
        page.wait_for_load_state("networkidle", timeout=min(SETTLE_IDLE_MS, budget_ms))
    except Exception:
        pass
    probe_ms = left_ms(1000)
    if probe_ms:
        try:
            innertxt = page.inner_text("body", timeout=probe_ms) or ""
            if len(innertxt) >= 200 and page.evaluate("document.querySelectorAll('a').length") >= 5:
                return
        except Exception:
            # body not queryable yet (blank shell, or the execution context was
            # destroyed by a mid-render navigation) - exactly the pages the
            # stability poll below exists for, so fall through to it.
            pass
    prev = None
    while left_ms(budget_ms):
        page.wait_for_timeout(min(300, left_ms(300)) or 1)
        html = _page_content(page)
        cur = len(html) if html is not None else None
        if cur is not None and cur == prev:
            return
        prev = cur


def click_turnstile(page):
    """Real mouse click on a Cloudflare Turnstile checkbox: the widget
    iframe's bounding box, x at 1/9 of the width (the checkbox), y at the
    middle, with a little jitter and a stepped mouse move. Returns True when
    a challenge frame was found and clicked. Shared home for the copies in
    envato/util.py, crunchbase/fetch.py, letterboxd/fetch.py and
    indeed/fetch.py (switching those over is a separate cleanup)."""
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


# Every substring cloudflare_verdict looks for. The challenge loop asks the
# page which of these it contains (_cf_markers) instead of shipping the whole
# document out of Chrome: on a non-Cloudflare page the detection then costs a
# title read and one tiny evaluate, whatever the page size.
_CF_PROBE_MARKERS = CF_CHALLENGE_HTML + ("challenge-platform", 'id="cf-error-details"') + CF_REFUSED_HTML
_CF_PROBE_JS = """(markers) => {
  const d = document.documentElement;
  if (!d) return '';
  const h = d.outerHTML;
  return markers.filter((m) => h.includes(m)).join('\\n');
}"""


def _cf_markers(page):
    """The _CF_PROBE_MARKERS present in the page, newline-joined (a string
    cloudflare_verdict reads like html); None while a navigation is in
    flight."""
    try:
        return page.evaluate(_CF_PROBE_JS, list(_CF_PROBE_MARKERS))
    except Exception:
        return None


def cloudflare_verdict(title, html, cf_mitigated=False):
    """'refused' / 'challenge' / None for one page snapshot (title
    lowercased, html raw; either may be None). None means the page is not
    Cloudflare's interstitial or block page - whatever else it is."""
    if title and "cloudflare" in title and any(w in title for w in CF_REFUSED_TITLE_WORDS):
        return "refused"
    if html and 'id="cf-error-details"' in html and any(m in html for m in CF_REFUSED_HTML):
        return "refused"
    if cf_mitigated:
        return "challenge"
    if html and any(m in html for m in CF_CHALLENGE_HTML):
        return "challenge"
    if (title and html and any(t in title for t in CF_CHALLENGE_TITLES)
            and "challenge-platform" in html):
        return "challenge"
    return None


def _settle_challenge(page, budget_ms=CHALLENGE_BUDGET_MS, cf_mitigated=False):
    """Cloudflare challenge-clear loop for nav(mode='blocked'), run on the
    owner thread. Returns (outcome, last_title, clicks) with outcome one of:
      None       not a Cloudflare page (no challenge, or another vendor's):
                 returned at once, nothing waited for or clicked
      'cleared'  the interstitial swapped itself out for the real page
      'timeout'  still challenged when budget_ms ran out
      'refused'  a terminal Cloudflare page (firewall block / access rule):
                 this egress is refused, clicking cannot help
    cf_mitigated is the goto response's `cf-mitigated: challenge` header.
    Turnstile is clicked when due (click_turnstile)."""
    def state():
        try:
            title = (page.title() or "").lower()
        except Exception:
            title = None            # mid-reload: execution context destroyed
        return title, _cf_markers(page)

    title, html = state()
    for _ in range(3):
        if html is not None:
            break
        # The document is swapping right after domcontentloaded (a redirect,
        # or Cloudflare's own reload): let it become readable.
        page.wait_for_timeout(300)
        title, html = state()
    verdict = cloudflare_verdict(title, html, cf_mitigated)
    if verdict == 'refused':
        return 'refused', title, 0
    if verdict is None:
        return None, title, 0
    start = _now()
    clicks, last_click = 0, None
    while True:
        elapsed_ms = (_now() - start) * 1000
        if elapsed_ms >= budget_ms:
            return 'timeout', title, clicks
        due = ((last_click is None and elapsed_ms >= CHALLENGE_FIRST_CLICK_MS)
               or (last_click is not None
                   and (_now() - last_click) * 1000 >= CHALLENGE_CLICK_EVERY_MS))
        if due and clicks < CHALLENGE_MAX_CLICKS:
            try:
                if click_turnstile(page):
                    clicks += 1
                    last_click = _now()
            except Exception:
                pass                # the frame navigated under the click
        page.wait_for_timeout(max(1, min(CHALLENGE_POLL_MS, int(budget_ms - elapsed_ms))))
        title, html = state()
        if html is None:
            continue                # reloading - usually the clearance itself
        verdict = cloudflare_verdict(title, html)
        if verdict == 'refused':
            return 'refused', title, clicks   # terminal verdict mid-loop
        if verdict is None:
            # Cleared - give the real page a moment to finish loading.
            try:
                page.wait_for_load_state("networkidle", timeout=1000)
            except Exception:
                pass
            return 'cleared', title, clicks


# Chrome's OWN background services, sent direct (proxy bypass) by every driver:
# none of it is page traffic, no site can observe it, and on a throwaway
# profile every launch repeats it through the paid residential exit -
# optimizationguide model downloads alone were ~5.6 MB per request and 10 GB of
# the Sept 2026 residential-proxy usage (spikes of 4-6 GB on a single day).
# Playwright's --disable-background-networking / OptimizationHints switches do
# not stop these, hence the bypass. PATCHRIGHT_DIRECT_HOSTS="" restores the
# everything-through-the-proxy behavior.
CHROME_BACKGROUND_HOSTS = (
    "optimizationguide-pa.googleapis.com",   # on-device model / hints downloads
    "*.gvt1.com",                            # component + model payloads (edgedl, dl)
    "update.googleapis.com",                 # component updater
    "safebrowsing.googleapis.com",           # Safe Browsing list updates
    "mtalk.google.com",                      # GCM push-messaging keepalive
    "android.clients.google.com",            # GCM registration / check-in
    "clients2.google.com",                   # extension / component update checks
    "clientservices.googleapis.com",         # field-trial (variations) seed
    "content-autofill.googleapis.com",       # autofill type predictions
)
_DEFAULT_DIRECT_HOSTS = tuple(
    h.strip() for h in os.environ.get(
        "PATCHRIGHT_DIRECT_HOSTS", ",".join(CHROME_BACKGROUND_HOSTS)).split(",")
    if h.strip())


def _proxy_dict(proxy_url, direct_hosts=()):
    """http://user:pass@host:port -> playwright proxy dict. direct_hosts
    (Chrome proxy-bypass patterns: "host", "*.domain") connect from the
    machine's own IP instead of the proxy."""
    u = urlparse(proxy_url)
    proxy = {
        "server": f"http://{u.hostname}:{u.port}",
        "username": u.username,
        "password": u.password,
    }
    if direct_hosts:
        proxy["bypass"] = ",".join(direct_hosts)
    return proxy


# The URL patterns aborted through the single abort-only route (block_resources
# types with a URL shape + block_hosts globs) live in url_blocking.py, shared
# with camoufox_driver. "image" and "font" are Chrome flags instead, see
# PatchrightDriver.__init__.


def _in_container():
    """True inside docker/compose or a Kubernetes pod (botasaurus's IS_DOCKER
    plus the k8s service env, which containerd-based GKE nodes always set)."""
    return bool(IS_DOCKER or IS_IN_KUBERNETES)


def _proxy_label(proxy_url):
    """"own" for no proxy, else the proxy's port (the sticky-exit id)."""
    if not proxy_url:
        return "own"
    try:
        from urllib.parse import urlsplit
        return f"p{urlsplit(proxy_url).port}"
    except Exception:
        return "proxy"


def resolve_identity(proxy_url, locale=None, timezone=None, geoip_timezone=None, lookup=None):
    """What the browser announces for its exit -> (ExitProfile, locale,
    languages, timezone). The exit's language and clock come from the geo
    lookup of the exit IP when `geoip_timezone` is True and one succeeds
    (the truth about the IP; `lookup` defaults to config.exit_geo), else
    from the proxy URL's country tag (egress.exit_profile). Locale: an
    explicit `locale` (coupang ko-KR) beats the exit, PATCHRIGHT_LANG is the
    fallback when nothing is derived. Zone: the lookup's, else a string
    `geoip_timezone` / the explicit `timezone`, else the static zone of a
    single-zone exit country (egress.TIMEZONE), else None = the system's."""
    from egress import exit_profile, languages_for
    geo = None
    if isinstance(geoip_timezone, str) and geoip_timezone:
        timezone = geoip_timezone            # a zone given as-is, no lookup
    elif geoip_timezone and proxy_url:
        if lookup is None:
            import config
            lookup = config.exit_geo
        geo = lookup(proxy_url)              # sticky proxies only (see config)
        if geo:
            print(f"patchright: exit {geo.get('countryCode')}/{geo.get('regionName')} "
                  f"({geo.get('query')}) -> timezone {geo['timezone']}")
        else:
            print("patchright: exit geo lookup failed; deriving from the proxy URL")
    exit = exit_profile(proxy_url, geo=geo)
    timezone = (geo or {}).get("timezone") or timezone or exit.timezone or None
    if locale:
        languages = languages_for(locale)
    else:
        locale, languages = exit.locale, exit.languages
    if not locale:
        locale = os.environ.get("PATCHRIGHT_LANG") or None
        languages = languages_for(locale)
    return exit, locale, languages, timezone


# ---- the host's native identity ------------------------------------------------
# What a plain Chrome on this machine announces with no flags at all: its
# languages and its clock. A browser whose exit wants the same identity is
# launched exactly like that - no --lang / --accept-lang, no TZ, no Windows
# timezone_id emulation (a CDP override, itself a tell) - and only what
# differs is overridden (owner rule 2026-10-30; the boxes' OS clocks and
# locales are set to their own IP's place by
# browser_worker/setup/set-native-identity.*). Probed once per process, kept
# in memory only (a deploy or restart probes again).
_NATIVE_JS = ("document.write('@@'+JSON.stringify({language:navigator.language,"
              "languages:navigator.languages,"
              "timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})+'@@')")
_NATIVE_PAGE = "data:text/html,<script>" + _NATIVE_JS + "</script>"
_NATIVE_RE = re.compile(r"@@(\{.*?\})@@")
_native_lock = threading.Lock()
_native_state = {"probed": False, "identity": None}


def parse_native(dom):
    """The probe page's output -> {locale, languages, accept_language,
    timezone}, or None when it is not there."""
    from egress import accept_header
    m = _NATIVE_RE.search(dom or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return None
    langs = [t for t in (d.get("languages") or []) if t] or ([d["language"]] if d.get("language") else [])
    if not langs or not d.get("timezone"):
        return None
    return {"locale": langs[0], "languages": ",".join(langs),
            "accept_language": accept_header(langs), "timezone": d["timezone"]}


def _probe_native(timeout=30):
    """Run this host's Chrome headless with no locale / timezone flags in
    this process's environment and read what it announces."""
    import subprocess
    exes = _chrome_candidates()
    if not exes:
        raise RuntimeError("no Chrome executable on this host")
    profile = tempfile.mkdtemp(prefix="native-probe-")
    try:
        cmd = [exes[0], "--headless=new", "--disable-gpu", "--no-first-run",
               f"--user-data-dir={profile}", "--dump-dom", _NATIVE_PAGE]
        if get_os() == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.insert(1, "--no-sandbox")
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        native = parse_native(out.stdout.decode("utf-8", "replace"))
        if not native:
            raise RuntimeError(f"no identity in the probe output (exit {out.returncode}): "
                               f"{out.stderr.decode('utf-8', 'replace')[-200:]!r}")
        native["chrome"] = exes[0]
        return native
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def native_identity(force=False, probe=None):
    """This host's native identity ({locale, languages, accept_language,
    timezone, chrome}), or None when unknown (probe failed, or
    PATCHRIGHT_NATIVE=0) - then every derived value is overridden, as
    before 2026-10-30. Probed on the first call of the process and kept in
    memory; `force` probes again, `probe` replaces the Chrome run (tests)."""
    if os.environ.get("PATCHRIGHT_NATIVE", "1") == "0":
        return None
    with _native_lock:
        if _native_state["probed"] and not force:
            return _native_state["identity"]
        try:
            ident = (probe or _probe_native)()
            print(f"patchright: native identity {ident['languages']} / {ident['timezone']}")
        except Exception as e:
            print(f"patchright: native identity probe failed ({type(e).__name__}: {e}); "
                  "overriding every derived language / clock")
            ident = None
        _native_state.update(probed=True, identity=ident)
        return ident


def native_cached():
    """The native identity if one was probed in this process (for /health;
    never launches anything)."""
    return _native_state["identity"]


def _lang_list(value):
    return [t.strip().lower() for t in (value or "").split(",") if t.strip()]


def overrides_for(native, languages, timezone):
    """What must be overridden for the browser to announce `languages` and
    `timezone`: {"lang", "tz"} minus whatever the host already announces
    natively. Nothing to announce (None) is never an override; an unknown
    native identity overrides everything there is. Zones compare by IANA
    name: the boxes are always in the US, where Chrome (ICU) and the geo
    lookup use the same canonical names (America/Chicago, America/New_York,
    ...), so no alias handling is needed."""
    out = set()
    if languages and (not native or _lang_list(languages) != _lang_list(native.get("languages"))):
        out.add("lang")
    if timezone and (not native or timezone != native.get("timezone")):
        out.add("tz")
    return out


def lang_args(locale, languages):
    """The Chrome switches that pin the language: --lang (UI, ICU) and
    --accept-lang (navigator.language(s) and Accept-Language; Linux Chrome
    takes --lang for the UI only: probe 2026-10-28 on the Ubuntu box,
    --lang=ko-KR alone -> "en-US,en", with --accept-lang ->
    "ko-KR,ko,en-US,en"). Nothing when no locale is known."""
    args = [f"--lang={locale}"] if locale else []
    if locale and languages:
        args.append(f"--accept-lang={languages}")
    return args


def fingerprint_for(generator, languages, bf_os):
    """A browserforge fingerprint whose languages are `languages` (the same
    list Chrome announces through --accept-lang): the full list first, its
    bare language on a dataset miss, and a loud failure instead of a
    fingerprint that contradicts the headers. `languages` None = no locale
    constraint (nothing to contradict)."""
    error = None
    for langs in dict.fromkeys((languages, (languages or "").split(",")[0].split("-")[0] or None)):
        try:
            return generator.generate(browser="chrome", os=bf_os, device="desktop",
                                      **({"locale": langs} if langs else {}))
        except Exception as e:
            error = e
    raise RuntimeError(f"browserforge has no fingerprint for languages {languages!r}: {error}")


def _navigator_languages(locale, accept=None):
    """navigator.languages / --accept-lang for a locale: the full q-stripped
    list ("pl-PL,pl,en-US,en"), never a one-tag list (egress.languages_for
    is the one implementation; kept here for the existing callers)."""
    from egress import languages_for
    return languages_for(locale, accept)


class PatchrightDriver:
    def __init__(self, proxy_url, headless=False, block_resources=None,
                 reset_on_release=False, browserforge=False, timezone=None,
                 channel=None, executable=None, direct_hosts=None,
                 geoip_timezone=None, block_hosts=None, extra_args=None,
                 locale=None):
        self._headless = bool(headless)
        # extra_args: extra Chrome switches for this driver (browser_worker
        # recipes: e.g. "--disable-http2" for Google hosts through a proxy,
        # whose h2 tunnel never answers on the Contabo box, 2026-10-22).
        self._extra_args = list(extra_args or ())
        # direct_hosts: PER-DRIVER hosts that skip the proxy, on top of
        # Chrome's background services (CHROME_BACKGROUND_HOSTS). For a
        # site's static CDN (images / video / JS bundles) when the page must
        # still load them: the bytes are fetched from the machine's own IP,
        # so nothing is blocked and the DOM is unchanged. NEVER list the
        # site's own host or its anti-bot hosts here - those must keep seeing
        # the residential exit.
        self._direct_hosts = tuple(dict.fromkeys(
            _DEFAULT_DIRECT_HOSTS + tuple(direct_hosts or ())))
        # channel / executable: PER-DRIVER browser choice so pools with
        # different needs share one process (2026-09-08: G2's DataDome only
        # clears on Opera, Shein is proven on Chrome). None keeps the
        # process-wide default (real Chrome; PATCHRIGHT_CHANNEL on a host);
        # "" opts out of channels (explicit executable, else the Opera > Brave
        # probe, else bundled Chromium); "chrome"/"msedge" pick a channel.
        self._channel = channel
        self._executable = executable or None
        # browserforge / timezone are PER-DRIVER browser-identity knobs set by
        # the owning scraper (pool settings in config.CHROME_POOLS, or the
        # harvest call in <site>/fetch.py) — never process-wide env, so
        # scrapers with different anti-bot needs coexist in one process.
        # timezone: the IANA zone the browser's clock should show for scrapers
        # whose proxy exit country differs from the container TZ; None keeps
        # the system TZ. geoip_timezone is the pool knob of the same name for
        # ad-hoc drivers: True looks the proxy's exit IP up before launch and
        # uses its zone, a string is used as-is (config.resolve_timezone).
        # locale: the browser's UI language / navigator.language /
        # Accept-Language ("ko-KR"); None derives it from the exit
        # (egress.exit_profile: the geo lookup's country when one was done,
        # else the proxy URL's country tag), else PATCHRIGHT_LANG, else the
        # browser's own default. The timezone follows the same profile when
        # no lookup / explicit zone decided it (egress.TIMEZONE for
        # single-zone countries), so a browser never shows a French language
        # on a US clock because one lookup failed.
        # NOT applied through Playwright emulation (timezone_id / locale,
        # i.e. CDP Emulation.setTimezoneOverride / setLocaleOverride) where
        # the OS allows: both go to the Chrome PROCESS - TZ in its
        # environment and --lang on its command line - see _main (only
        # Windows keeps timezone_id, because its Chrome ignores TZ; a Mac
        # run keeps the Mac's language). Coupang's Akamai A/B
        # (2026-10-24, KR residential, fresh exit each run):
        # the same Chrome with the CDP overrides passed 4 of 34 searches
        # before "Access Denied"; with TZ/--lang on the process it served
        # 16/16 navigations on a good exit, the same as a Chrome launched
        # by hand - the override is the tell, not the automation.
        self._browserforge = bool(browserforge)
        self._exit, self._locale, self._languages, self._timezone = resolve_identity(
            proxy_url, locale=locale, timezone=timezone, geoip_timezone=geoip_timezone)
        # Override only what the host does not already announce natively;
        # what is not overridden IS the native value (browserforge and the
        # identity's Accept-Language then carry the native one).
        self._native = native = native_identity()
        self._overrides = overrides_for(native, self._languages, self._timezone)
        if native and "lang" not in self._overrides:
            self._locale, self._languages = native["locale"], native["languages"]
        if native and "tz" not in self._overrides:
            self._timezone = native["timezone"]
        from egress import accept_header
        self._accept_language = accept_header(self._languages)   # what Chrome sends for that list
        print(f"patchright: exit={self._exit.country or 'none'}({self._exit.source or '-'}) "
              f"locale={self._locale or '-'} languages={self._languages or '-'} "
              f"tz={self._timezone or 'system'} proxy={_proxy_label(proxy_url)} "
              f"native={(native or {}).get('languages', '?')}/{(native or {}).get('timezone', '?')} "
              f"overrides={','.join(sorted(self._overrides)) or 'none'}")
        block = frozenset(block_resources or ())
        # "image" and "font" in block_resources are honored at the Chrome
        # level (--blink-settings=imagesEnabled=false, --disable-remote-fonts)
        # instead of route interception: those requests are then never issued,
        # so no bandwidth is spent and - unlike aborting them via a route,
        # which wedged some SPAs into stalling every later page op on the
        # driver - no load-error paths ever fire. Detectable by WAFs
        # (naturalWidth 0, missing web fonts), so only pools that never face
        # anti-bot sites should list them.
        self._block_images = "image" in block
        self._block_fonts = "font" in block
        # The remaining types (stylesheet/media) and block_hosts (host globs
        # of trackers/CDNs the page does not need, e.g. "*.hotjar.io") are
        # aborted by ONE URL-pattern route - see url_blocking. JS/XHR/document
        # on every other host always load.
        from url_blocking import compile_block_re
        self._block_url_re = compile_block_re(block, block_hosts)
        # reset_on_release: chrome_manager parks this driver on about:blank
        # when it returns to the pool, so an idle driver never keeps a huge
        # rendered page (and its renderer memory) alive.
        self.reset_on_release = bool(reset_on_release)
        if not self._headless:
            _ensure_display()
        self._jobs = queue.Queue()
        self._started = threading.Event()
        self._start_error = None
        self._closed = False
        self._profile_dir = tempfile.mkdtemp(prefix="patchright-profile-")
        # OS processes backing this browser, recorded by pid-diff around the
        # launch. Only the spawn window (snapshot -> first page up -> diff)
        # is serialized (launch_lock.LAUNCH_LOCK, shared with camoufox); the
        # identity lookups above run concurrently with other launches.
        self._procs = []
        from launch_lock import LAUNCH_LOCK
        with LAUNCH_LOCK:
            pre_launch = {p.pid for p in psutil.Process().children()}
            self._thread = threading.Thread(
                target=self._main,
                args=(_proxy_dict(proxy_url, self._direct_hosts) if proxy_url else None,),
                daemon=True, name="patchright-driver")
            self._thread.start()
            started = self._started.wait(STARTUP_TIMEOUT)
            self._procs = _children_since(pre_launch)
            if not started or self._start_error is not None:
                # Hung or failed launch: the owner thread may be stuck inside the
                # launch with processes already spawned — reap everything ourselves,
                # re-diffing for processes that appeared after the snapshot above.
                self.close()
                self._procs = _children_since(pre_launch)
                self._force_kill()
                if not started:
                    raise RuntimeError(f"patchright startup timed out after {STARTUP_TIMEOUT}s")
                raise RuntimeError(f"patchright startup failed: {self._start_error}")
        with _drivers_lock:
            _drivers.add(self)

    # ---- owner thread ---------------------------------------------------
    def _main(self, proxy):
        pw = None
        browser = None
        page = None
        try:
            pw = sync_playwright().start()
            # launch_persistent_context + headed + default viewport (NEVER
            # no_viewport=True: its window geometry is a DataDome tell) —
            # (History) Aug 22 2026: G2's DataDome device check
            # started hard-blocking real Chrome 151+ from every IP. Brave
            # passes the same check consistently (probe: PASS 4/4 where
            # bundled Chromium got interstitial'd on a flagged IP), so use it
            # when installed; PATCHRIGHT_EXECUTABLE overrides, else patchright's
            # bundled patched Chromium. Locale and timezone follow the exit
            # (resolve_identity) and reach the Chrome process below.
            # Aug 24 2026: during a G2 DataDome escalation that hard-blocked
            # chrome/Brave/Edge from every fresh residential IP, OPERA +
            # browserforge cleared 5/7 (chrome/Brave/Edge 0/N) — its Chromium
            # fork carries a distinct-enough fingerprint to slip the device
            # check. That Opera > Brave > bundled-Chromium order is what the
            # executable probe below implements — but ONLY when the channel is
            # explicitly opted out (PATCHRIGHT_CHANNEL=""), see the next note.
            #
            # The channel is real Chrome BY DEFAULT, everywhere. In a container
            # that is decided here rather than by deployment env: the
            # botasaurus:chrome-151 image ships google-chrome but NO
            # ms-playwright cache, so patchright's bundled Chromium cannot
            # launch there at all ("Executable doesn't exist ...
            # ms-playwright/chromium"). On a host it is the default too, so a
            # local probe/harvest never falls through to whatever Opera/Brave
            # install the executable probe finds (Sept 5 2026: an unset
            # PATCHRIGHT_CHANNEL opened Opera on the Mac). PATCHRIGHT_CHANNEL
            # overrides the channel on a host; PATCHRIGHT_CHANNEL="" (empty)
            # opts out of channels entirely for the Opera/Brave/bundled
            # experiments (PATCHRIGHT_EXECUTABLE, else the probe list, else
            # bundled Chromium).
            if self._channel is not None:
                channel = self._channel
            else:
                channel = "chrome" if _in_container() else os.environ.get("PATCHRIGHT_CHANNEL", "chrome")
            local = os.environ.get("LOCALAPPDATA", "")
            exe = self._executable or (None if channel else (os.environ.get("PATCHRIGHT_EXECUTABLE") or next(
                (p for p in (
                    "/Applications/Opera.app/Contents/MacOS/Opera",
                    "/usr/bin/opera",
                    os.path.join(local, "Programs", "Opera", "opera.exe"),
                    os.path.join(local, "Programs", "Opera", "launcher.exe"),
                    r"C:\Program Files\Opera\opera.exe",
                    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                    "/usr/bin/brave-browser",
                    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                ) if p and os.path.exists(p)), None)))
            if exe:
                channel = ""   # an explicit binary never combines with a channel
            # Locale and timezone go to the Chrome PROCESS wherever the OS
            # lets them (see __init__): --lang pins the UI language and
            # --accept-lang pins navigator.language(s) and Accept-Language,
            # on every OS (macOS Chrome ignores --lang for its UI only, from
            # AppleLanguages, and still honours --accept-lang: the FR Shein
            # proof of 2026-10-28 ran on a Mac). TZ in the process environment
            # pins Intl / Date on Linux and macOS. Windows Chrome ignores TZ
            # (ICU reads the system zone), so Windows alone keeps the
            # Playwright timezone_id override: on the Windows box it did not
            # raise Coupang blocking (2026-10-28, 12/15 pages with it vs 19/30
            # on the box clock). The Playwright `locale` override is never
            # used: it got Coupang searches 0/6 refused vs 6/6 without (it
            # shrinks navigator.languages to one tag). Verified on the Mac:
            # TZ=Europe/London -> Intl Europe/London, offset 0.
            launch_args = lang_args(self._locale, self._languages) if "lang" in self._overrides else []
            launch_env, emulate = None, {}
            if self._timezone and "tz" in self._overrides:
                if get_os() == "windows":
                    emulate["timezone_id"] = self._timezone
                else:
                    launch_env = dict(os.environ, TZ=self._timezone)
            if self._block_images:
                launch_args.append("--blink-settings=imagesEnabled=false")
            if self._block_fonts:
                launch_args.append("--disable-remote-fonts")
            if proxy:
                # Behind a proxy, WebRTC's STUN gathering still goes out the
                # machine's own interface and hands the page its real public
                # IPv4 + IPv6 as srflx candidates (seen on the Contabo box,
                # 2026-10-22). Google's reCAPTCHA already challenges that IP,
                # so every token minted through a residential exit scored
                # 0.0-0.3 while the same flow from the GKE pod scored fine.
                # disable_non_proxied_udp keeps WebRTC inside the proxy.
                launch_args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
            launch_args.extend(self._extra_args)
            browser = pw.chromium.launch_persistent_context(
                user_data_dir=self._profile_dir,
                headless=self._headless,
                proxy=proxy,
                **({"args": launch_args} if launch_args else {}),
                **({"env": launch_env} if launch_env else {}),
                **emulate,
                **({"channel": channel,
                    "no_viewport":True,
                    } if channel else {}),
                **({"executable_path": exe} if exe else {}),
            )
            # browserforge=True: overlay a fresh, statistically-real
            # browserforge fingerprint (navigator/screen/WebGL/fonts) per
            # driver via an init script — opted into PER SCRAPER (g2 pool,
            # etsy/skyscanner harvests) where DataDome/PerimeterX challenge the
            # bare Chrome fingerprint, so each driver looks like a distinct
            # real device instead of N identical ones. Patchright delivers
            # init scripts by rewriting the document response (a route on
            # every request), so they DO run in the page's MAIN world, which
            # is what the site's own anti-bot JS reads. Do not "verify" the
            # overlay with page.evaluate(): patchright's evaluate defaults to
            # isolated_context=True, a separate world where none of the
            # overrides exist — that is how the Sept 8 2026 "browserforge is
            # a no-op" reading came about (main-world dump 2026-09-23:
            # UA/hw/screen/WebGL all swapped). The fingerprint OS must
            # match the real platform (DataDome cross-checks claimed platform
            # against rendering/font tells): linux in containers, macos on the
            # Mac host; PATCHRIGHT_BROWSERFORGE_OS overrides (a host-platform
            # knob, so that one stays env).
            if self._browserforge:
                from browserforge.fingerprints import FingerprintGenerator
                from browserforge.injectors.utils import InjectFunction
                bf_os_map = {"windows": "windows", "mac": "macos", "linux": "linux"}
                bf_os = (os.environ.get("PATCHRIGHT_BROWSERFORGE_OS")
                         or bf_os_map.get(get_os(), "linux"))
                # The fingerprint's languages are the same list Chrome
                # announces (--accept-lang above), on every OS: an overlay
                # that says en-US on a browser whose Accept-Language says
                # de-DE is the tell this exists to avoid. A locale absent
                # from browserforge's dataset is retried with its bare
                # language; if that fails too the launch fails loudly
                # instead of injecting a fingerprint that contradicts the
                # headers.
                fp = fingerprint_for(FingerprintGenerator(), self._languages, bf_os)
                browser.add_init_script(InjectFunction(fp))
            if self._block_url_re is not None:
                # Route ONLY the URLs to abort (abort is the sole action, so
                # there is no continue/fallback path at all). The old handler
                # routed "**/*" and decided per request in Python, so EVERY
                # request round-tripped over the playwright pipe; a page that
                # keeps issuing requests (SPA polling, analytics beacons)
                # saturated that pipe and starved the driver's own page ops
                # until they hit CALL_TIMEOUT - the "page wedged" timeouts,
                # with the backed-up requests showing up as multi-GB renderer
                # memory. (fallback() vs continue_() never mattered: an
                # unhandled fallback is continued from Python as well.)
                browser.route(self._block_url_re, lambda route: route.abort())
            page = browser.new_page()
            page.set_default_timeout(30000)
        except Exception as e:
            self._start_error = e
            self._started.set()
            for closer in (browser, pw):
                if closer is not None:
                    try:
                        closer.close() if closer is browser else closer.stop()
                    except Exception:
                        pass
            return
        self._started.set()
        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, done, box = job
            try:
                box["result"] = fn(page)
            except BaseException as e:
                box["error"] = e
            done.set()
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

    # ---- calling threads ------------------------------------------------
    def call(self, fn, timeout=CALL_TIMEOUT):
        """Run fn(page) on the owner thread; raise whatever it raised.
        A timeout means the page is wedged — the driver is unusable and the
        caller/pool should close it (the wedged op keeps the daemon thread)."""
        if self._closed:
            raise RuntimeError("patchright driver is closed")
        done = threading.Event()
        box = {}
        self._jobs.put((fn, done, box))
        if not done.wait(timeout):
            raise TimeoutError(f"patchright op timed out after {timeout}s (page wedged)")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def goto(self, url, referer=None):
        return self.call(lambda page: page.goto(
            url, referer=referer, wait_until="domcontentloaded",
            timeout=GOTO_TIMEOUT_MS))

    def nav(self, url, referer=None, mode='csr', challenge_markers=()):
        """Navigate + mode-aware settle + snapshot as ONE owner-thread op.
        Returns plain data {'url','status','ok','headers','html','challenge',
        'challenge_title','challenge_clicks'} that is safe to hand across
        threads (goto()'s playwright Response is greenlet-bound to the owner
        thread and must never leave it). status/headers are None/{} when
        playwright returns no response (same-document navigation). Raises on
        navigation failure (net::ERR_*, goto timeout) exactly like goto().

        mode picks the settle strategy (budgets are module constants):
          'csr'      networkidle capped at SETTLE_IDLE_MS; if the body is
                     still an empty app shell afterwards, a content-length
                     stability poll runs up to SETTLE_BUDGET_MS.
          'blocked'  Cloudflare pages only (cloudflare_verdict): poll up to
                     CHALLENGE_BUDGET_MS, clicking the Turnstile checkbox
                     when shown, then the csr settle on the cleared page.
                     Every other page - another vendor's interstitial, a
                     plain 403, a normal page - gets exactly the csr settle.
                     'challenge' reports None / 'cleared' / 'timeout' /
                     'refused' (see _settle_challenge); the html snapshot is
                     taken regardless so callers can inspect an interstitial.
                     challenge_markers is accepted for the existing callers
                     and no longer consulted.
        The whole op is budgeted under NAV_OP_BUDGET_MS (< CALL_TIMEOUT):
        a slow goto shortens the challenge loop and the settle after it.
        status/ok/headers come from the LAST main-frame document response
        seen — a challenge that clears during the settle reports the real
        page's status, not the interstitial's 403."""
        def _do(page):
            docs = []
            op_start = time.monotonic()

            def left_ms():
                return max(0, NAV_OP_BUDGET_MS - int((time.monotonic() - op_start) * 1000))

            def _on_response(resp):
                try:
                    if (resp.request.resource_type == "document"
                            and resp.frame == page.main_frame):
                        docs.append(resp)
                except Exception:
                    pass

            page.on("response", _on_response)
            challenge, challenge_title, clicks = None, None, 0
            try:
                resp = page.goto(url, referer=referer,
                                 wait_until="domcontentloaded",
                                 timeout=GOTO_TIMEOUT_MS)
                if mode == 'blocked':
                    try:
                        cf_mitigated = (resp is not None and "challenge" in
                                        (resp.headers.get("cf-mitigated") or "").lower())
                    except Exception:
                        cf_mitigated = False
                    challenge, challenge_title, clicks = _settle_challenge(
                        page,
                        budget_ms=min(CHALLENGE_BUDGET_MS,
                                      max(0, left_ms() - NAV_SETTLE_RESERVE_MS)),
                        cf_mitigated=cf_mitigated)
                    if challenge is None:
                        _settle_csr(page)           # not Cloudflare: unchanged path
                    elif challenge == 'cleared':
                        # The real page: the usual csr settle (an SPA behind
                        # Cloudflare still has to render - tesla.com).
                        _settle_csr(page, budget_ms=min(SETTLE_BUDGET_MS, left_ms()))
                else:
                    _settle_csr(page)
                final = docs[-1] if docs else resp
            finally:
                page.remove_listener("response", _on_response)
            try:
                html = _capped_content(page)
            except Exception:
                # Snapshot raced a navigation (challenge still reloading at
                # the deadline): let it land, then read once more.
                page.wait_for_timeout(500)
                html = _capped_content(page)
            return {"url": page.url,
                    "status": final.status if final is not None else None,
                    "ok": final.ok if final is not None else True,
                    "headers": dict(final.headers) if final is not None else {},
                    "html": html,
                    "challenge": challenge,
                    "challenge_title": (challenge_title or "")[:80],
                    "challenge_clicks": clicks}
        return self.call(_do)

    def evaluate(self, js, arg=None, main_world=False):
        """Run js in the page. Patchright evaluates in an ISOLATED world by
        default (its stealth: the page's scripts cannot see our probes), which
        shares the DOM and cookies but NOT the page's JS globals — a site's own
        `window.<state>` blob reads as undefined there. main_world=True runs
        in the page's main world instead (aliexpress search reads
        window._dida_config_ that way)."""
        return self.call(lambda page: page.evaluate(
            js, arg, isolated_context=not main_world))

    def title(self):
        return self.call(lambda page: page.title())

    def cookies(self):
        """Every cookie in the browser context (all hosts) as playwright
        cookie dicts — e.g. the aliexpress `_m_h5_tk` mtop signing token."""
        return self.call(lambda page: page.context.cookies())

    def content(self):
        return self.call(_capped_content)

    def blank(self):
        """Park on about:blank, releasing the current page's renderer memory."""
        self.call(lambda page: page.goto("about:blank"))

    def clear_cookies(self, url=None):
        """Drop the cookies for url's host and its parent domains (every
        cookie when url is None). Used after a terminal Cloudflare refusal so
        a half-minted cf_clearance is not replayed on the next crawl of that
        host. The domain filter needs playwright >= 1.43; older builds clear
        the whole jar (other hosts just re-earn their clearance)."""
        host = (urlparse(url).hostname or "").lower() if url else ""
        labels = host.split(".") if host else []
        suffixes = [".".join(labels[i:]) for i in range(len(labels) - 1)]
        pattern = re.compile(r"^\.?(" + "|".join(re.escape(s) for s in suffixes) + r")$") \
            if suffixes else None

        def _do(page):
            if pattern is not None:
                try:
                    page.context.clear_cookies(domain=pattern)
                    return
                except TypeError:
                    pass
            page.context.clear_cookies()
        self.call(_do)

    def content_frames(self):
        """HTML of every frame (main first, then iframes) as one owner-thread
        op. DataDome renders its block/captcha verdict inside a
        captcha-delivery.com iframe that page.content() (main frame only)
        never shows. Frames that are mid-navigation or unreadable yield ""."""
        def _do(page):
            out = []
            for f in page.frames:
                try:
                    out.append(f.evaluate(_CAPPED_CONTENT_JS,
                                          _MAX_CONTENT_CHARS) or "")
                except Exception:
                    out.append("")
            return out
        return self.call(_do)

    @property
    def current_url(self):
        return self.call(lambda page: page.url)

    def get(self, url, headers=None, referrer=None,
            timeout_ms=FETCH_TIMEOUT_MS, call_timeout=CALL_TIMEOUT):
        """requests-like GET run as an in-page fetch (inherits the page's
        cookies, Cloudflare clearance and TLS fingerprint). Returns a
        FetchResponse; never raises on HTTP/network errors — check .ok /
        .status_code. `call_timeout` is the hard owner-thread ceiling."""
        res = self.call(
            lambda page: page.evaluate(_FETCH_JS, {
                "url": url,
                "method": "GET",
                "headers": headers if headers is not None else DEFAULT_FETCH_HEADERS,
                "body": None,
                "referrer": referrer,
                "timeoutMs": timeout_ms,
            }),
            timeout=call_timeout,
        ) or {}
        return FetchResponse(
            ok=bool(res.get("ok")),
            status_code=int(res.get("status") or 0),
            url=res.get("url") or "",
            text=res.get("body") or "",
            error=res.get("error"),
            headers=res.get("headers") or {},
        )

    def request(self, method, url, headers=None, body=None, referrer=None,
                timeout_ms=FETCH_TIMEOUT_MS, call_timeout=CALL_TIMEOUT):
        """Any-method in-page fetch (POST/PUT with a body) with get()'s
        semantics: inherits the page's cookies and clearance, never raises on
        HTTP/network errors. `body` is sent as-is (a str; JSON-encode first).
        Added 2026-10-21 for the browser worker's fetch recipes (envato,
        crunchbase and glassdoor hand-rolled this before)."""
        res = self.call(
            lambda page: page.evaluate(_FETCH_JS, {
                "url": url,
                "method": (method or "GET").upper(),
                "headers": headers if headers is not None else DEFAULT_FETCH_HEADERS,
                "body": body,
                "referrer": referrer,
                "timeoutMs": timeout_ms,
            }),
            timeout=call_timeout,
        ) or {}
        return FetchResponse(
            ok=bool(res.get("ok")),
            status_code=int(res.get("status") or 0),
            url=res.get("url") or "",
            text=res.get("body") or "",
            error=res.get("error"),
            headers=res.get("headers") or {},
        )

    @property
    def browser_pid(self):
        """Root pid of the Chrome process tree, or None (browser_worker uses
        it to measure the tree's RSS for recycling)."""
        for p in self._procs:
            try:
                if _is_playwright_root(p):
                    return p.pid
            except psutil.Error:
                continue
        return self._procs[0].pid if self._procs else None

    def get_many(self, urls, headers=None, referrer=None,
                 timeout_ms=FETCH_TIMEOUT_MS, stagger_ms=FETCH_STAGGER_MS,
                 call_timeout=CALL_TIMEOUT):
        """Concurrent in-page GETs: one marshalled evaluate() runs every fetch
        via Promise.all, each launched i*stagger_ms apart — the browser fires
        them in parallel like a page loading its subresources. Returns a list
        of FetchResponse in input order; like get(), never raises on
        HTTP/network errors."""
        if not urls:
            return []
        results = self.call(
            lambda page: page.evaluate(_FETCH_MANY_JS, {
                "requests": [{
                    "url": url,
                    "method": "GET",
                    "headers": headers if headers is not None else DEFAULT_FETCH_HEADERS,
                    "body": None,
                    "referrer": referrer,
                } for url in urls],
                "timeoutMs": timeout_ms,
                "staggerMs": stagger_ms,
            }),
            timeout=call_timeout,
        ) or []
        results += [{}] * (len(urls) - len(results))  # bridge miss -> transport error
        return [FetchResponse(
            ok=bool(res.get("ok")),
            status_code=int(res.get("status") or 0),
            url=res.get("url") or "",
            text=res.get("body") or "",
            error=res.get("error"),
            headers=res.get("headers") or {},
        ) for res in results]

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._jobs.put(None)
        self._thread.join(timeout=30)
        # A wedged page op keeps the owner thread stuck inside Playwright: it
        # never reaches the sentinel and the join above times out — kill the
        # browser's process trees directly.
        self._force_kill()
        shutil.rmtree(self._profile_dir, ignore_errors=True)
        with _drivers_lock:
            _drivers.discard(self)

    def _force_kill(self):
        """SIGKILL whatever of this driver's OS processes survived a clean
        shutdown. No-op when the owner thread already shut down cleanly."""
        for proc in self._procs:
            _kill_tree(proc)


class TabbedPatchrightDriver(PatchrightDriver):
    """One Chrome, one persistent tab per key (a host) — the patchright port of
    camoufox_driver.TabbedCamoufoxDriver (chrome pool backend
    "patchright-tabbed", used by shein-intl). Tabs are created and used only on
    the owner thread; the pool's single-lease semantics serialize all access,
    so the plain dict needs no locking. Tabs share the browser context (proxy,
    timezone, the URL-pattern block route) and die with the browser on
    close() — no per-tab cleanup required.

    At most MAX_TABS tabs are kept: each holds a fully-loaded page that keeps
    running JS for the browser's lifetime. Opening a tab beyond the cap evicts
    the least-recently-used one; its host simply reseeds on its next request.
    The dict doubles as the LRU order (call_tab reinserts on use)."""

    MAX_TABS = 6

    def __init__(self, *args, **kwargs):
        self._tabs = {}  # key -> page, least-recently-used first
        super().__init__(*args, **kwargs)

    def has_tab(self, key):
        return key in self._tabs

    def open_tab(self, key):
        def _open(default_page):
            while len(self._tabs) >= self.MAX_TABS:
                victim, page = next(iter(self._tabs.items()))
                del self._tabs[victim]
                try:
                    page.close()
                except Exception:
                    pass
                print(f"patchright: evicted LRU tab {victim} "
                      f"({len(self._tabs)}/{self.MAX_TABS} tabs)")
            page = default_page.context.new_page()
            page.set_default_timeout(30000)
            self._tabs[key] = page
        self.call(_open)

    def close_tab(self, key):
        def _close(_default_page):
            page = self._tabs.pop(key, None)
            if page is not None:
                page.close()
        self.call(_close)

    def call_tab(self, key, fn, timeout=CALL_TIMEOUT):
        """Run fn(page) on the owner thread against the key's tab."""
        def _run(_default_page):
            page = self._tabs.pop(key)   # reinsert -> most-recently-used
            self._tabs[key] = page
            return fn(page)
        return self.call(_run, timeout)
