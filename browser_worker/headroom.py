"""Host headroom for the box's browser cap: a sampler thread that keeps the
last ~10 s of CPU and the free memory, read by lifecycle.Registry when a
request wants a browser above the floor (settings.MAX_BROWSERS_PER_BOX).

Why a thread of its own and not the reaper: the reaper runs every 15 s and
its memory walk takes seconds on Windows; a trailing CPU average needs a
sample a second. Two non-blocking psutil calls per tick cost nothing.
Readers never lock: `_last` is one tuple swapped atomically.

Mint browsers never enter the Registry, but their CPU is in the host
sample, which is the point of measuring the host instead of counting.
"""
from __future__ import annotations

import threading
import time
from collections import deque, namedtuple
from typing import Optional

from . import settings

Sample = namedtuple("Sample", "cpu_pct cpu_now_pct free_mb at")


class HostSampler:
    """psutil CPU (trailing mean over HOST_SAMPLE_WINDOW ticks) and free memory.
    `last()` is None until 3 ticks are in (a fresh worker cannot burst on a
    fake 0 %), when a tick failed, or when the last sample is older than
    HOST_SAMPLE_MAX_AGE (the thread died)."""

    def __init__(self):
        self._last: Optional[Sample] = None
        self._window: deque = deque(maxlen=settings.HOST_SAMPLE_WINDOW)
        self._started = False
        self._lock = threading.Lock()
        self.error: Optional[str] = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            import psutil
            psutil.cpu_percent(interval=None)      # the first call always returns 0.0
        except Exception as e:
            self._fail(e)
        threading.Thread(target=self._loop, name="host-sampler", daemon=True).start()

    def last(self) -> Optional[Sample]:
        s = self._last
        if s is None or time.monotonic() - s.at > settings.HOST_SAMPLE_MAX_AGE:
            return None
        return s

    # ---- internals ---------------------------------------------------------------

    def _loop(self) -> None:
        # Sleep first: start() has just primed cpu_percent, and a tick right
        # after it would measure microseconds (psutil reports 0.0 or noise
        # for an empty interval), seeding the first mean with a fake idle.
        while True:
            time.sleep(settings.HOST_SAMPLE_INTERVAL)
            self._tick()

    def _tick(self) -> None:
        try:
            import psutil
            now = float(psutil.cpu_percent(interval=None))
            free = psutil.virtual_memory().available / 1_048_576
        except Exception as e:
            self._fail(e)
            return
        if self.error:
            print("browser_worker host sampler: recovered; the box may burst again")
            self.error = None
        self._window.append(now)
        if len(self._window) < 3:
            return
        self._last = Sample(sum(self._window) / len(self._window), now, free, time.monotonic())

    def _fail(self, e: Exception) -> None:
        self._last = None
        self._window.clear()
        msg = f"{type(e).__name__}: {e}"
        if msg != self.error:
            print(f"browser_worker host sampler: {msg}; the box stays at the floor "
                  f"({settings.MAX_BROWSERS_PER_BOX} browsers)")
        self.error = msg


class StaticSampler:
    """A sampler whose numbers are set by hand (tests, probes)."""

    def __init__(self, cpu_pct: Optional[float] = None, free_mb: Optional[float] = None):
        self.error = None
        self.set(cpu_pct, free_mb)

    def set(self, cpu_pct: Optional[float] = None, free_mb: Optional[float] = None) -> None:
        self.cpu_pct = cpu_pct
        self.free_mb = free_mb

    def start(self) -> None:
        pass

    def last(self) -> Optional[Sample]:
        if self.cpu_pct is None:
            return None
        return Sample(self.cpu_pct, self.cpu_pct, self.free_mb if self.free_mb is not None else 1e9,
                      time.monotonic())
