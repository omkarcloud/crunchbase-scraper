"""/crunchbase/funding-rounds, acquisitions, investments, ipos, funds:
the deal collections (searches, cursor-walked) and their entity pages.
None of these touch the organization cookie quota."""
from crunchbase import fetch, parsers, refs, shared
from crunchbase.companies import ACQUISITION_FIELDS, ROUND_FIELDS
from crunchbase.shared import add, date_range, money_range, paged_search

INVESTMENT_FIELDS = ["identifier", "announced_on", "investor_type", "organization_diversity_spotlights",
                     "funding_round_investment_type", "funding_round_money_raised", "is_lead_investor", "money_invested"]
IPO_FIELDS = ["identifier", "short_description", "went_public_on", "delisted_on", "organization_identifier",
              "stock_exchange_symbol", "stock_symbol", "valuation", "share_price", "rank_ipo"]
FUND_FIELDS = ["identifier", "short_description", "announced_on", "money_raised", "owner_identifier",
               "investor_identifiers", "num_investors"]
# Deepest a round's investor list is walked: the entity embeds the first
# fetch.CARD_PAGE (10) investments, each further cards call adds 10.
ROUND_INVESTORS_MAX = 300


def _one(collection, fields, uuid, parser):
    try:
        data = fetch.search(collection, fields, [fetch.predicate("uuid", "includes", [uuid])], [], limit=1)
        rows = data.get("entities") or []
        return parser(rows[0]) if rows else None
    except (fetch.CrunchbaseBadRequest, fetch.CrunchbaseUpstreamError):
        return None


# ---- funding rounds --------------------------------------------------------------------

def search_funding_rounds(company=None, investor=None, lead_investor=None, category=None, location=None,
                          round_type=None, funding_stage=None, announced_after=None, announced_before=None,
                          min_amount=None, max_amount=None, min_investors=None, equity_only=None,
                          sort="announced", page=1):
    q = []
    if company:
        add(q, "funded_organization_identifier", "includes", [shared.uuid_of("organization", company)])
    if investor:
        add(q, "investor_identifiers", "includes", [shared.entity_uuid_any(investor)[0]])
    if lead_investor:
        add(q, "lead_investor_identifiers", "includes", [shared.entity_uuid_any(lead_investor)[0]])
    if category:
        add(q, "funded_organization_categories", "includes", [shared.category_uuid(category)])
    if location:
        add(q, "funded_organization_location", "includes", [shared.location_uuid(location)])
    add(q, "investment_type", "includes", round_type or None)
    add(q, "funded_organization_funding_stage", "includes", [funding_stage] if funding_stage else None)
    date_range(q, "announced_on", announced_after, announced_before)
    money_range(q, "money_raised", min_amount, max_amount)
    add(q, "num_investors", "gte", [min_investors] if min_investors else None)
    if equity_only is True:
        add(q, "is_equity", "eq", [True])
    field, direction = refs.ROUND_SORTS[sort]
    result, _ = paged_search("funding_rounds", ROUND_FIELDS, q, field, direction, page, parsers.funding_round_row)
    result["sort"] = sort
    return result


def funding_round_details(funding_round):
    ident = funding_round["id"]
    data = fetch.entity("funding_rounds", ident, ["identifier", "short_description"], layout="funding_round",
                        missing=f"funding round '{ident}' not found on Crunchbase")
    uuid = ((data.get("properties") or {}).get("identifier") or {}).get("uuid")
    return parsers.funding_round(data, _one("funding_rounds", ROUND_FIELDS, uuid, parsers.funding_round_row) if uuid else None,
                                 investments=_round_investments(uuid or ident, data))


def _round_investments(ident, data):
    """Every investment of the round. The entity's investors_list card holds
    only the first 10 (a 16-investor seed round came back with 10); the rest
    page through the cards endpoint, which funding rounds serve outside the
    organization cookie quota. A rejected cards call (MD403 / MD101) falls
    back to the embedded first page; a block or upstream failure raises, so
    a transient short list is never served (or cached) as the whole round."""
    cards = data.get("cards") or {}
    first = cards.get("investors_list") or []
    total = parsers._get(cards, "investors_headline", "num_investors")
    try:
        return shared.card_all("funding_rounds", ident, "investors_list", first, total, ROUND_INVESTORS_MAX)
    except fetch.CrunchbaseBadRequest as e:
        print(f"crunchbase: round {ident} investors past the first page refused ({e}); serving {len(first)} of {total}")
        return first


# ---- acquisitions ----------------------------------------------------------------------

def search_acquisitions(acquirer=None, acquiree=None, category=None, location=None, acquisition_type=None,
                        announced_after=None, announced_before=None, completed_after=None, min_price=None,
                        max_price=None, acquiree_last_funding_type=None, sort="announced", page=1):
    q = []
    if acquirer:
        add(q, "acquirer_identifier", "includes", [shared.uuid_of("organization", acquirer)])
    if acquiree:
        add(q, "acquiree_identifier", "includes", [shared.uuid_of("organization", acquiree)])
    if category:
        add(q, "acquiree_categories", "includes", [shared.category_uuid(category)])
    if location:
        add(q, "acquiree_locations", "includes", [shared.location_uuid(location)])
    add(q, "acquisition_type", "includes", [acquisition_type] if acquisition_type else None)
    date_range(q, "announced_on", announced_after, announced_before)
    add(q, "completed_on", "gte", [completed_after] if completed_after else None)
    money_range(q, "price", min_price, max_price)
    add(q, "acquiree_last_funding_type", "includes", [acquiree_last_funding_type] if acquiree_last_funding_type else None)
    field, direction = refs.ACQUISITION_SORTS[sort]
    result, _ = paged_search("acquisitions", ACQUISITION_FIELDS, q, field, direction, page, parsers.acquisition_search_row)
    result["sort"] = sort
    return result


def acquisition_details(acquisition):
    ident = acquisition["id"]
    data = fetch.entity("acquisitions", ident, ["identifier", "short_description"], layout="acquisition",
                        missing=f"acquisition '{ident}' not found on Crunchbase")
    uuid = ((data.get("properties") or {}).get("identifier") or {}).get("uuid")
    return parsers.acquisition(data, _one("acquisitions", ACQUISITION_FIELDS, uuid, parsers.acquisition_search_row) if uuid else None)


# ---- investments -------------------------------------------------------------------------

def search_investments(company, funding_round=None, round_type=None, lead_only=None, page=1):
    q = [fetch.predicate("organization_identifier", "includes", [shared.uuid_of("organization", company)])]
    if funding_round:
        add(q, "funding_round_identifier", "includes", [shared.uuid_of("funding_round", funding_round)])
    add(q, "funding_round_investment_type", "includes", [round_type] if round_type else None)
    if lead_only is True:
        add(q, "is_lead_investor", "eq", [True])
    result, _ = paged_search("investments", INVESTMENT_FIELDS, q, "identifier", "asc", page, parsers.investment_search_row)
    return result


# ---- IPOs ---------------------------------------------------------------------------------

def search_ipos(stock_exchange=None, stock_symbol=None, went_public_after=None, went_public_before=None,
                sort="went_public", page=1):
    q = []
    add(q, "stock_exchange_symbol", "includes", [stock_exchange.lower()] if stock_exchange else None)
    add(q, "stock_symbol", "eq", [stock_symbol.upper()] if stock_symbol else None)
    date_range(q, "went_public_on", went_public_after, went_public_before)
    field, direction = refs.IPO_SORTS[sort]
    result, _ = paged_search("ipos", IPO_FIELDS, q, field, direction, page, parsers.ipo_row)
    result["sort"] = sort
    return result


def ipo_details(ipo):
    ident = ipo["id"]
    data = fetch.entity("ipos", ident, ["identifier", "short_description"], layout="ipo",
                        missing=f"IPO '{ident}' not found on Crunchbase")
    uuid = ((data.get("properties") or {}).get("identifier") or {}).get("uuid")
    return parsers.ipo(data, _one("ipos", IPO_FIELDS, uuid, parsers.ipo_row) if uuid else None)


# ---- funds --------------------------------------------------------------------------------

def search_funds(owner=None, announced_after=None, announced_before=None, min_amount=None, sort="announced", page=1):
    q = []
    if owner:
        add(q, "owner_identifier", "includes", [shared.uuid_of("organization", owner)])
    date_range(q, "announced_on", announced_after, announced_before)
    money_range(q, "money_raised", min_amount, None)
    field, direction = refs.FUND_SORTS[sort]
    result, _ = paged_search("funds", FUND_FIELDS, q, field, direction, page, parsers.fund_row)
    result["sort"] = sort
    return result


def fund_details(fund):
    ident = fund["id"]
    data = fetch.entity("funds", ident, ["identifier", "short_description"], layout="fund",
                        missing=f"fund '{ident}' not found on Crunchbase")
    uuid = ((data.get("properties") or {}).get("identifier") or {}).get("uuid")
    return parsers.fund(data, _one("funds", FUND_FIELDS, uuid, parsers.fund_row) if uuid else None)
