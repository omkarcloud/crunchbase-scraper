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


@pytest.mark.parametrize("value,expected", [
    ("stripe", {"id": "stripe", "kind": None}),                                    # still a permalink
    ("https://www.crunchbase.com/organization/stripe", {"id": "stripe", "kind": "organization"}),
    ("Stripe, Inc.", {"id": None, "kind": "organization", "name": "Stripe, Inc."}),
    ("  Open   AI ", {"id": None, "kind": "organization", "name": "Open AI"}),
])
def test_resolve_company_or_name(value, expected):
    assert refs.resolve_company_or_name(value) == expected


@pytest.mark.parametrize("value", [
    "https://www.crunchbase.com/person/sam-altman",      # a link is never read as a name
    "https://www.linkedin.com/company/stripe",
    "",
    "!!!",                                               # no letter or digit: names nothing
])
def test_resolve_company_or_name_rejects(value):
    with pytest.raises(ValueError):
        refs.resolve_company_or_name(value)


def test_name_key_reads_every_script_and_folds_accents():
    assert refs.name_key("Open AI") == refs.name_key("open-ai") == "openai"
    assert refs.name_key("Nestlé") == refs.name_key("Nestle") == "nestle"
    assert refs.name_key("Škoda") == "skoda" != refs.name_key("Koda")
    assert refs.name_key("Яндекс") == "яндекс" and refs.name_key("字节跳动") == "字节跳动"
    assert refs.name_key("की") != refs.name_key("का")                 # a vowel sign is part of the word
    assert refs.name_key(" . ") == "" and refs.name_key(None) == ""
    assert refs.resolve_company_or_name("Яндекс") == {"id": None, "kind": "organization", "name": "Яндекс"}


def test_company_details_takes_a_name():
    data, err = load_query(schemas.CompanyDetailsSchema, {"company": "Open AI"})
    assert err is None and data == {"company": {"id": None, "kind": "organization", "name": "Open AI"}}
    _, err = load_query(schemas.CompanySchema, {"company": "Open AI"})          # the other views stay strict
    assert err and "company" in err["errors"]


def _named_rows(*names):
    return [{"identifier": {"value": n, "permalink": n.lower().replace(" ", "-"), "uuid": None}} for n in names]


def test_pick_named():
    from crunchbase import companies
    rows = _named_rows("OpenAI Startup Fund", "OpenAI", "Open AI Labs")
    assert companies.pick_named(rows, "Open AI")["identifier"]["value"] == "OpenAI"          # exact, spacing aside
    assert companies.pick_named(rows, "openai startup")["identifier"]["value"] == "OpenAI Startup Fund"   # best-ranked prefix
    assert companies.pick_named(rows, "openai startup", exact_only=True) is None
    assert companies.pick_named(_named_rows("Applied OpenAI"), "OpenAI") is None             # contains is another company
    assert companies.pick_named(rows, " ") is None
    assert companies.name_spellings("Stripe, Inc.") == ["Stripe, Inc.", "Stripe"]
    assert companies.name_spellings("Open AI") == ["Open AI", "OpenAI"]
    assert companies.name_spellings("Inc") == ["Inc"]
    assert companies.name_spellings("Nestlé S.A.") == ["Nestlé S.A.", "Nestlé"]             # a dotted suffix
    assert companies.name_spellings("Acme Co., Ltd.") == ["Acme Co., Ltd.", "Acme"]
    assert companies.pick_named(_named_rows("Koda", "Škoda Auto"), "Škoda")["identifier"]["value"] == "Škoda Auto"
    assert companies.pick_named(_named_rows("Яндекс"), "яндекс")["identifier"]["value"] == "Яндекс"


def test_details_looks_a_name_up_and_retries_a_missing_permalink(monkeypatch):
    from crunchbase import companies, fetch
    monkeypatch.setattr(companies, "_names", shared._TTLCache(ttl=60, max_size=10))
    searched, fetched = [], []

    def typeahead(collection, query):
        searched.append(query)
        return (_named_rows("Tesla") if query.lower() == "tesla" else []), 0

    def entity(company):
        fetched.append(company["id"])
        if company["id"] != "tesla":
            raise fetch.CrunchbaseNotFound(f"company '{company['id']}' not found on Crunchbase")
        return {"properties": {"identifier": {"uuid": "u-tesla"}}}

    monkeypatch.setattr(fetch, "_typeahead", typeahead)
    monkeypatch.setattr(companies, "_entity", entity)
    monkeypatch.setattr(companies, "_row", lambda uuid: {})
    monkeypatch.setattr(companies.parsers, "organization", lambda data, extra: {"uuid": companies._uuid(data)})
    assert companies.details({"id": None, "kind": "organization", "name": "Tesla, Inc."}) == {"uuid": "u-tesla"}
    assert searched == ["Tesla, Inc.", "Tesla"] and fetched == ["tesla"]
    # a bare word that is no permalink is retried as a name; a link or a uuid is final
    fetched.clear()
    monkeypatch.setattr(companies, "_named", lambda name: {"id": "tesla", "kind": "organization"})
    assert companies.details({"id": "tesla-inc", "kind": None}) == {"uuid": "u-tesla"}
    assert fetched == ["tesla-inc", "tesla"]
    with pytest.raises(fetch.CrunchbaseNotFound):
        companies.details({"id": "tesla-inc", "kind": "organization"})


def test_investor_filter_takes_a_name():
    assert refs.resolve_entity_or_name("Sequoia Capital") == {"id": None, "kind": None, "name": "Sequoia Capital"}
    assert refs.resolve_entity_or_name("sequoia-capital") == {"id": "sequoia-capital", "kind": None}
    assert refs.resolve_entity_or_name("https://www.crunchbase.com/person/sam-altman") == {"id": "sam-altman", "kind": "person"}
    with pytest.raises(ValueError):
        refs.resolve_entity_or_name("https://www.crunchbase.com/funding_round/x--y")     # a link is never a name
    data, err = load_query(schemas.FundingRoundSearchSchema, {"investor": "Vocap Partners", "lead_investor": "a16z"})
    assert err is None and data["investor"]["name"] == "Vocap Partners" and data["lead_investor"]["id"] == "a16z"
    data, err = load_query(schemas.CompanySearchSchema, {"investor": "Y Combinator"})
    assert err is None and data["investor"]["name"] == "Y Combinator"
    assert shared.slug_words("john-smith-7d6e") == "john smith" and shared.slug_words("vocap-partners") == "vocap partners"
    assert shared.slug_words("1836-ventures") == "1836 ventures"          # a leading number is a word, not a suffix
    assert shared.slug_words("instinct-9264") == "instinct" and shared.slug_words("nfx") == "nfx"


def test_pick_named_matches_whole_words_only():
    rows = _investor_rows(("VentureSoul Partners", "venturesoul-partners", "organization"),
                          ("F-Prime Capital", "f-prime-capital", "organization"))
    assert shared.pick_named(rows, "ventures") is None                    # a partial word is another firm
    assert shared.pick_named(rows, "f prime")["identifier"]["permalink"] == "f-prime-capital"
    assert shared.pick_named(rows, "F-Prime Capital", exact_only=True)["identifier"]["permalink"] == "f-prime-capital"
    assert shared.pick_named(rows, "prime capital") is None               # contains is not begins with


def _investor_rows(*rows):
    return [{"identifier": {"value": name, "permalink": slug, "uuid": f"u-{slug}", "entity_def_id": kind}} for name, slug, kind in rows]


def test_investor_name_and_unknown_slug_are_looked_up(monkeypatch):
    from crunchbase import fetch
    monkeypatch.setattr(shared, "_investor_names", shared._TTLCache(ttl=60, max_size=10))
    searched = []

    def typeahead(collection, query):
        searched.append((collection, query))
        rows = _investor_rows(("Vocap Partners Fund II", "vocap-fund-ii", "organization"),
                              ("Vocap Partners", "vocap-ventures", "organization"))
        return (rows if query.lower().startswith("vocap") else []), 0

    def uuid_of(kind, ref, missing=None):
        raise fetch.CrunchbaseNotFound("nope")

    monkeypatch.setattr(fetch, "_typeahead", typeahead)
    monkeypatch.setattr(shared, "uuid_of", uuid_of)
    # a permalink Crunchbase does not have -> its words, looked up among investors (exact name wins over rank)
    assert shared.entity_uuid_any({"id": "vocap-partners", "kind": None}) == ("u-vocap-ventures", "organization")
    assert searched[0] == ("principal.investors", "vocap partners")
    # a plain name
    assert shared.entity_uuid_any({"id": None, "kind": None, "name": "Vocap Partners"}) == ("u-vocap-ventures", "organization")
    # nothing called that -> a clear 404
    with pytest.raises(fetch.CrunchbaseNotFound, match="no investor named"):
        shared.entity_uuid_any({"id": None, "kind": None, "name": "zzqx nothing"})
    with pytest.raises(fetch.CrunchbaseNotFound, match="not a company or person"):
        shared.entity_uuid_any({"id": "zzqx-nothing", "kind": None})


def test_email_is_cleaned():
    assert parsers.email("support@openrouter.ai.") == "support@openrouter.ai"
    assert parsers.email(" mailto:hi@stripe.com ") == "hi@stripe.com"
    assert parsers.email("dpo@stripe.com") == "dpo@stripe.com"
    assert parsers.email(" . ") is None and parsers.email(None) is None
    assert parsers.email("MAILTO:hi@stripe.com") == "hi@stripe.com"
    assert parsers.email("mailto:<hi@stripe.com>.") == "hi@stripe.com"


def test_search_rows_take_the_filtered_operating_status(monkeypatch):
    from crunchbase import discover
    monkeypatch.setattr(discover, "paged_search", lambda *a, **k: ({"items": [{"id": "x", "operating_status": None}]}, []))
    assert discover.search_companies(operating_status="closed")["items"][0]["operating_status"] == "closed"
    assert discover.search_companies()["items"][0]["operating_status"] is None


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


# ---- cursor memo (page N after pages 1..N-1 = one upstream call) ---------------------

def _rank_search(calls, ranks, field="rank_org_company"):
    """A search over rows ranked 1..N that honours the cursor predicate."""
    def search(collection, fields, query, order, limit=15, after_id=None):
        calls.append(query)
        rows = [{"uuid": f"u{r}", "properties": {field: r}} for r in ranks]
        for p in query:
            if p["field_id"] == field:
                op, v = p["operator_id"], p["values"][0]
                keep = {"gt": lambda x: x > v, "gte": lambda x: x >= v, "lt": lambda x: x < v, "lte": lambda x: x <= v}[op]
                rows = [r for r in rows if keep(r["properties"][field])]
        return {"count": len(ranks), "entities": rows[:limit]}
    return search


def _page(n, query=()):
    result, _ = shared.paged_search("organization.companies", ["identifier"], list(query), "rank_org_company",
                                    "asc", n, lambda r: r)
    return result


def test_cursor_memo_serves_page_n_with_one_call(monkeypatch):
    shared._cursors.clear()
    calls = []
    monkeypatch.setattr(shared.fetch, "search", _rank_search(calls, range(1, 61)))
    first = _page(1)
    assert len(calls) == 1 and [r["uuid"] for r in first["items"]] == [f"u{i}" for i in range(1, 16)]
    second = _page(2)
    assert len(calls) == 2 and second["items"][0]["uuid"] == "u16" and len(second["items"]) == 15
    assert second["pagination"] == {"page": 2, "items_per_page": 15, "total_pages": 4, "total_count": 60}
    third = _page(3)
    assert len(calls) == 3 and third["items"][0]["uuid"] == "u31"
    # the walk's end is not memoised past the last full page
    assert _page(4)["items"][-1]["uuid"] == "u60" and len(calls) == 4
    assert _page(5)["items"] == [] and len(calls) == 5
    # a cold page 3 still walks three calls, and re-primes the memo
    shared._cursors.clear()
    calls.clear()
    assert _page(3)["items"][0]["uuid"] == "u31" and len(calls) == 3
    assert _page(4)["items"][0]["uuid"] == "u46" and len(calls) == 4
    # page 3 after only page 1: two calls (resume from the deepest memoised hop)
    shared._cursors.clear()
    calls.clear()
    _page(1)
    assert _page(3)["items"][0]["uuid"] == "u31" and len(calls) == 3
    # a different query keys apart
    calls.clear()
    other = _page(2, [shared.predicate("categories", "includes", ["x"])])
    assert other["items"][0]["uuid"] == "u16" and len(calls) == 2
    # expired walks are forgotten
    calls.clear()
    monkeypatch.setattr(shared._cursors, "ttl", 0)
    assert _page(2)["items"][0]["uuid"] == "u16" and len(calls) == 2


def test_cursor_memo_carries_the_tie_skip_set(monkeypatch):
    """Date sorts tie: the inclusive cursor re-serves the boundary rows and
    the memoised `seen` set has to drop them on the resumed hop."""
    shared._cursors.clear()
    calls = []
    # 40 rows over 4 dates, 10 rows a date, newest first
    rows = [{"uuid": f"d{i}", "properties": {"announced_on": f"2026-0{4 - i // 10}-01"}} for i in range(40)]

    def search(collection, fields, query, order, limit=15, after_id=None):
        calls.append(query)
        out = rows
        for p in query:
            if p["field_id"] == "announced_on":
                v = p["values"][0]
                out = [r for r in out if (r["properties"]["announced_on"] <= v if p["operator_id"] == "lte"
                                          else r["properties"]["announced_on"] < v)]
        return {"count": 40, "entities": out[:limit]}

    monkeypatch.setattr(shared.fetch, "search", search)

    def page(n):
        result, _ = shared.paged_search("funding_rounds", ["identifier"], [], "announced_on", "desc", n, lambda r: r)
        return [r["uuid"] for r in result["items"]]

    warm = [page(1), page(2), page(3), page(4)]
    assert len(calls) == 4                      # one call a page with the memo
    assert warm[0] == [f"d{i}" for i in range(15)]
    # the inclusive cursor re-serves the boundary date's rows; dedupe drops
    # the 5 already served, so a page past the first holds 10 new rows
    assert warm[1] == [f"d{i}" for i in range(15, 25)]
    assert warm[2] == [f"d{i}" for i in range(25, 35)]
    assert warm[3] == [f"d{i}" for i in range(35, 40)]
    shared._cursors.clear()
    calls.clear()
    assert page(3) == warm[2] and len(calls) == 3   # the cold walk agrees
    assert page(4) == warm[3] and len(calls) == 4


def test_card_cursor_memo(monkeypatch):
    shared._card_cursors.clear()
    calls = []
    rows = [{"identifier": {"uuid": f"c{i}"}} for i in range(1, 26)]

    def card(collection, ident, card_id, after_id=None):
        calls.append(after_id)
        start = next(i for i, r in enumerate(rows) if r["identifier"]["uuid"] == after_id) + 1 if after_id else 0
        return rows[start:start + 10]

    monkeypatch.setattr(shared.fetch, "card", card)
    embedded = rows[:8]                          # the entity embeds a short first page
    ids = lambda page: [r["identifier"]["uuid"] for r in page]
    assert ids(shared.card_pages("organizations", "acme", "investors", 2, first_rows=embedded)) == [f"c{i}" for i in range(9, 19)]
    assert calls == ["c8"]
    assert ids(shared.card_pages("organizations", "acme", "investors", 3, first_rows=embedded)) == [f"c{i}" for i in range(19, 26)]
    assert calls == ["c8", "c18"]                # one call, resumed from the memoised after_id
    assert shared.card_pages("organizations", "acme", "investors", 4, first_rows=embedded) == []
    assert calls == ["c8", "c18", "c18"]         # the short page 3 ends the walk, as fetch.card_pages would
    shared._card_cursors.clear()
    calls.clear()
    assert ids(shared.card_pages("organizations", "acme", "investors", 3, first_rows=embedded))[0] == "c19"
    assert calls == ["c8", "c18"]                # cold: N-1 calls


# ---- a round's full investor list (the entity embeds only the first 10) --------------

def _investment(i):
    return {"identifier": {"uuid": f"i{i}", "value": f"Investor {i} investment in Seed Round - Acme",
                           "permalink": f"investor-{i}-invested-in-acme-seed--i{i}", "entity_def_id": "investment"},
            "investor_identifier": {"uuid": f"v{i}", "value": f"Investor {i}", "permalink": f"investor-{i}",
                                    "entity_def_id": "organization"}}


def _cards_endpoint(calls, rows):
    def card(collection, ident, card_id, after_id=None):
        calls.append((collection, card_id, after_id))
        start = next(i for i, r in enumerate(rows) if r["identifier"]["uuid"] == after_id) + 1 if after_id else 0
        return rows[start:start + 10]
    return card


def _round_entity(rows, total):
    return {"properties": {"identifier": {"uuid": "r-uuid", "value": "Seed Round - Acme",
                                          "permalink": "acme-seed--5ab76566", "entity_def_id": "funding_round"}},
            "cards": {"investors_headline": {"num_investors": total}, "investors_list": rows[:10],
                      "overview_fields_v2": {"investment_type": "seed"}}}


def test_round_details_walks_every_investor(monkeypatch):
    from crunchbase import deals
    rows = [_investment(i) for i in range(1, 17)]            # a 16-investor seed round
    calls = []
    monkeypatch.setattr(shared.fetch, "card", _cards_endpoint(calls, rows))
    monkeypatch.setattr(deals.fetch, "entity", lambda *a, **k: _round_entity(rows, 16))
    monkeypatch.setattr(deals, "_one", lambda *a, **k: None)
    fr = deals.funding_round_details({"id": "acme-seed--5ab76566"})
    assert fr["investors_count"] == 16
    assert [i["investor"]["id"] for i in fr["investments"]] == [f"investor-{i}" for i in range(1, 17)]
    assert [r["id"] for r in fr["investors"]] == [f"investor-{i}" for i in range(1, 17)]
    assert calls == [("funding_rounds", "investors_list", "i10")]   # one cards call, after the 10th


def test_round_details_skip_the_walk_when_complete(monkeypatch):
    from crunchbase import deals
    rows = [_investment(i) for i in range(1, 11)]            # exactly 10: no extra call
    calls = []
    monkeypatch.setattr(shared.fetch, "card", _cards_endpoint(calls, rows))
    monkeypatch.setattr(deals.fetch, "entity", lambda *a, **k: _round_entity(rows, 10))
    monkeypatch.setattr(deals, "_one", lambda *a, **k: None)
    assert len(deals.funding_round_details({"id": "acme-seed--5ab76566"})["investments"]) == 10 and calls == []


def test_round_details_keep_the_first_page_when_refused(monkeypatch):
    from crunchbase import deals

    def refused(*a, **k):
        raise deals.fetch.CrunchbaseBadRequest("insufficient permissions (investors_list)")

    rows = [_investment(i) for i in range(1, 17)]
    monkeypatch.setattr(shared.fetch, "card", refused)
    monkeypatch.setattr(deals.fetch, "entity", lambda *a, **k: _round_entity(rows, 16))
    monkeypatch.setattr(deals, "_one", lambda *a, **k: None)
    fr = deals.funding_round_details({"id": "acme-seed--5ab76566"})
    assert len(fr["investments"]) == 10 and fr["investors_count"] == 16

    def blocked(*a, **k):
        raise deals.fetch.CrunchbaseBlocked("challenged")

    monkeypatch.setattr(shared.fetch, "card", blocked)
    with pytest.raises(deals.fetch.CrunchbaseBlocked):       # never a silent short list
        deals.funding_round_details({"id": "acme-seed--5ab76566"})


def test_card_all_stops(monkeypatch):
    rows = [_investment(i) for i in range(1, 46)]
    calls = []
    monkeypatch.setattr(shared.fetch, "card", _cards_endpoint(calls, rows))
    ids = lambda got: [r["identifier"]["uuid"] for r in got]
    # the headline count bounds the walk
    assert ids(shared.card_all("funding_rounds", "r", "investors_list", rows[:10], 25, 300)) == [f"i{i}" for i in range(1, 31)]
    assert [c[2] for c in calls] == ["i10", "i20"]
    # max_rows caps it
    calls.clear()
    assert len(shared.card_all("funding_rounds", "r", "investors_list", rows[:10], 45, 20)) == 20 and len(calls) == 1
    # a short cards page ends it even when the headline promised more
    calls.clear()
    assert len(shared.card_all("funding_rounds", "r", "investors_list", rows[:10], 99, 300)) == 45
    assert [c[2] for c in calls] == ["i10", "i20", "i30", "i40"]
    # a cards page of rows already held (after_id ignored) ends it
    calls.clear()
    monkeypatch.setattr(shared.fetch, "card", lambda *a, **k: calls.append(1) or rows[:10])
    assert len(shared.card_all("funding_rounds", "r", "investors_list", rows[:10], 16, 300)) == 10 and calls == [1]
    # no embedded page: the walk starts from the first cards page
    calls.clear()
    monkeypatch.setattr(shared.fetch, "card", _cards_endpoint(calls, rows[:16]))
    assert len(shared.card_all("funding_rounds", "r", "investors_list", [], 16, 300)) == 16
    assert [c[2] for c in calls] == [None, "i10"]


def test_round_lead_flags():
    rows = [_investment(i) for i in range(1, 17)]
    flags = lambda fr: {i["investor"]["id"]: i["is_lead_investor"] for i in fr["investments"]}
    lead_refs = parsers.refs_list([rows[2]["investor_identifier"], rows[11]["investor_identifier"]])
    # the search row's lead list is complete: listed = lead, everyone else = not
    fr = parsers.funding_round(_round_entity(rows, 16), {"lead_investors": lead_refs}, investments=rows)
    assert [k for k, v in flags(fr).items() if v] == ["investor-3", "investor-12"]
    assert sum(v is False for v in flags(fr).values()) == 14
    # a search row with no leads and no lead card: a round without leads
    assert set(flags(parsers.funding_round(_round_entity(rows, 16), {"lead_investors": []}, investments=rows)).values()) == {False}
    # no search row: the lead card names who led, the rest stay unknown
    entity = _round_entity(rows, 16)
    entity["cards"]["lead_investors_image_list"] = [{"identifier": rows[1]["investor_identifier"]}]
    fr = parsers.funding_round(entity, None, investments=rows)
    assert flags(fr)["investor-2"] is True and sum(v is None for v in flags(fr).values()) == 15
    assert [r["id"] for r in fr["lead_investors"]] == ["investor-2"]
    # nothing known about leads: all null
    assert set(flags(parsers.funding_round(_round_entity(rows, 16), None, investments=rows)).values()) == {None}
    # the captured round: its one investor is its lead
    assert parsers.funding_round(fx("funding_round"))["investments"][0]["is_lead_investor"] is True


def test_round_investors_count_from_the_gated_list_length():
    row = fx("search_funding_rounds")["entities"][0]
    assert row["properties"]["num_relationships"] == 134                  # not an investor count
    assert parsers.funding_round_row(row)["investors_count"] == row["preview_properties"]["investor_identifiers"]["length"] == 32
    card_row = {"identifier": {"uuid": "u", "value": "Seed Round - Acme", "permalink": "acme-seed--1", "entity_def_id": "funding_round"},
                "preview_properties": {"investor_identifiers": {"is_present": True, "length": 16}}}
    assert parsers.funding_round_row({"properties": card_row})["investors_count"] == 16
    assert parsers.funding_round_row({"properties": {"investor_identifiers": [{"uuid": "a", "value": "A"}]}})["investors_count"] == 1
    assert parsers.funding_round_row({"properties": {"identifier": card_row["identifier"]}})["investors_count"] is None
