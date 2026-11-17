"""Run a recipe in the calling process instead of on a browser box.

The scrapers reach the boxes over HTTP (browser_client). When
BROWSER_BOX_URLS names a site's box as "inprocess" (`ubuntu=inprocess`), the
same request is answered here: the recipe runs in this process, on this
machine's own Chrome, through the same registry, caps and idle reaper. That
is how a laptop runs a browser-backed scraper without a box, and how an
open-source repo gets a browser at all.

act() mirrors server.act(): the same checks in the same order, and the same
status for each outcome, so browser_client maps both to the same errors.
"""
import threading
import time

from . import registry_runtime
from .lifecycle import BoxBusy
from .types import REQUEST, RecipeError, chosen_candidate

_load_lock = threading.Lock()


def recipes():
    """The loaded recipes, loaded on first use."""
    if not registry_runtime.RECIPES:
        with _load_lock:
            if not registry_runtime.RECIPES:
                registry_runtime.load()
    return registry_runtime.RECIPES


def _error(status, kind, message, candidate=None):
    body = {"error": message, "kind": kind}
    if candidate is not None:
        body["candidate"] = candidate
    return status, body, candidate


def act(site, action, body):
    """One mint / token / fetch -> (status, payload, candidate index or None).
    The payload of a non-200 is {"error", "kind"}, as the worker answers."""
    recipe = recipes().get(site)
    if recipe is None:
        return _error(404, "no_such_site", f"{site!r} is not served by this box")
    if action not in recipe.actions:
        return _error(404, "no_such_action", f"{site!r} supports {list(recipe.actions)}, not {action!r}")
    b = body or {}
    proxy = b.get("proxy") or None
    variant = b.get("variant") or ""
    candidates = b.get("candidates")
    if candidates is not None and (not isinstance(candidates, list) or not candidates
                                   or not all(isinstance(c, dict) for c in candidates)):
        return _error(400, "bad_request", "candidates must be a non-empty list of {proxy, session}")
    if candidates and action == "mint":
        return _error(400, "bad_request", "mint takes one proxy, not candidates")
    REQUEST.candidates, REQUEST.chosen = candidates, None
    try:
        if action == "mint":
            out = recipe.mint(proxy, variant)
        elif action == "token":
            out = recipe.token(proxy, variant, b.get("input"))
        else:
            if not (b.get("url") or b.get("urls")):
                return _error(400, "bad_request", "url or urls is required")
            out = recipe.fetch(proxy, variant, b.get("session"), b.get("url"), b.get("urls"),
                               b.get("method") or "GET", b.get("headers") or {}, b.get("body"))
    except BoxBusy as e:
        return _error(503, "busy", str(e))
    except RecipeError as e:
        return _error(502, "recipe", str(e), candidate=chosen_candidate())
    except Exception as e:
        print(f"browser_worker {site}/{action}: {type(e).__name__}: {e}")
        return _error(500, "error", f"{type(e).__name__}: {e}", candidate=chosen_candidate())
    finally:
        chosen = chosen_candidate()
        REQUEST.candidates = REQUEST.chosen = None
    return 200, out, chosen
