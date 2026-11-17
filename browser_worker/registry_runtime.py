"""Load every recipe from scrapers/recipes/, keyed by site name. The
*declaration* of which box serves a site in production lives in
scrapers/recipes/registry.py (plain data; GKE's browser_client routes by
it). Since 2026-10-22 both boxes load ALL recipes: a loaded recipe costs
nothing until it is called (nothing warms at start), and the Windows box
must be able to answer the D10 Windows-vs-Ubuntu A/B (tools/box_ab.py)
for any site without a redeploy. /health lists which loaded recipes are
this box's "home" sites."""
from __future__ import annotations

import importlib
import pkgutil

from . import settings

RECIPES: dict = {}


def load() -> dict:
    import recipes
    from recipes.registry import BOX_FOR
    RECIPES.clear()
    for mod in pkgutil.iter_modules(recipes.__path__):
        if mod.name.startswith("_") or mod.name == "registry":
            continue
        module = importlib.import_module(f"recipes.{mod.name}")
        cls = getattr(module, "RECIPE", None)
        if cls is None:
            continue
        inst = cls()
        RECIPES[inst.name] = inst
    return RECIPES


def home_sites() -> list:
    """The loaded recipes whose declared box (registry.BOX_FOR) is this one."""
    from recipes.registry import BOX_FOR
    return sorted(n for n, r in RECIPES.items() if BOX_FOR.get(n, (r.box,))[0] == settings.BOX)
