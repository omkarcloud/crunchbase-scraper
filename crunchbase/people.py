"""/crunchbase/people/* endpoints: the person entity (30 cards of the
`person` layout: jobs, board roles, education, investments, exits, event
appearances, hubs, news) plus the person's row in the people search for
the gated numbers. People are NOT under the organization cookie quota."""
from crunchbase import fetch, parsers, shared
from crunchbase.companies import APPEARANCE_FIELDS, PRESS_FIELDS, _card_pagination
from crunchbase.discover import PERSON_FIELDS
from crunchbase.shared import add, paged_search

_entities = shared._TTLCache(ttl=6 * 3600, max_size=400)


def _entity(person):
    ident = person["id"]
    hit = _entities.get(ident)
    if hit:
        return hit
    data = fetch.entity("people", ident, ["identifier", "short_description", "facet_ids"], layout="person",
                        missing=f"person '{ident}' not found on Crunchbase")
    uuid = ((data.get("properties") or {}).get("identifier") or {}).get("uuid")
    if uuid:
        shared._uuid_cache.put(("person", ident), uuid)
    _entities.put(ident, data)
    return data


def _uuid(data):
    return ((data.get("properties") or {}).get("identifier") or {}).get("uuid")


def _row(uuid):
    try:
        data = fetch.search("people", PERSON_FIELDS, [fetch.predicate("uuid", "includes", [uuid])], [], limit=1)
        rows = data.get("entities") or []
        return parsers.person_row(rows[0]) if rows else {}
    except fetch.CrunchbaseBadRequest:
        return {}
    except fetch.CrunchbaseUpstreamError as e:
        print(f"crunchbase: person row lookup failed ({e}); serving the entity alone")
        return {}


def details(person):
    data = _entity(person)
    return parsers.person(data, _row(_uuid(data)))


def jobs(person, kind="all"):
    data = _entity(person)
    cards = data.get("cards") or {}
    current = [parsers.job_row(j) for j in cards.get("current_jobs_image_list") or []]
    past = [parsers.job_row(j) for j in cards.get("past_jobs_list") or []]
    board = [parsers.job_row(j) for j in (cards.get("current_board_and_advisory_roles_image_list") or [])
             + (cards.get("past_board_and_advisory_roles_list") or [])]
    out = {"person": parsers.ref((data.get("properties") or {}).get("identifier")), "kind": kind}
    if kind in ("current", "all"):
        out["current_jobs"] = current
    if kind in ("past", "all"):
        out["past_jobs"] = past
    if kind in ("board", "all"):
        out["board_and_advisor_roles"] = board
    out["counts"] = {
        "current": parsers._get(cards, "jobs_headline", "num_current_jobs"),
        "past": parsers._get(cards, "jobs_headline", "num_past_jobs"),
        "current_advisor": parsers._get(cards, "board_and_advisory_roles_headline", "num_current_advisor_jobs"),
        "past_advisor": parsers._get(cards, "board_and_advisory_roles_headline", "num_past_advisor_jobs"),
    }
    out["education"] = [parsers.degree_row(d) for d in cards.get("education_image_list") or []]
    return out


def investments(person, page=1):
    data = _entity(person)
    cards = data.get("cards") or {}
    first = cards.get("investments_list") or []
    rows = first if page == 1 else shared.card_pages("people", person["id"], "investments_list", page, first_rows=first)
    inv = cards.get("investor_overview_headline") or {}
    return {
        "person": parsers.ref((data.get("properties") or {}).get("identifier")),
        "investments_count": parsers._get(cards, "investments_headline", "num_investments"),
        "partner_investments_count": parsers._get(cards, "partner_investments_headline", "num_partner_investments"),
        "portfolio_count": inv.get("num_portfolio_organizations"),
        "founded_companies_count": inv.get("num_founded_organizations"),
        "investor_rank": inv.get("rank_principal_investor"),
        "partner_investments": [parsers.investment_row(r) for r in cards.get("partner_investments_list") or []] if page == 1 else None,
        "exits": [parsers.ref(r) | {"short_description": r.get("short_description")} for r in cards.get("exits_image_list") or []] if page == 1 else None,
        "items": [parsers.investment_row(r) for r in rows],
        "pagination": _card_pagination(page, rows, parsers._get(cards, "investments_headline", "num_investments")),
    }


def news(person, page=1):
    uuid = shared.uuid_of("person", person)
    result, _ = paged_search("press_references", PRESS_FIELDS,
                             [fetch.predicate("activity_entities", "includes", [uuid])],
                             "posted_on", "desc", page, parsers.press_row)
    result["person"] = {"id": person["id"], "uuid": uuid}
    return result


def events(person, role=None, page=1):
    uuid = shared.uuid_of("person", person)
    q = [fetch.predicate("participant_identifier", "includes", [uuid])]
    add(q, "appearance_type", "includes", [role] if role else None)
    result, _ = paged_search("event_appearances", APPEARANCE_FIELDS, q, "event_starts_on", "desc", page,
                             parsers.appearance_row)
    result["person"] = {"id": person["id"], "uuid": uuid}
    return result
