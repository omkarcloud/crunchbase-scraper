"""Crunchbase reference parsing: ONE param per input that auto-detects its
forms (tripadvisor QueryOrLinkField convention — never a sibling `url` /
`id` pair), plus the vocabularies the routes share.

Every entity on Crunchbase has a permalink (slug) and a uuid, and the site
URL is https://www.crunchbase.com/<section>/<permalink>[/<tab>]. A ref
therefore accepts:

    stripe                                          bare permalink
    6f83ddd7-d637-61f8-06b2-438a0037605f            uuid
    https://www.crunchbase.com/organization/stripe  site link (any tab / query)
    crunchbase.com/organization/stripe              link without a scheme

and resolves to {"id": <permalink or uuid>, "kind": <entity kind from the
link, or None for a bare value>}. The link's section must match the
entity the endpoint wants (an /organization/ link on a person endpoint is
rejected), so a wrong paste fails as a 400 instead of a Crunchbase 404.

Vocabularies map the public values the API accepts to the enum tokens the
Crunchbase data model uses (`c_00011_00050` for 11-50 employees, …) and
back, so responses read as the public values.
"""
import re
import unicodedata
from urllib.parse import unquote, urlparse

SITE = "https://www.crunchbase.com"

# URL section -> entity kind (the collection the v4 API uses is in COLLECTIONS).
SECTIONS = {
    "organization": "organization",
    "person": "person",
    "funding_round": "funding_round",
    "acquisition": "acquisition",
    "ipo": "ipo",
    "fund": "fund",
    "event": "event",
    "hub": "hub",
    "location": "location",
    "category": "category",
    "investment": "investment",
    "ownership": "ownership",
    "job": "job",
    "press_reference": "press_reference",
}
COLLECTIONS = {
    "organization": "organizations",
    "person": "people",
    "funding_round": "funding_rounds",
    "acquisition": "acquisitions",
    "ipo": "ipos",
    "fund": "funds",
    "event": "events",
    "hub": "hubs",
    "location": "locations",
    "category": "categories",
    "category_group": "category_groups",
    "investment": "investments",
    "ownership": "ownerships",
    "job": "jobs",
    "press_reference": "press_references",
}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,200}$", re.I)


def is_uuid(value):
    return bool(_UUID_RE.match(value or ""))


def _parse_link(value):
    """(host, path segments) for a crunchbase link, else (None, None)."""
    raw = value.strip()
    if not raw.startswith(("http://", "https://", "//")):
        if re.match(r"^(www\.)?crunchbase\.com/", raw, re.I):
            raw = "https://" + raw
        else:
            return None, None
    url = raw if not raw.startswith("//") else "https:" + raw
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, [unquote(s) for s in parsed.path.split("/") if s]


def resolve_ref(value, kinds, what):
    """A permalink / uuid / site link for one of `kinds` -> {"id", "kind"}."""
    value = (value or "").strip()
    host, segs = _parse_link(value)
    if host is not None:
        if host != "crunchbase.com":
            raise ValueError(f"'{value}' is not a crunchbase.com link.")
        if len(segs) < 2 or SECTIONS.get(segs[0]) is None:
            raise ValueError(f"'{value}' is not a Crunchbase {what} link.")
        kind = SECTIONS[segs[0]]
        if kind not in kinds:
            raise ValueError(f"'{value}' is a {kind.replace('_', ' ')} link, not a {what}.")
        ident = segs[1].strip().lower()
        if not ident or not (is_uuid(ident) or _SLUG_RE.match(ident)):
            raise ValueError(f"'{value}' has no {what} permalink.")
        return {"id": ident, "kind": kind}
    ident = value.lower()
    if not ident or not (is_uuid(ident) or _SLUG_RE.match(ident)):
        raise ValueError(f"'{value}' is not a Crunchbase {what} permalink, uuid or link.")
    return {"id": ident, "kind": None}


def resolve_company(value):
    return resolve_ref(value, ("organization",), "company")


def resolve_entity_or_name(value):
    """resolve_entity, plus a plain company or person name ("Sequoia Capital",
    "Sam Altman"): {"id": None, "kind": None, "name": <name>}, looked up by
    shared.entity_uuid_any. A link is never read as a name."""
    try:
        return resolve_entity(value)
    except ValueError:
        name = " ".join((value or "").split())
        if not name or _parse_link(name)[0] is not None:
            raise
        return {"id": None, "kind": None, "name": name}


def resolve_company_or_name(value):
    """resolve_company, plus a plain company name ("Open AI", "Stripe, Inc."):
    {"id": None, "kind": "organization", "name": <name>}. The endpoint looks
    the name up (companies.details). A link is never read as a name."""
    try:
        return resolve_company(value)
    except ValueError:
        name = " ".join((value or "").split())
        # punctuation alone ("!!!") names nothing: no row could ever match it
        if not name_key(name) or _parse_link(name)[0] is not None:
            raise
        return {"id": None, "kind": "organization", "name": name}


def name_key(value):
    """A company name reduced to what two spellings of it share: lowercase
    letters and digits ("Open AI", "OpenAI" and "open-ai" are one key), in
    any script, accents folded ("Nestlé" and "Nestle" are one key). An
    ASCII-only key would be empty for "Яндекс" and make "Škoda" and "Koda"
    the same company."""
    text = unicodedata.normalize("NFKD", value or "").casefold()
    # U+0300-U+036F: the accents NFKD splits off a Latin / Greek / Cyrillic
    # letter. Every other mark (an Indic vowel sign, …) is part of the word.
    return "".join(ch for ch in text
                   if ch.isalnum() or (unicodedata.category(ch)[0] == "M" and not "̀" <= ch <= "ͯ"))


def resolve_person(value):
    return resolve_ref(value, ("person",), "person")


def resolve_entity(value):
    """Company OR person (investor endpoints, event participants)."""
    return resolve_ref(value, ("organization", "person"), "company or person")


def resolve_funding_round(value):
    return resolve_ref(value, ("funding_round",), "funding round")


def resolve_acquisition(value):
    return resolve_ref(value, ("acquisition",), "acquisition")


def resolve_ipo(value):
    return resolve_ref(value, ("ipo",), "IPO")


def resolve_fund(value):
    return resolve_ref(value, ("fund",), "fund")


def resolve_event(value):
    return resolve_ref(value, ("event",), "event")


def resolve_hub(value):
    return resolve_ref(value, ("hub",), "hub")


def resolve_location(value):
    return resolve_ref(value, ("location",), "location")


def resolve_category(value):
    return resolve_ref(value, ("category",), "category")


def link(kind, permalink):
    section = {v: k for k, v in SECTIONS.items()}.get(kind) or kind
    return f"{SITE}/{section}/{permalink}" if permalink else None


# ---- vocabularies -------------------------------------------------------------------
# public value -> Crunchbase enum token (responses map back with the *_LABELS dicts)

FUNDING_TYPES = {
    "pre_seed": "pre_seed", "seed": "seed", "angel": "angel", "series_a": "series_a",
    "series_b": "series_b", "series_c": "series_c", "series_d": "series_d", "series_e": "series_e",
    "series_f": "series_f", "series_g": "series_g", "series_h": "series_h", "series_i": "series_i",
    "series_j": "series_j", "series_unknown": "series_unknown", "private_equity": "private_equity",
    "debt_financing": "debt_financing", "convertible_note": "convertible_note", "grant": "grant",
    "corporate_round": "corporate_round", "equity_crowdfunding": "equity_crowdfunding",
    "product_crowdfunding": "product_crowdfunding", "secondary_market": "secondary_market",
    "post_ipo_equity": "post_ipo_equity", "post_ipo_debt": "post_ipo_debt",
    "post_ipo_secondary": "post_ipo_secondary", "non_equity_assistance": "non_equity_assistance",
    "initial_coin_offering": "initial_coin_offering", "undisclosed": "undisclosed",
}
FUNDING_STAGES = {
    "seed": "seed", "early_stage_venture": "early_stage_venture", "late_stage_venture": "late_stage_venture",
    "private_equity": "private_equity", "ipo": "ipo", "m_and_a": "m_and_a",
}
EMPLOYEE_BANDS = {
    "1-10": "c_00001_00010", "11-50": "c_00011_00050", "51-100": "c_00051_00100",
    "101-250": "c_00101_00250", "251-500": "c_00251_00500", "501-1000": "c_00501_01000",
    "1001-5000": "c_01001_05000", "5001-10000": "c_05001_10000", "10001+": "c_10001_max",
}
EMPLOYEE_LABELS = {v: k for k, v in EMPLOYEE_BANDS.items()}
REVENUE_LABELS = {
    "r_00000000": "less than $1M", "r_00001000": "$1M to $10M", "r_00010000": "$10M to $50M",
    "r_00050000": "$50M to $100M", "r_00100000": "$100M to $500M", "r_00500000": "$500M to $1B",
    "r_01000000": "$1B to $10B", "r_10000000": "$10B+",
}
OPERATING_STATUSES = ["active", "closed"]
IPO_STATUSES = ["public", "private", "delisted"]
COMPANY_TYPES = ["for_profit", "non_profit"]
INVESTOR_TYPES = {
    "accelerator": "accelerator", "angel_group": "angel_group", "co_working_space": "co_working_space",
    "corporate_venture_capital": "corporate_venture_capital", "entrepreneurship_program": "entrepreneurship_program",
    "family_investment_office": "family_investment_office", "fund_of_funds": "fund_of_funds",
    "government_office": "government_office", "hedge_fund": "hedge_fund", "incubator": "incubator",
    "investment_bank": "investment_bank", "investment_partner": "investment_partner",
    "micro_vc": "micro_vc", "pension_funds": "pension_funds", "private_equity_firm": "private_equity_firm",
    "secondary_purchaser": "secondary_purchaser", "startup_competition": "startup_competition",
    "syndicate": "syndicate", "university_program": "university_program", "venture_capital": "venture_capital",
    "venture_debt": "venture_debt", "angel": "angel", "individual": "individual",
}
INVESTOR_STAGES = {
    "seed": "seed", "early_stage_venture": "early_stage_venture", "late_stage_venture": "late_stage_venture",
    "private_equity": "private_equity", "debt": "debt", "convertible_note": "convertible_note",
    "grant": "grant", "crowdfunding": "crowdfunding", "secondary_market": "secondary_market",
    "initial_coin_offering": "initial_coin_offering", "post_ipo": "post_ipo", "non_equity_assistance": "non_equity_assistance",
}
GENDERS = ["male", "female", "non_binary", "other", "not_provided"]
ACQUISITION_TYPES = ["acquisition", "merge", "acquihire", "lbo", "management_buyout"]
APPEARANCE_TYPES = ["sponsor", "speaker", "exhibitor", "contestant", "organizer"]
EVENT_TYPES = ["conference", "meetup", "hackathon", "demo_day", "networking", "competition",
               "seminar", "webinar", "expo", "class", "festival", "other"]
LOCATION_TYPES = {"city": "location.cities", "region": "location.regions", "country": "location.countries",
                  "group": "location.groups"}
JOB_TYPES = ["employee", "executive", "board_member", "advisor", "board_observer"]
JOB_LEVELS = ["individual", "manager", "director", "vp", "exec"]
JOB_DEPARTMENTS = ["management", "engineering", "information_technology", "operations", "sales", "marketing",
                   "finance", "human_resources", "consulting", "support", "design", "legal", "education",
                   "medical", "business_development", "product", "research", "other"]
OWNERSHIP_TYPES = ["subsidiary", "division", "affiliated_company", "joint_venture", "investment_arm"]

# /autocomplete `type` -> collection_ids the site's typeahead uses
AUTOCOMPLETE_TYPES = {
    "all": "organizations,people",
    "company": "organization.companies",
    "investor": "principal.investors",
    "school": "organization.schools",
    "person": "people",
    "funding_round": "funding_rounds",
    "acquisition": "acquisitions",
    "ipo": "ipos",
    "fund": "funds",
    "event": "events",
    "hub": "hubs",
    "category": "categories",
    "category_group": "category_groups",
    "location": "locations",
    "city": "location.cities",
    "region": "location.regions",
    "country": "location.countries",
    "product": "products",
}

# Company search `sort` -> (field, default direction). Only fields the
# anonymous entitlement may sort on.
COMPANY_SORTS = {
    "rank": ("rank_org_company", "asc"),
    "rank_overall": ("rank_org", "asc"),
    "founded": ("founded_on", "desc"),
    "last_funding": ("last_funding_at", "desc"),
    "employees": ("num_employees_enum", "desc"),
    "name": ("identifier", "asc"),
    "growth_score": ("growth_score", "desc"),
    "heat_score": ("heat_score", "desc"),
    "went_public": ("went_public_on", "desc"),
    "exits": ("num_exits", "desc"),
}
INVESTOR_SORTS = {
    "rank": ("rank_principal_investor", "asc"),
    "investments": ("num_investments_funding_rounds", "desc"),
    "exits": ("num_exits", "desc"),
    "name": ("identifier", "asc"),
}
PERSON_SORTS = {
    "rank": ("rank_person", "asc"),
    "investments": ("num_investments_funding_rounds", "desc"),
    "partner_investments": ("num_partner_investments", "desc"),
    "exits": ("num_exits", "desc"),
    "name": ("identifier", "asc"),
}
ROUND_SORTS = {
    "announced": ("announced_on", "desc"),
    "amount": ("money_raised", "desc"),
    "rank": ("rank_funding_round", "asc"),
}
ACQUISITION_SORTS = {
    "announced": ("announced_on", "desc"),
    "price": ("price", "desc"),
    "rank": ("rank_acquisition", "asc"),
}
IPO_SORTS = {"went_public": ("went_public_on", "desc"), "rank": ("rank_ipo", "asc")}
FUND_SORTS = {"announced": ("announced_on", "desc"), "amount": ("money_raised", "desc")}
EVENT_SORTS = {"starts": ("starts_on", "desc"), "rank": ("rank_event", "asc")}
HUB_SORTS = {"rank": ("rank_hub", "asc"), "name": ("identifier", "asc")}
