"""Site recipes for the browser worker: one module per site, each exposing
RECIPE (a subclass of a browser_worker.types type). Only "how to get past
the site" lives here; parsing stays on GKE. registry.py declares which box
owns each site and is the one module GKE imports."""
