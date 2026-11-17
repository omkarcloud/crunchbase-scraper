"""Where a site's browser work runs. The hosted service sends it to separate
machines; this repo runs the recipe in its own process, on the Chrome of the
machine it runs on (browser_worker.inprocess). Google Chrome must be installed."""
import os

os.environ.setdefault("BROWSER_BOX_URLS", "local=inprocess")
BOX_FOR = {}


def box_for(site):
    return "local"
