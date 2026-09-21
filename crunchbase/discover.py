"""/crunchbase/* discovery endpoints: autocomplete, company / investor /
people / school search, categories, locations, hubs.

Every function takes the validated kwargs of its schema (schemas.py) and
returns a JSON-able dict; paged ones return a `pagination` block that
route_glue lifts into the gateway shape.
"""
from crunchbase import fetch, parsers, refs, shared
from crunchbase.shared import add, date_range, money_range, paged_search

# The anonymous field set of the organization search (everything the
# entitlement lets us select; the rest comes from the entity endpoint).
COMPANY_FIELDS = [
    "identifier", "short_description", "categories", "category_groups", "location_identifiers",
    "rank_org_company", "rank_org", "num_employees_enum", "funding_total", "funding_stage",
    "last_funding_type", "last_funding_at", "last_equity_funding_total", "founded_on", "closed_on",
    "website_url", "ipo_status", "investor_identifiers", "founder_identifiers", "growth_score", "heat_score",
    "acquirer_identifier", "aliases", "num_exits", "investor_type", "investor_stage", "facet_ids",
    "num_contacts", "exited_on", "stock_exchange_symbol", "num_diversity_spotlight_investments",
    "went_public_on", "delisted_on",
]
INVESTOR_FIELDS = [
    "identifier", "short_description", "investor_type", "investor_stage", "num_exits", "location_identifiers",
    "rank_principal_investor", "facet_ids", "categories", "num_investments_funding_rounds", "last_funding_type",
    "founded_on", "num_exits_ipo", "num_diversity_spotlight_investments",
]
PERSON_FIELDS = [
    "identifier", "short_description", "primary_job_title", "primary_organization", "location_identifiers",
    "rank_person", "num_partner_investments", "num_exits", "investor_type", "investor_stage", "facet_ids",
    "aliases", "num_diversity_spotlight_investments", "num_exits_ipo", "num_investments_funding_rounds",
]
SCHOOL_FIELDS = ["identifier", "short_description", "categories", "location_identifiers", "rank_org_school", "founded_on"]
HUB_FIELDS = ["identifier", "short_description", "org_num", "person_num", "event_num", "rank_hub"]


def autocomplete(query, type="all", limit=10):
    collection = refs.AUTOCOMPLETE_TYPES[type]
    data = fetch.autocomplete(query, collection, limit=limit)
    items = [x for x in (parsers.autocomplete_row(r) for r in data.get("entities") or []) if x]
    return {"query": query, "type": type, "total_count": data.get("count"), "items": items}


def _geo_and_category(query, category=None, category_group=None, location=None,
                      cat_field="categories", group_field="category_groups", loc_field="location_identifiers"):
    if category:
        add(query, cat_field, "includes", [shared.category_uuid(category)])
    if category_group:
        add(query, group_field, "includes", [shared.category_group_uuid(category_group)])
    if location:
        add(query, loc_field, "includes", [shared.location_uuid(location)])


def search_companies(query=None, description=None, category=None, category_group=None, location=None,
                     founded_after=None, founded_before=None, closed_after=None, funding_stage=None,
                     last_funding_type=None, last_funding_after=None, last_funding_before=None,
                     employee_count=None, operating_status=None, ipo_status=None, company_type=None,
                     min_funding_total=None, max_funding_total=None, min_funding_rounds=None, min_investors=None,
                     min_lead_investors=None, min_acquisitions=None, min_exits=None, min_articles=None,
                     min_employees_listed=None, min_contacts=None, min_growth_score=None, min_heat_score=None,
                     investor=None, founder=None, hub_tag=None, investor_type=None, investor_stage=None,
                     stock_exchange=None, sort="rank", page=1):
    q = []
    add(q, "identifier", "starts", [query] if query else None)
    add(q, "description", "contains", [description] if description else None)
    _geo_and_category(q, category, category_group, location)
    date_range(q, "founded_on", founded_after, founded_before)
    add(q, "closed_on", "gte", [closed_after] if closed_after else None)
    add(q, "funding_stage", "includes", [funding_stage] if funding_stage else None)
    add(q, "last_funding_type", "includes", [last_funding_type] if last_funding_type else None)
    date_range(q, "last_funding_at", last_funding_after, last_funding_before)
    add(q, "num_employees_enum", "includes", [refs.EMPLOYEE_BANDS[b] for b in employee_count] if employee_count else None)
    add(q, "operating_status", "includes", [operating_status] if operating_status else None)
    add(q, "ipo_status", "includes", [ipo_status] if ipo_status else None)
    add(q, "company_type", "includes", [company_type] if company_type else None)
    money_range(q, "funding_total", min_funding_total, max_funding_total)
    for field, value in (("num_funding_rounds", min_funding_rounds), ("num_investors", min_investors),
                         ("num_lead_investors", min_lead_investors), ("num_acquisitions", min_acquisitions),
                         ("num_exits", min_exits), ("num_articles", min_articles),
                         ("num_current_positions", min_employees_listed), ("num_contacts", min_contacts),
                         ("growth_score", min_growth_score), ("heat_score", min_heat_score)):
        add(q, field, "gte", [value] if value is not None else None)
    if investor:
        add(q, "investor_identifiers", "includes", [shared.entity_uuid_any(investor)[0]])
    if founder:
        add(q, "founder_identifiers", "includes", [shared.uuid_of("person", founder)])
    add(q, "hub_tags", "includes", [hub_tag.lower().replace(" ", "_")] if hub_tag else None)
    add(q, "investor_type", "includes", [investor_type] if investor_type else None)
    add(q, "investor_stage", "includes", [investor_stage] if investor_stage else None)
    add(q, "stock_exchange_symbol", "includes", [stock_exchange.lower()] if stock_exchange else None)
    field, direction = refs.COMPANY_SORTS[sort]
    result, _ = paged_search("organization.companies", COMPANY_FIELDS, q, field, direction, page,
                             parsers.organization_summary)
    result["sort"] = sort
    return result


def company_by_domain(domain):
    domain = domain.lower().strip()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    data = fetch.search("organization.companies", COMPANY_FIELDS,
                        [fetch.predicate("website_url", "domain_eq", [domain])],
                        [{"field_id": "rank_org_company", "sort": "asc"}])
    items = [parsers.organization_summary(r) for r in data.get("entities") or []]
    return {"domain": domain, "total_count": data.get("count"), "items": items}


def search_investors(query=None, investor_type=None, investor_stage=None, location=None, category=None,
                     min_investments=None, min_exits=None, sort="rank", page=1):
    q = []
    add(q, "identifier", "contains", [query] if query else None)
    add(q, "investor_type", "includes", [investor_type] if investor_type else None)
    add(q, "investor_stage", "includes", [investor_stage] if investor_stage else None)
    _geo_and_category(q, category, None, location)
    add(q, "num_investments_funding_rounds", "gte", [min_investments] if min_investments else None)
    add(q, "num_exits", "gte", [min_exits] if min_exits else None)
    field, direction = refs.INVESTOR_SORTS[sort]
    result, _ = paged_search("principal.investors", INVESTOR_FIELDS, q, field, direction, page, _investor_summary)
    result["sort"] = sort
    return result


def _investor_summary(row):
    out = parsers.investor_row(row)
    out.pop("facets", None)
    return out


def search_people(query=None, company=None, title=None, bio=None, location=None, gender=None, is_investor=None,
                  investor_type=None, min_investments=None, min_partner_investments=None, min_exits=None,
                  min_founded=None, born_after=None, born_before=None, max_rank=None, sort="rank", page=1):
    q = []
    add(q, "identifier", "contains", [query] if query else None)
    if company:
        add(q, "primary_organization", "includes", [shared.uuid_of("organization", company)])
    add(q, "primary_job_title", "contains", [title] if title else None)
    add(q, "description", "contains", [bio] if bio else None)
    _geo_and_category(q, None, None, location)
    add(q, "gender", "includes", [gender] if gender else None)
    if is_investor is True:
        add(q, "facet_ids", "includes", ["investor"])
    elif is_investor is False:
        add(q, "facet_ids", "not_includes", ["investor"])
    add(q, "investor_type", "includes", [investor_type] if investor_type else None)
    add(q, "num_investments", "gte", [min_investments] if min_investments else None)
    add(q, "num_partner_investments", "gte", [min_partner_investments] if min_partner_investments else None)
    add(q, "num_exits", "gte", [min_exits] if min_exits else None)
    add(q, "num_founded_organizations", "gte", [min_founded] if min_founded else None)
    date_range(q, "born_on", born_after, born_before)
    add(q, "rank_person", "lte", [max_rank] if max_rank else None)
    field, direction = refs.PERSON_SORTS[sort]
    result, _ = paged_search("people", PERSON_FIELDS, q, field, direction, page, parsers.person_summary)
    result["sort"] = sort
    return result


def search_schools(query=None, location=None, founded_after=None, page=1):
    q = []
    add(q, "identifier", "contains", [query] if query else None)
    _geo_and_category(q, None, None, location)
    add(q, "founded_on", "gte", [founded_after] if founded_after else None)
    result, _ = paged_search("organization.schools", SCHOOL_FIELDS, q, "rank_org_school", "asc", page, _school_summary)
    return result


def _school_summary(row):
    out = parsers.organization_summary(row)
    out["rank"] = (row.get("properties") or {}).get("rank_org_school")
    return {k: out[k] for k in ("id", "uuid", "name", "link", "image", "short_description", "categories",
                                 "location", "founded_on", "rank")}


def search_categories(query, limit=15):
    data = fetch.autocomplete(query, "categories,category_groups", limit=limit)
    items = [x for x in (parsers.category_row(r) for r in data.get("entities") or []) if x]
    return {"query": query, "total_count": data.get("count"), "items": items}


def search_locations(query, type=None, limit=15):
    collection = refs.LOCATION_TYPES.get(type, "locations")
    data = fetch.autocomplete(query, collection, limit=limit)
    items = [x for x in (parsers.location_row(r) for r in data.get("entities") or []) if x]
    return {"query": query, "type": type, "total_count": data.get("count"), "items": items}


def search_hubs(query=None, sort="rank", page=1):
    q = []
    add(q, "identifier", "contains", [query] if query else None)
    field, direction = refs.HUB_SORTS[sort]
    result, _ = paged_search("hubs", HUB_FIELDS, q, field, direction, page, parsers.hub_row)
    result["sort"] = sort
    return result


def hub_details(hub):
    ident = hub["id"]
    data = fetch.entity("hubs", ident, ["identifier", "short_description"], layout="hub",
                        missing=f"hub '{ident}' not found on Crunchbase")
    return parsers.hub(data)
