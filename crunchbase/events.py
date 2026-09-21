"""/crunchbase/events/*, /crunchbase/press/search and /crunchbase/news/*:
industry events (search + entity + participants), the press-reference feed
(articles Crunchbase tagged to companies and people) and Crunchbase News
(news.crunchbase.com, an open WordPress site)."""
from crunchbase import fetch, parsers, refs, shared
from crunchbase.companies import APPEARANCE_FIELDS, PRESS_FIELDS
from crunchbase.shared import add, date_range, paged_search

EVENT_FIELDS = ["identifier", "short_description", "starts_on", "ends_on", "location_identifiers", "categories",
                "category_groups", "rank_event"]
NEWS_PAGE = 10


def search_events(query=None, starts_after=None, starts_before=None, location=None, category=None,
                  event_type=None, sort="starts", page=1):
    q = []
    add(q, "identifier", "contains", [query] if query else None)
    date_range(q, "starts_on", starts_after, starts_before)
    if location:
        add(q, "location_identifiers", "includes", [shared.location_uuid(location)])
    if category:
        add(q, "categories", "includes", [shared.category_uuid(category)])
    add(q, "event_type", "includes", [event_type] if event_type else None)
    field, direction = refs.EVENT_SORTS[sort]
    result, _ = paged_search("events", EVENT_FIELDS, q, field, direction, page, parsers.event_row)
    result["sort"] = sort
    return result


def event_details(event):
    ident = event["id"]
    data = fetch.entity("events", ident, ["identifier", "short_description"], layout="event",
                        missing=f"event '{ident}' not found on Crunchbase")
    return parsers.event(data)


def event_participants(event, role=None, page=1):
    uuid = shared.uuid_of("event", event)
    q = [fetch.predicate("event_identifier", "includes", [uuid])]
    add(q, "appearance_type", "includes", [role] if role else None)
    result, _ = paged_search("event_appearances", APPEARANCE_FIELDS, q, "identifier", "asc", page, parsers.appearance_row)
    result.update({"event": {"id": event["id"], "uuid": uuid}, "role": role})
    return result


def entity_events(entity, role=None, page=1):
    uuid, kind = shared.entity_uuid_any(entity)
    q = [fetch.predicate("participant_identifier", "includes", [uuid])]
    add(q, "appearance_type", "includes", [role] if role else None)
    result, _ = paged_search("event_appearances", APPEARANCE_FIELDS, q, "event_starts_on", "desc", page,
                             parsers.appearance_row)
    result.update({"entity": {"id": entity["id"], "uuid": uuid, "type": kind}, "role": role})
    return result


def search_press(query=None, entity=None, posted_after=None, posted_before=None, page=1):
    q = []
    add(q, "title", "contains", [query] if query else None)
    if entity:
        add(q, "activity_entities", "includes", [shared.entity_uuid_any(entity)[0]])
    date_range(q, "posted_on", posted_after, posted_before)
    result, _ = paged_search("press_references", PRESS_FIELDS, q, "posted_on", "desc", page, parsers.press_row)
    return result


def _news_page(params, page):
    params = {**params, "per_page": NEWS_PAGE, "page": page, "_embed": "author,wp:featuredmedia,wp:term"}
    try:
        posts, headers = fetch.news_json("/wp-json/wp/v2/posts", params)
    except fetch.CrunchbaseBadRequest:
        posts, headers = [], {}          # WordPress answers 400 past the last page
    total = headers.get("x-wp-total") or headers.get("X-WP-Total")
    total_pages = headers.get("x-wp-totalpages") or headers.get("X-WP-TotalPages")
    items = [parsers.news_post(p) for p in posts if isinstance(p, dict)]
    return {
        "items": items,
        "pagination": {"page": page, "items_per_page": NEWS_PAGE,
                       "total_pages": int(total_pages) if total_pages else (page if items else max(page - 1, 0)),
                       "total_count": int(total) if total else None},
    }


def latest_news(page=1):
    return _news_page({}, page)


def search_news(query=None, page=1):
    out = _news_page({"search": query} if query else {}, page)
    out["query"] = query
    return out
