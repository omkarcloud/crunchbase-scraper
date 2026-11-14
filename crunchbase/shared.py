"""Helpers shared by the /crunchbase/* endpoint modules: reference ->
uuid resolution (the searches filter on uuids), name -> uuid lookups for
categories / locations, predicate builders, and the cursor-emulated
pagination the anonymous search entitlement allows.

Pagination: Crunchbase refuses `after_id` paging anonymously (MD403
"insufficient permissions to paginate") and caps every search at 15 rows,
but filtering on the sort field itself walks pages exactly — sort by
rank_org_company ascending, then ask for rank > the last row's rank; sort
by announced_on descending, then ask for announced_on <= the last date and
skip the rows already seen. `paged_search` runs that walk: a cold page N
costs N search calls (no organization quota involved), but the cursor each
hop ends on is memoised per query for CURSOR_TTL seconds, so a client
walking page 1, 2, 3… pays one call per page (measured before the memo:
2.16 GB / 20,869 search requests in 3 days on this host, 2026-10-23).
"""
import json
import re
import threading
import time

from crunchbase import fetch, refs
from crunchbase.fetch import predicate

PAGE_SIZE = fetch.SEARCH_LIMIT
MAX_WALK = 40           # deepest page reachable by cursor walking
CURSOR_TTL = 600        # seconds a query's walked cursors stay resumable
CURSOR_MEMO_SIZE = 2000  # distinct (query, sort) walks remembered at once


class _TTLCache:
    def __init__(self, ttl, max_size):
        self.ttl, self.max_size = ttl, max_size
        self.lock = threading.Lock()
        self.data = {}

    def get(self, key):
        with self.lock:
            hit = self.data.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            self.data.pop(key, None)
            return None

    def put(self, key, value):
        with self.lock:
            if len(self.data) >= self.max_size:
                oldest = min(self.data, key=lambda k: self.data[k][0])
                self.data.pop(oldest, None)
            self.data[key] = (time.time(), value)


_uuid_cache = _TTLCache(ttl=6 * 3600, max_size=5000)
_name_cache = _TTLCache(ttl=24 * 3600, max_size=5000)

_AUTOCOMPLETE_KIND = {
    "organization": "organizations",
    "person": "people",
    "funding_round": "funding_rounds",
    "acquisition": "acquisitions",
    "event": "events",
    "hub": "hubs",
    "fund": "funds",
    "ipo": "ipos",
}


def uuid_of(kind, ref, missing=None):
    """The uuid of a resolved ref ({id, kind}) of entity `kind`. A uuid is
    returned as is; a permalink is looked up through the search typeahead
    (fetch.autocomplete, no quota) and, when that does not list it, through
    the entity endpoint (one organization-quota unit for companies)."""
    ident = ref["id"] if isinstance(ref, dict) else ref
    if refs.is_uuid(ident):
        return ident
    key = (kind, ident)
    hit = _uuid_cache.get(key)
    if hit:
        return hit
    found = None
    coll = _AUTOCOMPLETE_KIND.get(kind)
    if coll:
        words = " ".join(w for w in ident.replace("-", " ").split() if not (len(w) == 4 and all(c in "0123456789abcdef" for c in w)))
        try:
            # every row the typeahead's two searches fetched: a cut would only
            # push a listed permalink onto the quota-spending entity lookup
            data = fetch.autocomplete(words or ident, coll, limit=2 * fetch.SEARCH_LIMIT)
        except fetch.CrunchbaseUpstreamError:
            data = None
        for row in (data or {}).get("entities") or []:
            ent = row.get("identifier") or {}
            if (ent.get("permalink") or "").lower() == ident:
                found = ent.get("uuid")
                break
    if not found:
        data = fetch.entity(refs.COLLECTIONS[kind], ident, ["identifier"],
                            missing=missing or f"{kind.replace('_', ' ')} '{ident}' not found on Crunchbase")
        found = ((data.get("properties") or {}).get("identifier") or {}).get("uuid")
    if not found:
        raise fetch.CrunchbaseNotFound(missing or f"{kind} '{ident}' not found on Crunchbase")
    _uuid_cache.put(key, found)
    return found


def entity_uuid(ref, default_kind):
    """uuid for a ref whose link may have fixed the kind (investors, participants)."""
    kind = (ref.get("kind") if isinstance(ref, dict) else None) or default_kind
    return uuid_of(kind, ref)


_LEGAL_SUFFIX = ("inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc",
                 "gmbh", "ag", "sa", "bv", "pbc", "lp", "llp", "pvt", "pte")


def name_spellings(name):
    """The spellings a typed name is searched under, in order: as typed,
    without a legal suffix ("Stripe, Inc." -> "Stripe"), and written as one
    word ("Open AI" -> "OpenAI": Crunchbase matches a name prefix, so a space
    the site does not have finds nothing)."""
    words = name.replace(",", " ").split()
    while len(words) > 1 and words[-1].lower().replace(".", "") in _LEGAL_SUFFIX:     # "Inc.", "S.A.", "L.L.C."
        words.pop()
    plain = " ".join(words)
    out = []
    for spelling in (" ".join(name.split()), plain, "".join(plain.split())):
        if spelling and spelling.lower() not in [o.lower() for o in out]:
            out.append(spelling)
    return out


def _words(value):
    return re.findall(r"[a-z0-9]+", (value or "").lower())


def pick_named(rows, name, exact_only=False):
    """The typeahead row (best rank first) that IS the entity called `name`:
    the row named exactly so (spacing, case and punctuation aside), else the
    best-ranked row whose name begins with the typed WORDS ("f prime" ->
    "F-Prime Capital"). A partial word never matches ("ventures" is not
    "VentureSoul Partners"), nor does a name that merely contains the words."""
    key = refs.name_key(name)
    if not key:
        return None
    typed = _words(name)
    for row in rows:
        if refs.name_key((row.get("identifier") or {}).get("value")) == key:
            return row
    if exact_only:
        return None
    for row in rows:
        words = _words((row.get("identifier") or {}).get("value"))
        if len(words) > len(typed) and words[:len(typed)] == typed:
            return row
    return None


def find_named(collection, name):
    """The identifier ({uuid, permalink, value, entity_def_id}) of the
    `collection` row called `name`, through the search typeahead (two
    searches per spelling, no organization quota), or None. An exact name
    under any spelling wins; failing that, the best-ranked row whose name
    starts with what was typed."""
    row, first_rows = None, None
    for spelling in name_spellings(name):
        rows, _total = fetch._typeahead(collection, spelling)
        if first_rows is None:
            first_rows = rows
        row = pick_named(rows, spelling, exact_only=True)
        if row:
            break
    row = row or pick_named(first_rows or [], name)
    ident = (row or {}).get("identifier") or {}
    return ident if (ident.get("uuid") or ident.get("permalink")) else None


def slug_words(ident):
    """A permalink read as the name it was made from: dashes to spaces, minus
    the 4-hex suffix Crunchbase adds to a taken name ("john-smith-7d6e",
    "instinct-9264"). Only a LAST word is a suffix: "1836-ventures" keeps 1836."""
    words = (ident or "").replace("-", " ").split()
    if len(words) > 1 and len(words[-1]) == 4 and all(c in "0123456789abcdef" for c in words[-1]):
        words.pop()
    return " ".join(words)


_investor_names = _TTLCache(ttl=6 * 3600, max_size=5000)     # name key -> (uuid, kind)


def investor_by_name(name):
    """(uuid, kind) of the investor called `name` (a company or a person,
    from the principal.investors collection), or None. Memoised."""
    key = refs.name_key(name)
    if not key:
        return None
    hit = _investor_names.get(key)
    if hit:
        return hit
    ident = find_named("principal.investors", name)
    if not ident or not ident.get("uuid"):
        return None
    found = (ident["uuid"], ident.get("entity_def_id"))
    _investor_names.put(key, found)
    _uuid_cache.put((found[1] or "organization", ident.get("permalink") or ident["uuid"]), ident["uuid"])
    return found


def entity_uuid_any(ref):
    """A company-or-person ref without a link: try organization, then person.
    A plain name ({"id": None, "name": …}) and a permalink Crunchbase does not
    have ("vocap-partners": the firm is vocap-ventures, named Vocap Partners)
    are looked up by name among investors."""
    kind = ref.get("kind") if isinstance(ref, dict) else None
    if isinstance(ref, dict) and ref.get("id") is None and ref.get("name"):
        found = investor_by_name(ref["name"])
        if found:
            return found
        raise fetch.CrunchbaseNotFound(f"no investor named '{ref['name']}' on Crunchbase")
    if kind:
        return uuid_of(kind, ref), kind
    ident = ref["id"]
    if refs.is_uuid(ident):
        return ident, None
    for k in ("organization", "person"):
        try:
            return uuid_of(k, ref), k
        except fetch.CrunchbaseNotFound:
            continue
    found = investor_by_name(slug_words(ident) or ident)
    if found:
        return found
    raise fetch.CrunchbaseNotFound(f"'{ident}' is not a company or person on Crunchbase")


def _permalink_uuid(collection, value):
    """uuid of the `collection` entity whose permalink is `value`, or None."""
    try:
        data = fetch.entity(collection, value.strip().lower().replace(" ", "-"), ["identifier"])
    except (fetch.CrunchbaseNotFound, fetch.CrunchbaseBadRequest):
        return None
    return ((data.get("properties") or {}).get("identifier") or {}).get("uuid")


def _hub_location(name):
    """The location Crunchbase's best-ranked hub named after `name` covers
    ("Berlin Companies" -> Berlin, Germany), or None. Hubs are ranked and
    carry their locations, which makes them the one cheap signal for which
    of many namesakes is the place people mean."""
    data = fetch.search("hubs", ["identifier", "locations", "rank_hub"], [predicate("identifier", "starts", [name])],
                        [{"field_id": "rank_hub", "sort": "asc"}])
    for row in data.get("entities") or []:
        for location in (row.get("properties") or {}).get("locations") or []:
            if isinstance(location, dict) and (location.get("value") or "").lower() == name:
                return location.get("uuid")
    return None


def _busiest_location(rows):
    """Of several locations, the one with the most companies. One
    count-only company search per candidate. A count that fails raises: a
    pick made without it would be a guess, and lookup_uuid caches the pick
    for a day."""
    def companies(row):
        data = fetch.search("organization.companies", ["identifier"],
                            [predicate("location_identifiers", "includes", [row["identifier"]["uuid"]])], limit=1)
        return data.get("count") or 0
    counts = fetch.run_parallel([lambda r=r: companies(r) for r in rows])
    best = max(range(len(rows)), key=lambda i: (counts[i], -i))
    return rows[best]


def _pick_location(value, name, rows, named, total):
    """The uuid of the location `value` means among the typeahead `rows`
    (`named`: the ones called exactly `name`), or None when there is none.

    Locations repeat names ("London" is seven places, "San Francisco"
    sixty) and the search has no relevance to tell them apart. Several
    candidates are settled by the best-ranked hub of that name, else by
    their company count. A name with more namesakes than one search page
    shows, and no hub, is refused with a hint rather than guessed."""
    candidates = named or rows
    if len(candidates) <= 1:
        return candidates[0]["identifier"].get("uuid") if candidates else None
    # names sort first, so a page with room after them holds every namesake;
    # without a namesake, both searches must have fit their page (`total` is
    # the larger of the two counts, and `rows` their union: comparing those
    # two called 20 + 20 matches cut to 15 + 15 rows complete)
    complete = len(named) < fetch.SEARCH_LIMIT if named else total <= fetch.SEARCH_LIMIT
    hub = _hub_location(name)
    if hub and (hub in {c["identifier"].get("uuid") for c in candidates} or (not complete and "," not in value)):
        return hub
    if not complete:
        raise ValueError(f"Several locations match '{value}'. Add its region or country after a comma "
                         f"(for example 'San Francisco, California') or pass the location's id.")
    return _busiest_location(candidates)["identifier"].get("uuid")


def _name_uuid(collection, text, value):
    """uuid of the `collection` entity named `text` (one spelling of the
    caller's `value`) through the search typeahead (fetch.autocomplete), or
    None: the row whose permalink is `value`, else the row named exactly
    `text` (_pick_location among namesake locations), else the first row."""
    data = fetch.autocomplete(text, collection, limit=2 * fetch.SEARCH_LIMIT) or {}
    rows = data.get("entities") or []
    located = collection.startswith("location")
    # only a location query carries qualifiers after commas (fetch._typeahead)
    name = (text.split(",")[0] if located else text).strip().lower()
    by_permalink = [r for r in rows if (r["identifier"].get("permalink") or "").lower() == value.lower()]
    named = [r for r in rows if (r["identifier"].get("value") or "").lower() == name]
    if by_permalink:
        return by_permalink[0]["identifier"].get("uuid")
    if located:
        return _pick_location(text, name, rows, named, data.get("count") or 0)
    return (named or rows)[0]["identifier"].get("uuid") if rows else None


def lookup_uuid(collection, value, kind_label):
    """Category / location given as a uuid, a permalink or a name -> uuid.
    A permalink resolves through the entity endpoint, a name through the
    search typeahead (_name_uuid). A location name may carry its region or
    country after a comma ("Springfield, Illinois").

    The searches match a name literally, hyphens included ("e commerce"
    finds nothing, "e-commerce" finds E-Commerce; Winston Salem and
    Winston-salem are two places), so a hyphenated value is searched both
    ways until one resolves: with spaces first when it looks like a slug
    ("new-york"), as written first when it is a display name."""
    if refs.is_uuid(value):
        return value
    key = (collection, value.lower())
    hit = _name_cache.get(key)
    if hit:
        return hit
    slug_like = "-" in value and " " not in value and "," not in value
    found = _permalink_uuid(collection, value) if slug_like else None
    spaced = value.replace("-", " ")
    for text in dict.fromkeys((spaced, value) if slug_like else (value, spaced)):
        if found:
            break
        found = _name_uuid(collection, text, value)
    if not found and not slug_like and "," not in value:
        found = _permalink_uuid(collection, value)
    if not found:
        raise ValueError(f"Unknown {kind_label} '{value}'.")
    _name_cache.put(key, found)
    return found


def category_uuid(value):
    return lookup_uuid("categories", value, "category")


def category_group_uuid(value):
    return lookup_uuid("category_groups", value, "category group")


def location_uuid(value):
    return lookup_uuid("locations", value, "location")


# ---- predicates -----------------------------------------------------------------------

def usd(amount):
    return {"currency": "usd", "value": int(amount)}


def add(query, field_id, operator_id, values):
    """Append a predicate unless the value is empty."""
    if values is None or values == [] or values == "":
        return
    query.append(predicate(field_id, operator_id, values))


def date_range(query, field_id, after, before):
    if after and before:
        add(query, field_id, "between", [after, before])
    elif after:
        add(query, field_id, "gte", [after])
    elif before:
        add(query, field_id, "lte", [before])


def money_range(query, field_id, minimum, maximum):
    if minimum is not None and maximum is not None:
        add(query, field_id, "between", [usd(minimum), usd(maximum)])
    elif minimum is not None:
        add(query, field_id, "gte", [usd(minimum)])
    elif maximum is not None:
        add(query, field_id, "lte", [usd(maximum)])


# ---- pagination ------------------------------------------------------------------------

def pagination(page, total_count, per_page=PAGE_SIZE, has_items=True, reachable=None):
    total_pages = -(-int(total_count or 0) // per_page) if total_count else (page if has_items else max(page - 1, 0))
    if reachable is not None:
        total_pages = min(total_pages, reachable)
    return {"page": page, "items_per_page": per_page, "total_pages": max(total_pages, page if has_items else 0),
            "total_count": total_count}


def _sort_value(row, field):
    return (row.get("properties") or {}).get(field)


def _cursor_value(row, field):
    value = _sort_value(row, field)
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return value


class _CursorMemo:
    """Resumable walk states per query: key -> {hop: state}, where `hop` is
    the 0-based search call the state lets the walk start from (hop 2 = the
    cursor page 3 is read with). A walk's states expire together, CURSOR_TTL
    after the walk's first hop was stored, so every page a client reads in
    one sitting comes from the same snapshot of the sort order and a stale
    page-1 cursor is never kept alive by deeper reads."""

    def __init__(self, ttl, max_size):
        self.ttl, self.max_size = ttl, max_size
        self.lock = threading.Lock()
        self.data = {}          # key -> (created_at, {hop: state})

    def resume_point(self, key, page):
        """(hop, state) of the deepest memoised hop <= page - 1, or (0, None)."""
        with self.lock:
            entry = self.data.get(key)
            if not entry or time.time() - entry[0] >= self.ttl:
                self.data.pop(key, None)
                return 0, None
            states = entry[1]
        for hop in range(page - 1, 0, -1):
            state = states.get(hop)
            if state is not None:
                return hop, state
        return 0, None

    def store(self, key, hop, state):
        with self.lock:
            now = time.time()
            entry = self.data.get(key)
            if entry and now - entry[0] >= self.ttl:
                entry = None
            if entry is None:
                if len(self.data) >= self.max_size:
                    oldest = min(self.data, key=lambda k: self.data[k][0])
                    self.data.pop(oldest, None)
                entry = self.data[key] = (now, {})
            entry[1][hop] = state

    def clear(self):
        with self.lock:
            self.data.clear()


_cursors = _CursorMemo(ttl=CURSOR_TTL, max_size=CURSOR_MEMO_SIZE)
_card_cursors = _CursorMemo(ttl=CURSOR_TTL, max_size=CURSOR_MEMO_SIZE)


def _memo_key(*parts):
    return json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))


def paged_search(collection, field_ids, query, sort_field, direction, page, row_parser,
                 cursor_field=None, dedupe=True):
    """One page of a search, walking the sort field as a cursor. Returns
    ({items, pagination}, raw rows). Sorting by `identifier` (name) cannot
    walk (gt/lt are unsupported on identifier): page 1 only.

    Ranks are unique, so the next hop asks for rank > the last one. Every
    other sort field (dates, scores, counts, enums) repeats across rows, so
    the next hop uses the inclusive operator and skips the uuids already
    served — a strict operator would silently drop every row that ties with
    the last one of the previous page. A tie group larger than a page would
    stall the walk; it is then stepped past with the strict operator.

    The cursor each hop ends on (with the uuids skipped so far and the
    unfiltered total count) is memoised per (collection, fields, query,
    sort) for CURSOR_TTL seconds, so page N right after pages 1..N-1 is one
    search call; a cold page N still walks N calls."""
    cursor_field = cursor_field or sort_field
    order = [{"field_id": sort_field, "sort": direction}]
    if cursor_field == "identifier" and page > 1:
        return {"items": [], "pagination": pagination(page, None, has_items=False, reachable=1)}, []
    fields = list(dict.fromkeys(list(field_ids) + [cursor_field]))
    base = list(query)
    strict_op = "gt" if direction == "asc" else "lt"
    tie_op = "gte" if direction == "asc" else "lte"
    ties = not cursor_field.startswith("rank_")
    cursor = None                     # (operator, value) of the cursor predicate
    seen = set()
    rows, total = [], None
    walkable = True

    memo_key = _memo_key(collection, fields, base, sort_field, direction, cursor_field)
    start, state = _cursors.resume_point(memo_key, page)
    if state is not None:
        cursor, seen, total = state[0], set(state[1]), state[2]

    def run(cur):
        q = base + ([predicate(cursor_field, cur[0], [cur[1]])] if cur else [])
        return fetch.search(collection, fields, q, order, limit=PAGE_SIZE)

    for hop in range(start, page):
        data = run(cursor)
        batch = data.get("entities") or []
        rows = [r for r in batch if r.get("uuid") not in seen]
        if cursor and cursor[0] == tie_op and not rows and len(batch) >= PAGE_SIZE:
            # a tie group bigger than a page: step past its value
            cursor = (strict_op, cursor[1])
            data = run(cursor)
            batch = data.get("entities") or []
            rows = [r for r in batch if r.get("uuid") not in seen]
        total = data.get("count") if total is None else total
        if rows and _sort_value(rows[-1], cursor_field) is None:
            # the sort field is one the anonymous entitlement hides (dates,
            # amounts come back as preview placeholders): no cursor, one page
            walkable = False
        continues = len(batch) >= PAGE_SIZE and bool(rows) and walkable
        if continues:
            # remember where the next page starts, whether or not it is
            # asked for in this call
            last = _cursor_value(rows[-1], cursor_field)
            if ties:
                seen = seen | {r.get("uuid") for r in rows}
                cursor = (tie_op, last)
            else:
                cursor = (strict_op, last)
            _cursors.store(memo_key, hop + 1, (cursor, frozenset(seen), total))
        if hop == page - 1:
            break
        if not continues:
            rows = []
            break
    items = [x for x in (row_parser(r) for r in rows) if x]
    reachable = 1 if (cursor_field == "identifier" or not walkable) else MAX_WALK
    return {"items": items, "pagination": pagination(page, total, has_items=bool(items), reachable=reachable)}, rows


def card_pages(collection, ident, card_id, page, first_rows=None):
    """`fetch.card_pages` with the after_id each hop ends on memoised per
    (collection, entity, card) for CURSOR_TTL seconds: page N right after
    pages 1..N-1 costs one cards call (one unit of the organization quota)
    instead of N-1. Same end-of-walk rules as fetch.card_pages: an empty
    hop, or one shorter than a full cards page, ends the walk — except a
    short embedded first page, which the entity truncates."""
    memo_key = _memo_key(collection, ident, card_id)
    start, state = _card_cursors.resume_point(memo_key, page)
    if state is not None:
        rows = fetch.card(collection, ident, card_id, after_id=state)
        hop = start
    else:
        rows = first_rows if first_rows is not None else fetch.card(collection, ident, card_id)
        hop = 0
    embedded = first_rows is not None
    while True:
        last = None
        short = len(rows) < fetch.CARD_PAGE and not (hop == 0 and embedded)
        if rows and not short:
            last = (rows[-1].get("identifier") or {}).get("uuid") or rows[-1].get("uuid")
            if last:
                _card_cursors.store(memo_key, hop + 1, last)
        if hop == page - 1:
            return rows
        if not last:
            return []
        rows = fetch.card(collection, ident, card_id, after_id=last)
        hop += 1


def _card_row_uuid(row):
    return (row.get("identifier") or {}).get("uuid") or row.get("uuid")


def card_all(collection, ident, card_id, first_rows, total, max_rows):
    """Every row of a list card, up to `total` (the card's headline count)
    and never past `max_rows`: the rows the entity embedded, then the cards
    endpoint after the last one, CARD_PAGE rows a call. An empty hop, a hop
    of rows already held, or one shorter than a full cards page ends the
    walk. Only for entities outside the organization cookie quota — an
    organization card walk spends one quota unit per call."""
    rows = list(first_rows or [])
    seen = {_card_row_uuid(r) for r in rows}
    while len(rows) < min(int(total or 0), max_rows):
        after = _card_row_uuid(rows[-1]) if rows else None
        if rows and not after:
            break
        batch = fetch.card(collection, ident, card_id, after_id=after)
        fresh = [r for r in batch if _card_row_uuid(r) not in seen]
        if not fresh:
            break
        rows += fresh
        seen.update(_card_row_uuid(r) for r in fresh)
        if len(batch) < fetch.CARD_PAGE:
            break
    return rows[:max_rows]


def strip_cursor(query):
    return [{k: v for k, v in p.items() if k != "_cursor"} for p in query]


def one_row(collection, field_ids, uuid, row_parser):
    """The search row of one entity by uuid (fills the numbers the entity
    cards hide behind preview_properties)."""
    try:
        data = fetch.search(collection, field_ids, [predicate("uuid", "includes", [uuid])], [], limit=1)
    except fetch.CrunchbaseBadRequest:
        data = fetch.search(collection, field_ids, [predicate("identifier", "includes", [uuid])], [], limit=1)
    rows = data.get("entities") or []
    return row_parser(rows[0]) if rows else None

