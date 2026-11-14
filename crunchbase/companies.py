"""/crunchbase/companies/* endpoints: one organization entity fetch (every
card of the `organization` layout, ~90 cards) sliced into the profile
views, plus the deal listings that come from the search collections.

The entity fetch is the expensive call (one unit of the per-cookie
organization quota), so it is memoised in-process for ENTITY_TTL seconds
and every view of the same company reuses it; the response cache
(cache_config) sits on top per view. The numbers the anonymous entity
hides behind preview_properties (funding total, founded date, website,
rank, scores…) come from the company's own row in the organization search,
which is free of that quota — one POST, merged in by parsers.organization.

List cards return 10 rows per call and page with `after_id`; a cold page N
of an investors / people list therefore costs N-1 extra quota units (one
when pages 1..N-1 were read within shared.CURSOR_TTL: shared.card_pages
memoises the after_id each hop ends on), while funding rounds,
acquisitions, news and events are served by searches (15 rows,
cursor-walked, no quota).
"""
from crunchbase import fetch, parsers, refs, shared
from crunchbase.discover import COMPANY_FIELDS
from crunchbase.shared import add, paged_search

ENTITY_TTL = 6 * 3600
CARD_PAGE = fetch.CARD_PAGE
_entities = shared._TTLCache(ttl=ENTITY_TTL, max_size=400)
_rows = shared._TTLCache(ttl=ENTITY_TTL, max_size=2000)
_names = shared._TTLCache(ttl=ENTITY_TTL, max_size=2000)      # company name -> ref

# investor_identifiers comes back gated, but its preview placeholder carries
# the list's length: the round's investor count (parsers._investors_count).
# num_relationships is NOT an investor count (30 for a 16-investor round).
ROUND_FIELDS = ["identifier", "short_description", "announced_on", "closed_on", "funded_organization_identifier",
                "funded_organization_funding_total", "funded_organization_diversity_spotlights", "investment_type",
                "money_raised", "target_money_raised", "pre_money_valuation", "lead_investor_identifiers",
                "investor_identifiers", "rank_funding_round"]
ACQUISITION_FIELDS = ["identifier", "short_description", "announced_on", "completed_on", "acquirer_identifier",
                      "acquirer_categories", "acquirer_locations", "acquirer_funding_total", "acquiree_identifier",
                      "acquiree_categories", "acquiree_locations", "acquiree_funding_total",
                      "acquiree_last_funding_type", "price", "rank_acquisition"]
PRESS_FIELDS = ["identifier", "posted_on", "publisher", "url", "title", "activity_entities", "author", "thumbnail_url"]
APPEARANCE_FIELDS = ["identifier", "short_description", "event_identifier", "event_starts_on",
                     "participant_identifier", "appearance_type"]


# Asked from the entity by name. The ranks and aliases normally come with the
# company's row in the companies search (_row), but an organization that is
# not in that collection (an investment firm: Sequoia Capital) has no row
# there, and the entity hands them out all the same.
ENTITY_FIELDS = ["identifier", "short_description", "rank_delta_d90", "category_groups", "rank_org", "rank_org_company", "aliases"]


def _entity(company):
    """The full organization entity (cached) + its uuid."""
    ident = company["id"]
    hit = _entities.get(ident)
    if hit:
        return hit
    data = fetch.entity("organizations", ident, ENTITY_FIELDS,
                        layout="organization", missing=f"company '{ident}' not found on Crunchbase")
    uuid = ((data.get("properties") or {}).get("identifier") or {}).get("uuid")
    if uuid:
        shared._uuid_cache.put(("organization", ident), uuid)
    _entities.put(ident, data)
    if uuid and uuid != ident:
        _entities.put(uuid, data)
    return data


def _row(uuid):
    """The company's row in the organization search (the ungated numbers)."""
    hit = _rows.get(uuid)
    if hit is not None:
        return hit
    row = None
    try:
        data = fetch.search("organization.companies", COMPANY_FIELDS,
                            [fetch.predicate("uuid", "includes", [uuid])], [], limit=1)
        rows = data.get("entities") or []
        row = parsers.organization_row(rows[0]) if rows else None
    except fetch.CrunchbaseBadRequest:
        row = None
    except fetch.CrunchbaseUpstreamError as e:
        # transient: serve the entity alone but do not memoise the miss
        print(f"crunchbase: company row lookup failed ({e}); serving the entity alone")
        return {}
    _rows.put(uuid, row or {})
    return row or {}


def _uuid(data):
    return ((data.get("properties") or {}).get("identifier") or {}).get("uuid")


def _cards(company):
    data = _entity(company)
    return data, data.get("cards") or {}


def _card_page(company, data, card_id, page):
    """Page `page` of a list card: page 1 from the entity in hand, deeper
    pages from the cards endpoint (one call per hop)."""
    first = (data.get("cards") or {}).get(card_id) or []
    if page == 1:
        return first
    return shared.card_pages("organizations", company["id"], card_id, page, first_rows=first)


def _card_pagination(page, rows, total=None, first_len=None):
    """`first_len` = rows the entity embedded for page 1: image-list cards
    embed fewer than a cards page (8), and later pages hold CARD_PAGE each."""
    if total:
        total = int(total)
        if first_len and first_len < CARD_PAGE and total > first_len:
            total_pages = 1 + -(-(total - first_len) // CARD_PAGE)
        else:
            total_pages = -(-total // CARD_PAGE)
    else:
        total_pages = page + 1 if len(rows) == CARD_PAGE else (page if rows else max(page - 1, 0))
    return {"page": page, "items_per_page": CARD_PAGE, "total_pages": max(total_pages, page if rows else 0),
            "total_count": total}


# ---- profile views ---------------------------------------------------------------------

name_spellings = shared.name_spellings      # kept here: the company lookup's own names
pick_named = shared.pick_named


def _named(name):
    """A company name -> its ref ({"id": permalink, "kind"}), through the
    search typeahead (shared.find_named, no organization quota). Memoised."""
    key = refs.name_key(name)
    hit = _names.get(key)
    if hit:
        return hit
    ident = shared.find_named("organizations", name)
    if not ident:
        raise fetch.CrunchbaseNotFound(f"no company named '{name}' on Crunchbase")
    ref = {"id": ident.get("permalink") or ident.get("uuid"), "kind": "organization"}
    _names.put(key, ref)
    return ref


def details(company):
    """`company` is a permalink / uuid / link ref, or a name ({"id": None,
    "name": …}). A bare word that is no permalink ("tesla": the permalink
    is tesla-motors) is retried as a name before it is reported missing."""
    if company.get("id") is None:
        company = _named(company["name"])
    try:
        data = _entity(company)
    except fetch.CrunchbaseNotFound:
        if company.get("kind") or refs.is_uuid(company["id"]):     # a link, a uuid or a looked-up name: final
            raise
        try:
            named = _named(shared.slug_words(company["id"]) or company["id"])
        except fetch.CrunchbaseNotFound:
            named = None
        if not named or named["id"] == company["id"]:
            raise
        data = _entity(named)
    return parsers.organization(data, _row(_uuid(data)))


def funding(company, sort="announced", page=1):
    data = _entity(company)
    uuid = _uuid(data)
    cards = data.get("cards") or {}
    field, direction = refs.ROUND_SORTS[sort]
    result, _ = paged_search("funding_rounds", ROUND_FIELDS,
                             [fetch.predicate("funded_organization_identifier", "includes", [uuid])],
                             field, direction, page, parsers.funding_round_row)
    row = _row(uuid)
    result.update({
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "summary": {
            "total": row.get("funding_total"),
            "equity_total": row.get("equity_funding_total"),
            "stage": row.get("funding_stage"),
            "rounds_count": parsers._get(cards, "funding_rounds_headline", "num_funding_rounds"),
            "last_round_type": parsers._get(cards, "funding_rounds_summary", "last_funding_type"),
            "last_round_at": row.get("last_funding_at"),
            "investors_count": parsers._get(cards, "investors_headline", "num_investors"),
            "lead_investors_count": parsers._get(cards, "investors_headline", "num_lead_investors"),
        },
    })
    return result


def investors(company, page=1):
    data = _entity(company)
    rows = _card_page(company, data, "investors_list", page)
    cards = data.get("cards") or {}
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "investors_count": parsers._get(cards, "investors_headline", "num_investors"),
        "lead_investors_count": parsers._get(cards, "investors_headline", "num_lead_investors"),
        "items": [parsers.investment_row(r) for r in rows],
        "pagination": _card_pagination(page, rows, parsers._get(cards, "investors_headline", "num_investors")),
    }


def investments(company, page=1):
    data = _entity(company)
    rows = _card_page(company, data, "investments_list", page)
    cards = data.get("cards") or {}
    head = cards.get("investments_headline") or {}
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "investments_count": head.get("num_investments"),
        "lead_investments_count": head.get("num_lead_investments"),
        "portfolio_count": head.get("num_portfolio_organizations"),
        "exits": [parsers.ref(r) | {"short_description": r.get("short_description")} for r in cards.get("exits_image_list") or []] if page == 1 else None,
        "funds": [parsers.fund_row({"properties": r}) for r in cards.get("funds_list") or []] if page == 1 else None,
        "diversity_spotlight_investments": [parsers.investment_row(r) for r in cards.get("diversity_spotlight_investments_list") or []] if page == 1 else None,
        "items": [parsers.investment_row(r) for r in rows],
        "pagination": _card_pagination(page, rows, head.get("num_investments")),
    }


def acquisitions(company, sort="announced", page=1):
    data = _entity(company)
    uuid = _uuid(data)
    cards = data.get("cards") or {}
    field, direction = refs.ACQUISITION_SORTS[sort]
    result, _ = paged_search("acquisitions", ACQUISITION_FIELDS,
                             [fetch.predicate("acquirer_identifier", "includes", [uuid])],
                             field, direction, page, parsers.acquisition_search_row)
    result.update({
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "acquisitions_count": parsers._get(cards, "acquisitions_headline", "num_acquisitions"),
        "acquired_by": parsers._acquired_by(cards.get("acquired_by_fields")),
    })
    return result


_ROLE_CARDS = {
    "employees": "current_employees_image_list",
    "advisors": "current_advisors_image_list",
    "board": "current_advisors_image_list",
    "alumni": "alumni_image_list",
}


def people(company, role="employees", page=1):
    data = _entity(company)
    cards = data.get("cards") or {}
    card_id = _ROLE_CARDS[role]
    rows = _card_page(company, data, card_id, page)
    first_len = len(cards.get(card_id) or [])
    if role == "board":
        rows = [r for r in rows if r.get("job_type") == "board_member"]
    highlights = cards.get("people_highlights") or {}
    featured = {parsers._get(r, "identifier", "uuid"): r for r in cards.get("current_employees_featured_order_field") or []}
    items = []
    for r in rows:
        job = parsers.job_row(r)
        extra = featured.get(parsers._get(r, "identifier", "uuid"))
        if extra:
            job["started_on"] = job["started_on"] or parsers.date(extra.get("started_on"))
            job["featured_order"] = extra.get("employee_featured_order")
        items.append(job)
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "role": role,
        "employees_count": highlights.get("num_current_positions"),
        "advisors_count": highlights.get("num_current_advisor_positions"),
        "contacts_count": highlights.get("num_contacts"),
        "founders": parsers._founder(parsers._get(cards, "overview_fields_extended", "founder_identifiers")),
        "items": items,
        "pagination": _card_pagination(page, rows, highlights.get("num_current_positions") if role == "employees" else
                                       highlights.get("num_current_advisor_positions") if role in ("advisors", "board") else None,
                                       first_len),
    }


def subsidiaries(company):
    data, cards = _cards(company)
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "subsidiaries_count": parsers._get(cards, "sub_organizations_headline", "num_sub_organizations"),
        "subsidiaries": [{
            "id": parsers._get(r, "identifier", "permalink"),
            "company": parsers.ref(r.get("ownee_identifier")),
            "ownership_type": r.get("ownership_type"),
        } for r in cards.get("sub_organizations_image_list") or []],
        "acquired_by": parsers._acquired_by(cards.get("acquired_by_fields")),
        "parent": parsers.ref(parsers._get(cards, "acquired_by_fields", "acquirer_identifier")),
    }


def similar(company):
    data, cards = _cards(company)
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "similar_companies": [parsers.similar_row(r) for r in cards.get("org_similarity_list") or []],
        "similar_by_products": [parsers.ref(r) for r in cards.get("product_similarity_target_org_list") or []],
        "competitors": [parsers.ref(r) for r in cards.get("competitors_list") or []],
        "ranks_by_category": parsers._category_ranks(cards.get("org_category_ranks"),
                                                     parsers._get(cards, "company_about_fields2", "categories")),
    }


def technology(company):
    data, cards = _cards(company)
    highlights = cards.get("technology_highlights") or {}
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "web_traffic": {
            "visits_monthly": highlights.get("semrush_visits_latest_month"),
            "visits_change_monthly": parsers._get(cards, "semrush_rank_headline", "semrush_visits_mom_pct"),
            "updated_on": parsers._get(cards, "semrush_attribution", "semrush_last_updated"),
        },
        "technologies_count": highlights.get("builtwith_num_technologies_used"),
        "technologies": [parsers.technology_row(r) for r in cards.get("builtwith_tech_used_list") or []],
        "products_count": highlights.get("siftery_num_products"),
        "products_used": [parsers.product_row(r) for r in cards.get("siftery_product_list") or []],
        "apps_count": parsers._get(cards, "apptopia_summary", "apptopia_total_apps"),
        "app_downloads_monthly": highlights.get("apptopia_total_downloads"),
        "app_downloads_change_monthly": parsers._get(cards, "apptopia_app_headline", "apptopia_total_downloads_mom_pct"),
        "apps": [parsers.app_row(r) for r in cards.get("apptopia_app_overview_list_public") or []],
        "intellectual_property": {
            "patents_count": parsers._get(cards, "ipqwery_summary", "ipqwery_num_patent_granted"),
            "trademarks_count": parsers._get(cards, "ipqwery_summary", "ipqwery_num_trademark_registered"),
            "top_patent_category": parsers._get(cards, "ipqwery_summary", "ipqwery_popular_patent_category"),
            "top_trademark_class": parsers._get(cards, "ipqwery_summary", "ipqwery_popular_trademark_class"),
        },
        "it_spend": parsers.money(parsers._get(cards, "aberdeen_summary", "aberdeen_site_it_spend")),
        "buyer_intent": {
            "updated_on": parsers._get(cards, "bombora_attribution", "bombora_last_updated"),
            "topics": [parsers.intent_row(r) for r in cards.get("bombora_surge_list") or []],
        },
        "products": [{"name": p.get("name") or parsers._get(p, "identifier", "value"), "description": p.get("description")}
                     for p in cards.get("product") or []],
    }


def growth(company):
    data, cards = _cards(company)
    out = {"company": parsers.ref((data.get("properties") or {}).get("identifier"))}
    out.update(parsers.growth(cards))
    row = _row(_uuid(data))
    out["rank"] = {"overall": row.get("rank"), "company": row.get("rank_company"),
                   "delta_90d": (data.get("properties") or {}).get("rank_delta_d90")}
    return out


_SIGNAL_CARDS = {
    "layoffs": ("layoff_list", "layoff"),
    "key_employee_changes": ("key_employee_change_list", "key_employee_change"),
    "product_launches": ("product_launches", "product_launch"),
    "partnerships": ("partnership_announcements", "partnership_announcement"),
    "legal_proceedings": ("legal_proceedings", "legal_proceeding"),
    "awards": ("awards", "award"),
}


def signals(company, type=None):
    data, cards = _cards(company)
    kinds = [type] if type else list(_SIGNAL_CARDS)
    items = []
    for kind in kinds:
        card_id, event_kind = _SIGNAL_CARDS[kind]
        items += [parsers.key_event(r, event_kind) for r in cards.get(card_id) or []]
    items.sort(key=lambda e: e.get("date") or "", reverse=True)
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "type": type,
        "counts": {k: len(cards.get(v[0]) or []) for k, v in _SIGNAL_CARDS.items()},
        "items": items,
    }


def offices(company):
    data, cards = _cards(company)
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "headquarters": parsers.location(parsers._get(cards, "company_about_fields2", "location_identifiers")),
        "offices": [parsers.office_row(r) for r in cards.get("offices") or []],
    }


def timeline(company, page=1):
    """The press / activity timeline card (10 per page)."""
    data = _entity(company)
    cards = data.get("cards") or {}
    if page == 1:
        rows = parsers._get(cards, "overview_timeline", "entities") or []
    else:
        rows = shared.card_pages("organizations", company["id"], "overview_timeline", page,
                                 first_rows=parsers._get(cards, "overview_timeline", "entities") or [])
        if isinstance(rows, dict):
            rows = rows.get("entities") or []
    total = parsers._get(cards, "overview_timeline", "count")
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "items": [parsers.timeline_row(r) for r in rows],
        "pagination": _card_pagination(page, rows, total),
    }


def news(company, page=1):
    uuid = shared.uuid_of("organization", company)
    result, _ = paged_search("press_references", PRESS_FIELDS,
                             [fetch.predicate("activity_entities", "includes", [uuid])],
                             "posted_on", "desc", page, parsers.press_row)
    result["company"] = {"id": company["id"], "uuid": uuid}
    return result


def events(company, role=None, page=1):
    uuid = shared.uuid_of("organization", company)
    q = [fetch.predicate("participant_identifier", "includes", [uuid])]
    add(q, "appearance_type", "includes", [role] if role else None)
    result, _ = paged_search("event_appearances", APPEARANCE_FIELDS, q, "event_starts_on", "desc", page,
                             parsers.appearance_row)
    result["company"] = {"id": company["id"], "uuid": uuid}
    return result


def hubs(company):
    data, cards = _cards(company)
    ext = cards.get("overview_fields_extended") or {}
    return {
        "company": parsers.ref((data.get("properties") or {}).get("identifier")),
        "hub_tags": ext.get("hub_tags") or [],
        "hubs": [{"id": h.get("permalink"), "name": h.get("value"), "link": refs.link("hub", h.get("permalink"))}
                 for h in ext.get("hubs") or []],
        "related_lists": [{
            "id": parsers._get(r, "identifier", "permalink"),
            "name": parsers._get(r, "identifier", "value"),
            "link": refs.link("hub", parsers._get(r, "identifier", "permalink")),
            "companies": parsers.refs_list(r.get("org_identifiers")),
        } for r in cards.get("recommended_search") or []],
    }
