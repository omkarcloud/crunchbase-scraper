"""Offline tests: refs resolvers, schemas and the parsers against captured
v4 API payloads (crunchbase/fixtures/*.json.gz, recorded 2026-09-21).

    python -m pytest crunchbase/test_parsers.py -q
"""
import gzip
import json
import os

import pytest

from schema_fields import load_query

from crunchbase import parsers, refs, schemas, shared

FX = os.path.join(os.path.dirname(__file__), "fixtures")


def fx(name):
    with gzip.open(os.path.join(FX, name + ".json.gz"), "rt", encoding="utf-8") as f:
        return json.load(f)


# ---- refs + schemas ------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("stripe", {"id": "stripe", "kind": None}),
    ("Stripe", {"id": "stripe", "kind": None}),
    ("6f83ddd7-d637-61f8-06b2-438a0037605f", {"id": "6f83ddd7-d637-61f8-06b2-438a0037605f", "kind": None}),
    ("https://www.crunchbase.com/organization/stripe", {"id": "stripe", "kind": "organization"}),
    ("https://www.crunchbase.com/organization/stripe/company_financials?x=1", {"id": "stripe", "kind": "organization"}),
    ("crunchbase.com/organization/stripe", {"id": "stripe", "kind": "organization"}),
])
def test_resolve_company(value, expected):
    assert refs.resolve_company(value) == expected


@pytest.mark.parametrize("value", [
    "https://www.crunchbase.com/person/sam-altman",      # wrong entity
    "https://www.linkedin.com/company/stripe",           # wrong site
    "https://www.crunchbase.com/organization/",          # no permalink
    "stripe inc",                                        # not a permalink
    "",
])
def test_resolve_company_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_company(value)


def test_resolve_entity_and_others():
    assert refs.resolve_entity("https://www.crunchbase.com/person/sam-altman") == {"id": "sam-altman", "kind": "person"}
    assert refs.resolve_entity("sequoia-capital")["kind"] is None
    assert refs.resolve_funding_round("https://www.crunchbase.com/funding_round/stripe-series-i--b7a5d9a3")["id"] == "stripe-series-i--b7a5d9a3"
    assert refs.resolve_hub("https://www.crunchbase.com/hub/unicorn-companies")["id"] == "unicorn-companies"
    with pytest.raises(ValueError):
        refs.resolve_person("https://www.crunchbase.com/organization/stripe")


def test_company_search_schema():
    data, err = load_query(schemas.CompanySearchSchema, {
        "category": "Artificial Intelligence", "location": "san-francisco-california", "employee_count": "11-50, 51-100",
        "last_funding_type": "SERIES_B", "min_funding_total": "1000000", "sort": "growth_score", "page": "3",
        "investor": "https://www.crunchbase.com/organization/sequoia-capital", "founded_after": "2020-01-01"})
    assert err is None
    assert data["employee_count"] == ["11-50", "51-100"] and data["last_funding_type"] == "series_b"
    assert data["min_funding_total"] == 1000000 and data["page"] == 3
    assert data["investor"] == {"id": "sequoia-capital", "kind": "organization"}
    _, err = load_query(schemas.CompanySearchSchema, {"employee_count": "11-49"})
    assert err and "employee_count" in err["errors"]
    _, err = load_query(schemas.CompanySearchSchema, {"bogus": "1"})
    assert err
    _, err = load_query(schemas.CompanySearchSchema, {"founded_after": "2020/01/01"})
    assert err


def test_detail_schemas():
    data, err = load_query(schemas.CompanyPeopleSchema, {"company": "openai", "role": "Advisors"})
    assert err is None and data == {"company": {"id": "openai", "kind": None}, "role": "advisors", "page": 1}
    _, err = load_query(schemas.CompanySchema, {})
    assert err and "company" in err["errors"]
    data, err = load_query(schemas.AutocompleteSchema, {"query": "  stripe ", "type": "Company"})
    assert err is None and data == {"query": "stripe", "type": "company", "limit": 10}
    data, err = load_query(schemas.InvestmentSearchSchema, {"company": "openai", "funding_round": "openai-series-unknown--f23ea79d", "lead_only": "yes"})
    assert err is None and data["lead_only"] is True and data["company"] == {"id": "openai", "kind": None}
    _, err = load_query(schemas.InvestmentSearchSchema, {"funding_round": "openai-series-unknown--f23ea79d"})
    assert err and "company" in err["errors"]


# ---- primitives ----------------------------------------------------------------------

def test_primitives():
    r = parsers.ref({"uuid": "u1", "value": "Stripe", "permalink": "stripe", "image_id": "img", "entity_def_id": "organization"})
    assert r == {"id": "stripe", "uuid": "u1", "name": "Stripe", "type": "organization",
                 "link": "https://www.crunchbase.com/organization/stripe", "image": parsers.IMAGE_BASE + "img"}
    assert parsers.ref({"identifier": {"uuid": "u2", "value": "X", "entity_def_id": "job"}})["id"] == "u2"
    assert parsers.ref(None) is None and parsers.ref({}) is None
    assert parsers.money({"value": 5, "currency": "EUR", "value_usd": 6}) == {"amount": 5, "currency": "EUR", "amount_usd": 6}
    assert parsers.date({"value": "2023-08-02", "precision": "day"}) == "2023-08-02" and parsers.date("") is None
    assert parsers.employees("c_00011_00050") == "11-50" and parsers.revenue("r_00010000") == "$10M to $50M"
    loc = parsers.location([{"value": "Paris", "location_type": "city", "permalink": "paris-ile-de-france"},
                            {"value": "France", "location_type": "country", "permalink": "france"},
                            {"value": "EMEA", "location_type": "group"}])
    assert loc["city"] == "Paris" and loc["country"] == "France" and loc["groups"] == ["EMEA"] and loc["region"] is None
    assert parsers.location([]) is None
    assert parsers._clean_enum("c200_evidence_present") == "evidence_present"
    assert parsers._clean_enum("p100_positive_high") == "positive_high"
    assert parsers._ipo_from_text("X went public on 2026-09-21 and raised $13088478. X was valued at $52249622") == (
        "2026-09-21", {"amount": 13088478.0, "currency": "USD", "amount_usd": 13088478.0},
        {"amount": 52249622.0, "currency": "USD", "amount_usd": 52249622.0})


# ---- organization ---------------------------------------------------------------------

def test_organization_entity():
    data = fx("org_openai")
    row = parsers.organization_row(fx("search_companies")["entities"][0]) if False else {}
    org = parsers.organization(data, row)
    assert org["id"] == "openai" and org["uuid"] == "cf2c678c-b81a-80c3-10d1-9c5e76448e51"
    assert org["link"] == "https://www.crunchbase.com/organization/openai" and org["image"].endswith("jjykwqqhsscreywea4gb")
    assert org["website"] == "https://openai.com" and org["email"] and org["social"]["linkedin"]
    assert org["employee_count"] == "1001-5000" and org["ipo_status"] == "private" and org["operating_status"] == "active"
    assert {"id": "artificial-intelligence", "name": "Artificial Intelligence (AI)"} in org["categories"]
    assert org["location"]["city"] == "San Francisco" and "San Francisco Bay Area" in org["location"]["groups"]
    assert org["hub_tags"] == ["unicorn"] and org["founders"][0]["type"] == "person"
    assert org["funding"]["rounds_count"] >= 10 and org["funding"]["investors_count"] > 50
    assert org["people"]["employees_count"] > 10 and org["technology"]["technologies_count"] > 10
    assert org["growth"]["growth_score"] is not None and org["signals_count"]["layoffs"] >= 1
    assert org["rank"]["by_category"][0]["rank"] >= 1
    assert org["funding"]["total"] is None          # anonymous entitlement hides it; the search row fills it when present
    assert org["products"][0]["name"]


def test_organization_row_merges():
    row = parsers.organization_row({"uuid": "u", "properties": {
        "identifier": {"uuid": "u", "value": "Acme", "permalink": "acme", "entity_def_id": "organization"},
        "num_employees_enum": "c_00011_00050", "rank_org": 12, "rank_org_company": 10,
        "website_url": "https://acme.io", "aliases": ["ACME Inc"], "growth_score": 55,
        "funding_total": {"value": 1000, "currency": "USD", "value_usd": 1000}, "founded_on": "2015-01-02"}})
    assert row["employee_count"] == "11-50" and row["rank"] == 12 and row["funding_total"]["amount"] == 1000
    org = parsers.organization({"properties": {"identifier": {"uuid": "u", "value": "Acme", "permalink": "acme",
                                                              "entity_def_id": "organization"}}, "cards": {}}, row)
    assert org["founded_on"] == "2015-01-02" and org["funding"]["total"]["amount"] == 1000
    assert org["website"] == "https://acme.io" and org["employee_count"] == "11-50" and org["rank"]["overall"] == 12
    assert org["aliases"] == ["ACME Inc"]


def test_organization_cards():
    cards = fx("org_openai")["cards"]
    inv = [parsers.investment_row(r) for r in cards["investors_list"]]
    assert inv and inv[0]["investor"]["name"] and inv[0]["funding_round"]["id"] and inv[0]["is_lead_investor"] in (True, False)
    jobs = [parsers.job_row(r) for r in cards["current_employees_image_list"]]
    assert jobs and jobs[0]["person"]["link"].startswith("https://www.crunchbase.com/person/") and jobs[0]["title"]
    sim = [parsers.similar_row(r) for r in cards["org_similarity_list"]]
    assert sim and sim[0]["company"]["name"] and sim[0]["score"] and isinstance(sim[0]["micro_categories"], list)
    offices = [parsers.office_row(r) for r in cards["offices"]]
    assert offices and (offices[0]["opened_on"] or offices[0]["opening_press"] or offices[0]["location"])
    tech = [parsers.technology_row(r) for r in cards["builtwith_tech_used_list"]]
    assert tech and tech[0]["name"] and tech[0]["companies_using"]
    growth = parsers.growth(cards)
    assert growth["growth_score"] is not None and isinstance(growth["growth_score_history"], list)
    kinds = {p["type"] for p in growth["predictions"]}
    assert kinds <= {"funding", "ipo", "acquisition", "growth"} and all(0 <= p["probability"] <= 1 for p in growth["predictions"] if p["probability"] is not None)
    layoffs = [parsers.key_event(r, "layoff") for r in cards["layoff_list"]]
    assert layoffs and layoffs[0]["date"] and layoffs[0]["press"][0]["link"]
    tl = [parsers.timeline_row(r) for r in cards["overview_timeline"]["entities"]]
    assert tl and tl[0]["type"] == "press_reference" and tl[0]["date"] and tl[0]["title"]
    acq = [parsers.acquisition_row(r) for r in cards["acquisitions_list"]]
    assert acq and acq[0]["acquiree"]["name"] and acq[0]["link"].startswith("https://www.crunchbase.com/acquisition/")


# ---- person ---------------------------------------------------------------------------

def test_person_entity():
    p = parsers.person(fx("person_sam_altman"), {"rank": 25})
    assert p["id"] == "sam-altman" and p["title"] == "Co-Founder and CEO" and p["company"]["id"] == "openai"
    assert p["gender"] == "male" and p["location"]["city"] == "San Francisco" and p["rank"] == 25
    assert p["is_investor"] is True and p["investing"]["investments_count"] > 50
    assert p["current_jobs"][0]["company"]["name"] == "OpenAI" and p["current_jobs"][0]["started_on"]
    assert p["past_jobs"] and p["board_and_advisor_roles"][0]["ended_on"]
    assert p["social"]["twitter"] and p["website"]


# ---- search rows ------------------------------------------------------------------------

def test_search_rows():
    rows = fx("search_companies")["entities"]
    c = parsers.organization_summary(rows[0])
    assert c["id"] and c["name"] and c["link"] and c["rank_company"] >= 1 and "employee_count_token" not in c
    assert all(cat["id"] and cat["name"] for cat in c["categories"])
    inv = parsers.investor_row(fx("search_investors")["entities"][0])
    assert inv["name"] and inv["rank"] and inv["investments_count"] and inv["location"]["country"]
    person = parsers.person_summary(fx("search_people")["entities"][0])
    assert person["company"]["id"] == "openai" and person["title"] and isinstance(person["is_investor"], bool)
    fr = parsers.funding_round_row(fx("search_funding_rounds")["entities"][0])
    assert fr["company"]["id"] == "openai" and fr["round_type"] and fr["link"].startswith("https://www.crunchbase.com/funding_round/")
    assert fr["announced_on"] is None and fr["amount"] is None       # gated anonymously
    acq = parsers.acquisition_search_row(fx("search_acquisitions")["entities"][0])
    assert acq["acquirer"]["id"] == "openai" and acq["acquiree"]["name"] and acq["acquiree_location"]
    inv2 = parsers.investment_search_row(fx("search_investments")["entities"][0])
    assert inv2["investor"]["id"] and inv2["investor"]["name"] and inv2["funding_round"]["id"].endswith(inv2["funding_round"]["id"][-8:])
    assert inv2["company"]["name"] == "OpenAI" and inv2["round_type"]
    ipo = parsers.ipo_row(fx("search_ipos")["entities"][0])
    assert ipo["stock_exchange"] == "nasdaq" and ipo["stock_symbol"] and ipo["went_public_on"]
    fund = parsers.fund_row(fx("search_funds")["entities"][0])
    assert fund["name"] and fund["owner"]["id"] and fund["link"].startswith("https://www.crunchbase.com/fund/")
    ev = parsers.event_row(fx("search_events")["entities"][0])
    assert ev["starts_on"] >= "2026-10-01" and ev["link"].startswith("https://www.crunchbase.com/event/")
    app = parsers.appearance_row(fx("search_appearances")["entities"][0])
    assert app["participant"]["id"] == "openai" and app["role"] in schemas.APPEARANCE_TYPES and app["event"]["id"]
    press = parsers.press_row(fx("search_press")["entities"][0])
    assert press["title"] and press["posted_on"] and press["publisher"]
    hub = parsers.hub_row(fx("search_hubs")["entities"][0])
    assert hub["companies_count"] is not None and hub["rank"] and hub["link"].startswith("https://www.crunchbase.com/hub/")


def test_entities():
    fr = parsers.funding_round(fx("funding_round"))
    assert fr["id"] and fr["company"]["id"] and fr["round_type"] and isinstance(fr["investments"], list)
    acq = parsers.acquisition(fx("acquisition"))
    assert acq["acquirer"]["name"] and acq["acquiree"]["name"] and isinstance(acq["press"], list)
    fund = parsers.fund(fx("fund"))
    assert fund["owner"]["id"] and fund["link"]
    ev = parsers.event(fx("event"))
    assert ev["id"] == "cluecon-2027" and ev["starts_on"] and "participants_count" in ev and isinstance(ev["speakers"], list)
    hub = parsers.hub(fx("hub"))
    assert hub["id"] == "artificial-intelligence-companies" and hub["companies_count"] and hub["recent_funding_rounds"]
    assert hub["investors"][0]["name"] and hub["trending_companies"][0]["name"]


def test_autocomplete_and_news():
    rows = [parsers.autocomplete_row(r) for r in fx("autocomplete")["entities"]]
    assert rows[0]["id"] == "stripe" and rows[0]["is_investor"] is True and rows[0]["short_description"]
    loc = parsers.location_row(fx("autocomplete_locations")["entities"][0])
    assert loc["location_type"] == "city" and loc["name"] == "San Francisco"
    cat = parsers.category_row(fx("autocomplete_categories")["entities"][0])
    assert cat["type"] in ("category", "category_group") and cat["name"]
    post = parsers.news_post(fx("news_posts")[0])
    assert post["title"] and post["link"].startswith("https://news.crunchbase.com/") and post["published_at"] and post["authors"]


def test_pagination_helpers():
    p = shared.pagination(2, 100)
    assert p == {"page": 2, "items_per_page": 15, "total_pages": 7, "total_count": 100}
    assert shared.pagination(3, None, has_items=False)["total_pages"] == 2
    assert shared.pagination(1, 10000, reachable=40)["total_pages"] == 40
