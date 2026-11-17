"""The one lock around a browser launch's process attribution.

Both drivers (patchright_driver, camoufox_driver) record the OS processes a
browser owns by pid-diff: a snapshot of this process's children before the
spawn, another after the first page is up. Two launches overlapping that
window would own each other's processes, so the window is serialized -- and
it must be ONE lock across both drivers (a Windows box runs Camoufox mints
next to patchright browsers). Until 2026-11-04 browser_worker.types held a
lock around the whole PatchrightDriver constructor, which also covers the
exit geo lookup through the proxy (12 s timeout) and the native probe, so a
cold burst launched strictly one at a time; now only the spawn window is
under the lock (patchright_driver takes it itself; types takes it around
the Camoufox constructor, which does its own geoip inside).
"""
import threading

LAUNCH_LOCK = threading.Lock()
