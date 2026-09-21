"""Helpers shared by the /crunchbase/* endpoint modules: reference ->
uuid resolution (the searches filter on uuids), name -> uuid lookups for
categories / locations, predicate builders, and the cursor-emulated
pagination the anonymous search entitlement allows.

Pagination: Crunchbase refuses `after_id` paging anonymously (MD403
"insufficient permissions to paginate") and caps every search at 15 rows,
but filtering on the sort field itself walks pages exactly — sort by
rank_org_company ascending, then ask for rank > the last row's rank; sort
by announced_on descending, then ask for announced_on <= the last date and
skip the rows already seen. `paged_search` runs that walk: page N costs N
search calls (no organization quota involved).
"""
import threading
import time

from crunchbase import fetch, refs
from crunchbase.fetch import predicate

PAGE_SIZE = fetch.SEARCH_LIMIT
MAX_WALK = 40           # deepest page reachable by cursor walking


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
    returned as is; a permalink is looked up through autocomplete (free)
    and, when autocomplete does not list it, through the entity endpoint
    (one organization-quota unit for companies)."""
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
            data = fetch.autocomplete(words or ident, coll, limit=25)
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


def entity_uuid_any(ref):
    """A company-or-person ref without a link: try organization, then person."""
    kind = ref.get("kind") if isinstance(ref, dict) else None
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
    raise fetch.CrunchbaseNotFound(f"'{ident}' is not a company or person on Crunchbase")


def lookup_uuid(collection_ids, value, kind_label):
    """Category / location given as a permalink, uuid or name -> uuid via
    autocomplete (exact permalink match first, then the top hit)."""
    if refs.is_uuid(value):
        return value
    key = (collection_ids, value.lower())
    hit = _name_cache.get(key)
    if hit:
        return hit
    data = fetch.autocomplete(value.replace("-", " "), collection_ids, limit=25)
    rows = (data or {}).get("entities") or []
    found = None
    for row in rows:
        ent = row.get("identifier") or {}
        if (ent.get("permalink") or "").lower() == value.lower() or (ent.get("value") or "").lower() == value.lower():
            found = ent.get("uuid")
            break
    if not found and rows:
        found = (rows[0].get("identifier") or {}).get("uuid")
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
    stall the walk; it is then stepped past with the strict operator."""
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

    def run(cur):
        q = base + ([predicate(cursor_field, cur[0], [cur[1]])] if cur else [])
        return fetch.search(collection, fields, q, order, limit=PAGE_SIZE)

    for hop in range(page):
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
        if hop == page - 1 or len(batch) < PAGE_SIZE:
            if hop < page - 1:
                rows = []
            break
        if not rows or not walkable:
            rows = []
            break
        last = _cursor_value(rows[-1], cursor_field)
        if ties:
            seen |= {r.get("uuid") for r in rows}
            cursor = (tie_op, last)
        else:
            cursor = (strict_op, last)
    items = [x for x in (row_parser(r) for r in rows) if x]
    reachable = 1 if (cursor_field == "identifier" or not walkable) else MAX_WALK
    return {"items": items, "pagination": pagination(page, total, has_items=bool(items), reachable=reachable)}, rows


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

