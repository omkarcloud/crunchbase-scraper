"""The URL patterns a pooled browser aborts, shared by patchright_driver
(Chrome) and camoufox_driver (Firefox).

Both drivers install ONE abort-only route built from a single compiled regex:
playwright ships a string/regex route pattern to its driver process as the
interception pattern, so non-matching requests are continued inside the
driver and only the requests to abort ever cross the pipe into Python (the
old "**/*" route that decided per request in Python saturated that pipe and
wedged pages - see patchright_driver). Two kinds of pattern feed that regex:

  * block_resources types that have a URL shape ("stylesheet", "media").
    "image" and "font" are NOT url patterns: Chrome honours them as launch
    flags and Firefox as a pref, so compile_block_re() ignores them.
  * block_hosts: host globs for third-party trackers / CDNs a page does not
    need ("cdn.segment.com", "*.hotjar.io"), anchored on the URL authority.

URL matching is deliberately loose (extension-less assets and blob: media
slip through) - the goal is bandwidth and renderer memory, not an airtight
block. Keep every pattern JS-compatible (no inline flags, no lookbehind):
playwright hands the regex source + flags to Node.

    python -m pytest test_url_blocking.py -q
"""
import re

RESOURCE_URL_PATTERNS = {
    "stylesheet": r"\.css(?:[?#]|$)|//fonts\.googleapis\.com/",
    "media": r"\.(?:mp4|webm|m4v|mov|mp3|m4a|ogg|oga|wav|flac|aac)(?:[?#]|$)",
}


def host_glob_pattern(glob):
    """Regex source matching URLs whose host is `glob`: "cdn.segment.com" is
    that exact host; "*.hotjar.io" is any subdomain ("*" spans one or more
    labels; the bare domain is NOT matched - list it separately). Anchored on
    the authority, so "cdn.segment.com.evil.com" and "www.cdn.segment.com"
    never match a "cdn.segment.com" entry."""
    esc = re.escape(glob.strip().lower()).replace(r"\*", r"[^/:?#]+")
    return rf"^https?://(?:[^/?#]*@)?{esc}(?::\d+)?(?:[/?#]|$)"


def compile_block_re(block_resources=(), block_hosts=()):
    """One case-insensitive regex for the drivers' abort route, or None when
    nothing needs a URL match."""
    parts = [RESOURCE_URL_PATTERNS[t] for t in sorted(set(block_resources or ()))
             if t in RESOURCE_URL_PATTERNS]
    parts += [host_glob_pattern(h) for h in (block_hosts or ()) if h and h.strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None
