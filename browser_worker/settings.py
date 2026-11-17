"""Worker-wide knobs. Per-recipe values on the recipe class override these."""
import os

BOX = os.environ.get("BOX", "")                      # "windows" | "ubuntu"; empty = all recipes (dev)
PORT = int(os.environ.get("BROWSER_WORKER_PORT", "6010"))
THREADS = int(os.environ.get("BROWSER_WORKER_THREADS", "16"))

IDLE_SECONDS = 300          # token/fetch browser closes after this long unused
MAX_USES = 200              # recycle after this many requests
MAX_AGE_SECONDS = 1800      # recycle after this long alive
MAX_RSS_MB = 1500           # recycle when the browser tree passes this
MAX_BROWSERS_PER_RECIPE = 2
# Box cap = a floor that is always allowed plus a ceiling the box may use
# only while it has headroom (browser_worker/headroom.py measures it). With
# no burst configured the ceiling equals the floor: a fixed cap, as before
# 2026-11-03.
MAX_BROWSERS_PER_BOX = int(os.environ.get("BROWSER_WORKER_MAX_BROWSERS", "12"))       # floor
MAX_BROWSERS_BURST = max(MAX_BROWSERS_PER_BOX,
                         int(os.environ.get("BROWSER_WORKER_MAX_BROWSERS_BURST",
                                            str(MAX_BROWSERS_PER_BOX))))               # ceiling
CPU_HEADROOM_PCT = float(os.environ.get("BROWSER_WORKER_CPU_HEADROOM_PCT", "65"))     # admit above the floor below this
CPU_SHRINK_PCT = float(os.environ.get("BROWSER_WORKER_CPU_SHRINK_PCT", "80"))         # reaper closes idle above the floor at/over this
MEMORY_RESERVE_MB = int(os.environ.get("BROWSER_WORKER_MEMORY_RESERVE_MB", "2048"))   # free memory kept for the OS + the worker
LAUNCH_CPU_PCT = 100.0 / (os.cpu_count() or 4)   # a launching/settling browser is assumed to cost one core
LAUNCH_SETTLE_SECONDS = 15                       # a browser counts as unmeasured load this long after its launch BEGAN
HOST_SAMPLE_INTERVAL = 1.0                       # seconds between host CPU/memory samples
HOST_SAMPLE_WINDOW = 10                          # samples averaged (~10 s of CPU)
HOST_SAMPLE_MAX_AGE = 5.0                        # an older sample means the sampler died: floor only
QUEUE_SECONDS = 20          # wait this long for a slot, then 503 busy
REAP_INTERVAL = 15
DRAIN_SECONDS = 30          # graceful restart: finish in-flight work up to this long

# Mint defaults (cookie_mint recipes override per site)
MINT_TIMEOUT = 60
MINT_POLL = 1.0
