"""Browser lifecycle on the box: launch on demand, park, idle-close, recycle,
cap, and never keep anything the policy says to drop.

One `Lease` per live browser, keyed by the recipe's choice (proxy session,
or a caller `session` id). The worker starts with none. Rules (owner
decisions 2026-10-21):

- mint browsers are never kept: the recipe opens, mints, closes in-call
  (recipes of type cookie_mint do not use this registry at all)
- token and fetch browsers park on the recipe's park URL after every
  request and close after `idle_seconds` without one
- a browser is recycled (closed, relaunched on the next request) after
  `max_uses` requests, `max_age` seconds, or when its RSS passes
  `max_rss_mb`
- per-recipe `max_browsers` is a guaranteed share, elastic since 2026-11-04:
  a site goes above it only while the box has headroom and is under the
  ceiling; a site under its share that finds the box full takes an idle
  browser of a site above its share first; the reaper closes idle
  above-share browsers first when the host runs hot. A site at its share
  without headroom closes its own idle browser under another key to move.
- a per-box cap in two parts (since
  2026-11-03): a floor (MAX_BROWSERS_PER_BOX) always allowed, and above it
  up to a ceiling (MAX_BROWSERS_BURST) only while the host has headroom
  (headroom.HostSampler: trailing CPU under CPU_HEADROOM_PCT counting each
  launching/settling browser as a core, free memory over the reserve plus
  the recipe's max_rss_mb). Without headroom, or at the ceiling, a newcomer
  may still SWAP in for the least recently used idle browser on the box
  (the count does not rise, as under the old fixed cap); if nothing is idle
  the caller waits up to QUEUE_SECONDS, then gets BoxBusy (503). When the
  host runs hot (CPU_SHRINK_PCT, or memory under the reserve) the reaper
  closes idle browsers above the floor.
- on worker start, orphan Chromes from a crash are killed
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from . import settings
from .headroom import HostSampler


PARK_CHECK_TIMEOUT = 5   # s to read page.url before a park: a wedged page must not hold its lease longer


class BoxBusy(Exception):
    """No browser slot free within QUEUE_SECONDS; GKE answers fast."""


@dataclass
class Lease:
    recipe: str
    key: str                       # proxy session or caller session id
    driver: object
    proxy: Optional[str]
    created: float = field(default_factory=time.monotonic)   # reset when the launch completes (age / max_age)
    started: float = field(default_factory=time.monotonic)   # when the launch began; never reset (settling)
    last_used: float = field(default_factory=time.monotonic)
    uses: int = 0
    busy: bool = False
    parked: bool = False
    over_rss: bool = False         # set by the reaper's memory check (outside the lock)
    rss_mb: float = 0.0            # the reaper's last measurement (USS), read by /metrics

    @property
    def idle_for(self) -> float:
        return time.monotonic() - self.last_used

    @property
    def age(self) -> float:
        return time.monotonic() - self.created


class Registry:
    """All live token/fetch browsers on this box."""

    def __init__(self, sampler=None):
        self._lock = threading.Condition()
        self._leases: Dict[tuple, Lease] = {}      # (recipe, key) -> Lease
        self._reaper_started = False
        self._sampler = sampler or HostSampler()   # host CPU / memory for the cap above the floor
        self._share: Dict[str, int] = {}           # recipe -> its max_browsers as last admitted (guaranteed share)

    # ---- acquire / release ------------------------------------------------------

    def acquire(self, recipe, key: str, proxy: Optional[str], launch: Callable[[], object],
                warm: Optional[Callable[[object], None]] = None) -> Lease:
        """A browser for (recipe, key): reuse the parked one, or launch within
        the caps. `launch` returns a driver; `warm` runs once per launch
        (seed page etc.). The lease comes back marked busy; call release()."""
        name = recipe.name
        deadline = time.monotonic() + settings.QUEUE_SECONDS
        refusal = None                 # the box's reason from THIS call's last try (BoxBusy message)
        with self._lock:
            while True:
                lease = self._leases.get((name, key))
                if lease is not None and not lease.busy:
                    if self._stale(recipe, lease):
                        self._close_locked(lease)
                        lease = None   # its slot is free: admit now, not after a wait
                    else:
                        lease.busy = True
                        return lease
                if lease is None:
                    admitted, refusal = self._admit_locked(name, recipe)
                    if admitted:
                        break
                else:
                    refusal = None     # waiting for this key's own busy browser, not the box
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BoxBusy(f"{name}: no browser slot free in {settings.QUEUE_SECONDS}s"
                                  + _refusal_suffix(refusal))
                self._lock.wait(timeout=min(remaining, 1.0))
            # reserve the slot while launching outside the lock
            placeholder = Lease(name, key, None, proxy)
            placeholder.busy = True
            self._leases[(name, key)] = placeholder
        return self._launch_into(placeholder, name, key, launch, warm)

    def acquire_any(self, recipe, choices, warm=None) -> tuple:
        """A browser for ANY of `choices` [(key, proxy, launch)]: browsers the
        caller can use interchangeably (one site's sticky slots). The box,
        which sees every caller's work, picks instead of the caller guessing
        (until 2026-10-26 GKE named one slot per call and queued behind it
        while others sat idle; two relays pods could not see each other's
        calls on a shared slot):

          1. an idle one, the most recently used first (warm pages, fewer
             cold launches under light traffic);
          2. else the first not yet launched, within the caps (an idle
             browser of this recipe outside `choices` is closed to make room
             when the recipe is at its cap);
          3. else wait for whichever frees first, up to QUEUE_SECONDS.

        Returns (lease, index into choices), the lease marked busy."""
        name = recipe.name
        deadline = time.monotonic() + settings.QUEUE_SECONDS
        refusal = None
        with self._lock:
            while True:
                idle, absent = [], []
                for i, (key, _proxy, _launch) in enumerate(choices):
                    lease = self._leases.get((name, key))
                    if lease is None:
                        absent.append(i)
                    elif not lease.busy:
                        if self._stale(recipe, lease):
                            self._close_locked(lease)
                            absent.append(i)
                        else:
                            idle.append((lease.last_used, -i, i))
                if idle:
                    i = max(idle)[2]
                    lease = self._leases[(name, choices[i][0])]
                    lease.busy = True
                    return lease, i
                if absent:
                    admitted, refusal = self._admit_locked(name, recipe)
                    if admitted:
                        i = absent[0]
                        break
                else:
                    refusal = None     # every choice exists and is busy: not the box's doing
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BoxBusy(f"{name}: none of {len(choices)} browser slots free in "
                                  f"{settings.QUEUE_SECONDS}s" + _refusal_suffix(refusal))
                self._lock.wait(timeout=min(remaining, 1.0))
            key, proxy, launch = choices[i]
            placeholder = Lease(name, key, None, proxy)
            placeholder.busy = True
            self._leases[(name, key)] = placeholder
        return self._launch_into(placeholder, name, key, launch, warm), i

    def _launch_into(self, placeholder: Lease, name: str, key: str, launch, warm) -> Lease:
        """Launch (and warm) the browser for a reserved placeholder lease."""
        driver = None
        try:
            driver = launch()
            if warm is not None:
                warm(driver)
        except Exception:
            # a launch that failed half-way or a warm-up that refused the exit:
            # the Chrome must not outlive the lease (it did until 2026-10-22)
            if driver is not None:
                threading.Thread(target=_close_quietly, args=(driver,), daemon=True).start()
            with self._lock:
                self._leases.pop((name, key), None)
                self._lock.notify_all()
            raise
        with self._lock:
            placeholder.driver = driver
            placeholder.created = placeholder.last_used = time.monotonic()
            self._start_reaper_locked()
            return placeholder

    def release(self, lease: Lease, recipe, failed: bool = False) -> None:
        """Hand the browser back: park it (recipe.park_url) or close it when
        the recipe is done with it or a recycle limit was hit.

        The lease stays busy until the park navigation is done, so the next
        caller can never start on a page the park is about to navigate away
        (until 2026-10-23 busy was cleared first, then parked). A recipe with
        `park_in_background = True` parks on a worker thread, so the reply is
        sent without waiting for the park (~0.7-1.1 s through a proxy); a
        caller arriving meanwhile waits for it like for any busy browser."""
        with self._lock:
            lease.uses += 1
            lease.last_used = time.monotonic()
            if failed or self._stale(recipe, lease):
                lease.busy = False
                self._close_locked(lease)
                self._lock.notify_all()
                return
        park = getattr(recipe, "park_url", None)
        if not (park and getattr(recipe, "park", True)):
            self._free(lease)
            return
        if getattr(recipe, "park_in_background", False):
            threading.Thread(target=self._park_then_free, args=(lease, recipe, park),
                             name=f"park-{recipe.name}", daemon=True).start()
        else:
            self._park_then_free(lease, recipe, park)

    def _park_then_free(self, lease: Lease, recipe, park) -> None:
        try:
            if not _still_parked(lease.driver, park):
                recipe.park_page(lease.driver, park)
            lease.parked = True
        except Exception as e:
            print(f"{recipe.name}: park failed ({type(e).__name__}: {e}); closing")
            with self._lock:
                lease.busy = False
                self._close_locked(lease)
                self._lock.notify_all()
            return
        self._free(lease)

    def _free(self, lease: Lease) -> None:
        with self._lock:
            lease.busy = False
            self._lock.notify_all()

    # ---- limits ----------------------------------------------------------------

    def _stale(self, recipe, lease: Lease) -> bool:
        reason = self._stale_reason(recipe, lease)
        if reason:
            print(f"browser_worker: recycling {lease.recipe} browser ({reason})")
        return bool(reason)

    def _stale_reason(self, recipe, lease: Lease):
        """Cheap checks only: this runs under the lock on every lease handed
        out or given back. The memory cap is measured by the reaper outside
        the lock (_measure_memory) and lands here as `over_rss`: until
        2026-10-26 it was measured here, walking each Chrome's process tree
        (psutil USS, slow on Windows) while every other request waited for
        the lock — with ~170 Chrome processes the Windows worker stalled and
        stopped answering, /health included."""
        if lease.uses >= getattr(recipe, "max_uses", settings.MAX_USES):
            return f"{lease.uses} uses"
        if lease.age >= getattr(recipe, "max_age", settings.MAX_AGE_SECONDS):
            return f"age {lease.age:.0f}s"
        if lease.over_rss:
            return f"memory over {getattr(recipe, 'max_rss_mb', settings.MAX_RSS_MB)} MB"
        return None

    def _recipe_room_locked(self, name: str, recipe) -> bool:
        mine = sum(1 for (r, _k) in self._leases if r == name)
        return mine < getattr(recipe, "max_browsers", settings.MAX_BROWSERS_PER_RECIPE)

    def _admit_locked(self, name: str, recipe) -> tuple[bool, Optional[str]]:
        """One more browser of `recipe` for a key it has none for.

        Under its share (`max_browsers`, guaranteed): the box decides. Within
        the floor or with headroom the browser is launched; else the newcomer
        swaps in for an idle browser on the box, one of a site above its
        share first (_swap_idle_locked). A swap keeps the count, so it is
        allowed at any load, exactly what the fixed cap did when full; the
        reaper shrinks a hot box between requests.

        At or above its share (since 2026-11-04 the share is elastic): one
        more only while the box has headroom and is under the ceiling; else
        the site closes its own least recently used idle browser (under
        another key: an idle one for this key would have been reused) and
        takes that slot, as the fixed cap did.

        Returns (admitted, refusal): why not when the box or the headroom was
        the limit, None when admitted or when the site is at its share with
        nothing of its own idle. The reason is returned, not kept on the
        registry: a caller that times out must report ITS last refusal."""
        share = getattr(recipe, "max_browsers", settings.MAX_BROWSERS_PER_RECIPE)
        self._share[name] = share
        if self._recipe_room_locked(name, recipe):
            reason = self._box_refusal_locked(recipe)
            if reason is None:
                self._log_burst_locked(name)
                return True, None
            if self._swap_idle_locked(name, reason):
                return True, None
            return False, reason
        reason = self._burst_refusal_locked(recipe)
        if reason is None:
            mine = sum(1 for (r, _k) in self._leases if r == name)
            print(f"browser_worker: {name} above its share {share} ({mine + 1} open): "
                  f"{self._host_line_locked()}")
            return True, None
        mine_idle = [l for l in self._leases.values()
                     if l.recipe == name and not l.busy and l.driver is not None]
        if not mine_idle:
            return False, f"at its share {share}: {reason}"
        victim = min(mine_idle, key=lambda l: l.last_used)
        print(f"browser_worker: {name} at its share {share} ({reason}), closing idle {victim.key} "
              f"(idle {victim.idle_for:.0f}s) for another slot")
        self._close_locked(victim)
        return True, None

    def _burst_refusal_locked(self, recipe) -> Optional[str]:
        """Why a site may not go above its share right now, or None: the
        ceiling, else the host's headroom (the floor does not apply: above a
        share is a burst, and bursts are for an idle box)."""
        if len(self._leases) >= settings.MAX_BROWSERS_BURST:
            return f"at the ceiling {settings.MAX_BROWSERS_BURST}"
        return self._headroom_refusal_locked(recipe)

    def _over_share_locked(self, lease: Lease) -> bool:
        """Is this browser's site above its guaranteed share?"""
        mine = sum(1 for (r, _k) in self._leases if r == lease.recipe)
        return mine > self._share.get(lease.recipe, settings.MAX_BROWSERS_PER_RECIPE)

    def _headroom_refusal_locked(self, recipe) -> Optional[str]:
        """Why the host cannot take one more browser above the floor right
        now, or None. Reads the sampler's last numbers, never measures.
        Browsers still launching or just launched are counted as a core each
        (their CPU is not in the trailing average yet), so a burst of
        waiters is admitted one at a time as the sample catches up. Memory
        needs no such model: `available` shows a new Chrome within a second."""
        s = self._sampler.last()
        if s is None:
            return "no host sample"
        settling = self._settling_locked()
        predicted = s.cpu_pct + settling * settings.LAUNCH_CPU_PCT
        if predicted >= settings.CPU_HEADROOM_PCT:
            return f"cpu {s.cpu_pct:.0f}% + {settling} settling"
        need = getattr(recipe, "max_rss_mb", settings.MAX_RSS_MB) + settings.MEMORY_RESERVE_MB
        if s.free_mb < need:
            return f"free memory {s.free_mb:.0f} MB < {need} MB"
        return None

    def _box_refusal_locked(self, recipe) -> Optional[str]:
        """None when the box takes one more browser; else why not."""
        total = len(self._leases)
        if total < settings.MAX_BROWSERS_PER_BOX:
            return None                                   # the floor: always allowed
        if total >= settings.MAX_BROWSERS_BURST:
            return f"at the ceiling {settings.MAX_BROWSERS_BURST}"
        return self._headroom_refusal_locked(recipe)

    def _settling_locked(self) -> int:
        """Browsers whose CPU the trailing sample cannot show yet: launched
        within LAUNCH_SETTLE_SECONDS, counted from when the launch BEGAN. A
        longer launch (a Shein warm is 45-120 s) is real, measured load after
        that; until 2026-11-04 it counted as a core for its whole warm-up on
        top of being in the sample, so three warms blocked a burst at ~30%."""
        now = time.monotonic()
        return sum(1 for l in self._leases.values()
                   if now - l.started < settings.LAUNCH_SETTLE_SECONDS)

    def _hot_reason(self) -> Optional[str]:
        """Why the reaper should close idle browsers above the floor, or None."""
        s = self._sampler.last()
        if s is None:
            return None
        if s.cpu_pct >= settings.CPU_SHRINK_PCT:
            return f"cpu {s.cpu_pct:.0f}%"
        if s.free_mb < settings.MEMORY_RESERVE_MB:
            return f"free memory {s.free_mb:.0f} MB"
        return None

    def _host_line_locked(self) -> str:
        s = self._sampler.last()
        if s is None:
            return "no host sample"
        return (f"cpu {s.cpu_pct:.0f}% ({settings.HOST_SAMPLE_WINDOW} s), "
                f"{self._settling_locked()} settling, {s.free_mb:.0f} MB free")

    def _log_burst_locked(self, name: str) -> None:
        total = len(self._leases)
        if total < settings.MAX_BROWSERS_PER_BOX:
            return
        print(f"browser_worker: above the floor {total + 1}/{settings.MAX_BROWSERS_BURST}: "
              f"{self._host_line_locked()} -> launching {name}")

    def _swap_idle_locked(self, name: str, reason: str) -> bool:
        """Close an idle browser on the box so `name` (under its share) can
        take its slot: one of a site above its share first, else any, least
        recently used within each. False when nothing is idle."""
        idle = [l for l in self._leases.values() if not l.busy and l.driver is not None]
        if not idle:
            return False
        victim = min(idle, key=lambda l: (not self._over_share_locked(l), l.last_used))
        print(f"browser_worker: box full ({reason}), closing idle {victim.recipe} browser "
              f"({'above its share, ' if self._over_share_locked(victim) else ''}"
              f"idle {victim.idle_for:.0f}s) to make room for {name}")
        self._close_locked(victim)
        return True

    def _close_locked(self, lease: Lease) -> None:
        self._leases.pop((lease.recipe, lease.key), None)
        drv = lease.driver
        lease.driver = None
        if drv is not None:
            threading.Thread(target=_close_quietly, args=(drv,), daemon=True).start()

    # ---- reaper ----------------------------------------------------------------

    def _start_reaper_locked(self):
        if self._reaper_started:
            return
        self._reaper_started = True
        self._sampler.start()
        threading.Thread(target=self._reap_loop, name="browser-reaper", daemon=True).start()

    def _reap_loop(self):
        while True:
            time.sleep(settings.REAP_INTERVAL)
            try:
                self.reap()
            except Exception as e:
                print(f"browser_worker reaper: {type(e).__name__}: {e}")

    def _measure_memory(self, recipes: dict) -> None:
        """Measure every browser's process tree, keep it on the lease
        (rss_mb, served by /metrics) and flag (over_rss) the ones past their
        recipe's max_rss_mb. The measuring runs OUTSIDE the lock; only the
        results are set under it."""
        with self._lock:
            leases = [l for l in self._leases.values() if l.driver is not None]
        measured = []
        for lease in leases:
            try:
                measured.append((lease, _rss_mb(lease.driver)))
            except Exception:
                pass
        if measured:
            with self._lock:
                for lease, mb in measured:
                    lease.rss_mb = mb
                    cap = getattr(recipes.get(lease.recipe), "max_rss_mb", settings.MAX_RSS_MB)
                    if cap and mb > cap:
                        lease.over_rss = True

    def reap(self, recipes: Optional[dict] = None) -> int:
        """Close idle browsers past their recipe's idle_seconds, recycle limits
        or memory cap; on a hot host also the idle ones above the box's
        floor, least recently used first. Returns the count."""
        from .registry_runtime import RECIPES   # late import: avoids a cycle
        recipes = recipes or RECIPES
        self._measure_memory(recipes)
        closed = 0
        with self._lock:
            for lease in list(self._leases.values()):
                if lease.busy or lease.driver is None:
                    continue
                recipe = recipes.get(lease.recipe)
                idle_limit = getattr(recipe, "idle_seconds", settings.IDLE_SECONDS)
                if lease.idle_for >= idle_limit or self._stale(recipe, lease):
                    self._close_locked(lease)
                    closed += 1
            reason = self._hot_reason() if self._leases else None
            if reason:
                # A hot host takes bursts back: first every idle browser of a
                # site above its share (down to the share), then idle ones
                # above the box floor; least recently used first in both.
                idle = sorted((l for l in self._leases.values() if not l.busy and l.driver is not None),
                              key=lambda l: l.last_used)
                rest = []
                for victim in idle:
                    if self._over_share_locked(victim):
                        print(f"browser_worker: box hot ({reason}), closing idle {victim.recipe} browser "
                              f"(above its share {self._share.get(victim.recipe)}, idle {victim.idle_for:.0f}s)")
                        self._close_locked(victim)
                        closed += 1
                    else:
                        rest.append(victim)
                excess = len(self._leases) - settings.MAX_BROWSERS_PER_BOX
                for victim in rest[:max(0, excess)]:
                    print(f"browser_worker: box hot ({reason}), closing idle {victim.recipe} browser "
                          f"(idle {victim.idle_for:.0f}s) back toward the floor {settings.MAX_BROWSERS_PER_BOX}")
                    self._close_locked(victim)
                    closed += 1
            if closed:
                self._lock.notify_all()
        return closed

    # ---- introspection ---------------------------------------------------------

    def status(self) -> dict:
        """Per recipe: open / busy / idle and memory. The counts are read
        under the lock, the memory measured after releasing it."""
        with self._lock:
            leases = list(self._leases.values())
            out: Dict[str, dict] = {}
            for lease in leases:
                d = out.setdefault(lease.recipe, {"open": 0, "busy": 0, "idle": 0, "rss_mb": 0})
                d["open"] += 1
                d["busy" if lease.busy else "idle"] += 1
        for lease in leases:
            try:
                out[lease.recipe]["rss_mb"] += int(_rss_mb(lease.driver)) if lease.driver else 0
            except Exception:
                pass
        return out

    def snapshot(self) -> Dict[str, dict]:
        """Per recipe: open / busy and the memory the reaper last measured
        (at most REAP_INTERVAL old). Nothing is measured here: /metrics is
        scraped every minute and must stay as cheap as /health."""
        with self._lock:
            out: Dict[str, dict] = {}
            for lease in self._leases.values():
                d = out.setdefault(lease.recipe, {"open": 0, "busy": 0, "rss_mb": 0.0})
                d["open"] += 1
                d["busy"] += int(lease.busy)
                d["rss_mb"] += lease.rss_mb
        return out

    def admission(self) -> dict:
        """The box cap as it stands now, for /status and /metrics: floor,
        ceiling, what is open or settling, whether one more browser would be
        admitted and if not why, and the host numbers behind that. Counts
        under the lock; nothing is measured."""
        with self._lock:
            open_ = len(self._leases)
            settling = self._settling_locked()
            headroom = self._headroom_refusal_locked(None)
            refusal = self._box_refusal_locked(None)
        floor, ceiling = settings.MAX_BROWSERS_PER_BOX, settings.MAX_BROWSERS_BURST
        s = self._sampler.last()
        return {"floor": floor, "ceiling": ceiling, "open": open_, "settling": settling,
                "effective_cap": ceiling if headroom is None else floor,
                "admitting": refusal is None, "refusal": refusal,
                "host": {"cpu_pct": round(s.cpu_pct, 1), "cpu_now_pct": round(s.cpu_now_pct, 1),
                         "free_mb": round(s.free_mb), "sampled_s_ago": round(time.monotonic() - s.at, 1)}
                if s else None,
                "sampler_error": getattr(self._sampler, "error", None)}

    def count(self) -> int:
        # no lock: /health must answer even while the registry is busy
        return len(self._leases)

    def close_all(self, grace: float = 0.0) -> None:
        """Deploy restart: stop handing out browsers, wait for busy ones up to
        `grace` seconds, then close everything."""
        deadline = time.monotonic() + grace
        with self._lock:
            while any(l.busy for l in self._leases.values()) and time.monotonic() < deadline:
                self._lock.wait(timeout=0.5)
            for lease in list(self._leases.values()):
                self._close_locked(lease)


def _still_parked(driver, park: str) -> bool:
    """Is the browser's page still on the park URL? An in-page recipe runs
    fetch() from the parked page and never navigates, so re-parking it is a
    navigation through the proxy (~0.7-1.1 s busy) for nothing; a page that
    DID leave must be parked (a live page keeps painting and running timers:
    the 2026-10-15 CPU incident, and keeps sending its background traffic
    through the exit). Decided from the page's real URL (a local read on the
    owner thread), exact match; anything else, or no way to tell, parks."""
    try:
        current = driver.call(lambda page: page.url, timeout=PARK_CHECK_TIMEOUT)
    except Exception:
        return False
    return current == park


def _close_quietly(driver):
    try:
        driver.close()
    except Exception:
        pass


def _refusal_suffix(refusal: Optional[str]) -> str:
    return f" ({refusal})" if refusal else ""


def _rss_mb(driver) -> float:
    """Memory of the browser process tree behind `driver`, in MB, as the sum
    of each process's unique set size (USS). Summing RSS over a Chrome tree
    counts every shared page once per process and reported 1.5 GB+ for one
    idle page (2026-10-22), which tripped the recycle cap and closed every
    kept browser right after its first request. Falls back to RSS when USS
    is unavailable."""
    import psutil
    pid = getattr(driver, "browser_pid", None)
    if pid is None:
        return 0.0
    try:
        procs = [psutil.Process(pid)]
        procs += procs[0].children(recursive=True)
    except psutil.Error:
        return 0.0
    total = 0
    for p in procs:
        try:
            try:
                total += p.memory_full_info().uss
            except (psutil.AccessDenied, AttributeError):
                total += p.memory_info().rss
        except psutil.Error:
            pass
    return total / 1_048_576


REGISTRY = Registry()
