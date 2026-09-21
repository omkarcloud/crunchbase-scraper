"""Live endpoint smoke tests: one call per /crunchbase/* route against a
running service, with example values proven to return data (2026-09-21).
The listing tooling reads each route's FIRST call from this file's AST as
its working example, so the values stay literals.

Skipped unless CRUNCHBASE_BASE points at a running service:

    ONLY_SCRAPER=crunchbase python run.py
    CRUNCHBASE_BASE=http://127.0.0.1:6002 python -m pytest crunchbase/test_endpoints.py -q
"""
import os

import pytest

BASE = os.environ.get("CRUNCHBASE_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="set CRUNCHBASE_BASE to run live endpoint tests")


def call(path, status=200, **params):
    from curl_cffi import requests
    resp = requests.get(BASE + path, params=params, timeout=240)
    assert resp.status_code == status, f"{path} {params} -> {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body, f"{path} returned an empty body"
    return body


def test_discovery():
    assert call("/crunchbase/autocomplete", query="stripe", type="company")["items"][0]["id"] == "stripe"
    page = call("/crunchbase/companies/search", category="artificial-intelligence", location="san-francisco-california",
                last_funding_type="series_b", sort="rank")
    assert page["items"][0]["rank_company"] and page["total_pages"] > 1 and page["next"]
    assert call("/crunchbase/companies/search", employee_count="11-50,51-100", min_growth_score=80, sort="growth_score", page=2)["items"]
    assert call("/crunchbase/companies/search", investor="sequoia-capital", founded_after="2024-01-01")["items"]
    assert call("/crunchbase/companies/by-domain", domain="stripe.com")["items"][0]["id"] == "stripe"
    assert call("/crunchbase/investors/search", query="sequoia", sort="investments")["items"][0]["investments_count"]
    assert call("/crunchbase/people/search", company="stripe", title="CEO")["items"][0]["name"]
    assert call("/crunchbase/people/search", is_investor="true", location="san-francisco-california", sort="investments")["items"]
    assert call("/crunchbase/schools/search", query="stanford")["items"][0]["name"]
    assert call("/crunchbase/categories/search", query="fintech")["items"][0]["id"]
    assert call("/crunchbase/locations/search", query="san fr", type="city")["items"][0]["location_type"] == "city"
    assert call("/crunchbase/hubs/search", query="unicorn", page=2)["items"][0]["companies_count"] is not None
    assert call("/crunchbase/hubs/details", hub="artificial-intelligence-companies")["recent_funding_rounds"]


def test_companies():
    details = call("/crunchbase/companies/details", company="https://www.crunchbase.com/organization/openai")
    assert details["id"] == "openai" and details["employee_count"] and details["funding"]["rounds_count"] and details["founders"]
    assert call("/crunchbase/companies/funding", company="openai", sort="rank")["items"][0]["round_type"]
    assert call("/crunchbase/companies/investors", company="openai", page=2)["items"][0]["investor"]["name"]
    assert call("/crunchbase/companies/investments", company="sequoia-capital")["items"][0]["company"]["name"]
    assert call("/crunchbase/companies/acquisitions", company="google", sort="rank")["items"][0]["acquiree"]["name"]
    assert call("/crunchbase/companies/people", company="openai", role="advisors")["items"][0]["person"]["name"]
    assert "subsidiaries" in call("/crunchbase/companies/subsidiaries", company="stripe")
    assert call("/crunchbase/companies/similar", company="openai")["similar_companies"][0]["company"]["name"]
    assert call("/crunchbase/companies/technology", company="openai")["technologies"][0]["name"]
    assert call("/crunchbase/companies/growth", company="openai")["predictions"]
    assert call("/crunchbase/companies/signals", company="openai", type="layoffs")["items"][0]["date"]
    assert call("/crunchbase/companies/offices", company="openai")["headquarters"]["city"]
    assert call("/crunchbase/companies/timeline", company="openai")["items"][0]["title"]
    assert call("/crunchbase/companies/news", company="openai")["items"][0]["title"]
    assert call("/crunchbase/companies/events", company="openai", role="sponsor")["items"][0]["event"]["name"]
    assert "hubs" in call("/crunchbase/companies/hubs", company="stripe")
    call("/crunchbase/companies/details", status=404, company="no-such-company-xyz-123")
    call("/crunchbase/companies/details", status=400, company="https://www.crunchbase.com/person/sam-altman")


def test_people():
    person = call("/crunchbase/people/details", person="sam-altman")
    assert person["title"] and person["company"]["id"] == "openai" and person["current_jobs"]
    assert call("/crunchbase/people/jobs", person="patrick-collison", kind="current")["current_jobs"][0]["company"]["name"]
    assert call("/crunchbase/people/investments", person="sam-altman")["items"][0]["company"]["name"]
    assert call("/crunchbase/people/news", person="sam-altman")["items"][0]["title"]
    assert call("/crunchbase/people/events", person="sam-altman", role="speaker")["items"][0]["event"]["name"]


def test_deals():
    rounds = call("/crunchbase/funding-rounds/search", company="anthropic", sort="rank", page=2)
    assert rounds["items"] and rounds["previous"]
    assert call("/crunchbase/funding-rounds/search", investor="sequoia-capital", round_type="series_a")["items"]
    assert call("/crunchbase/funding-rounds/details", funding_round="openai-series-unknown--f23ea79d")["investments"]
    assert call("/crunchbase/acquisitions/search", acquirer="google", sort="rank")["items"][0]["acquiree"]["name"]
    assert call("/crunchbase/acquisitions/details", acquisition="stripe-acquires-clerky--0a0d0f5c")["acquiree"]["id"] == "clerky"
    assert call("/crunchbase/investments/search", company="stripe")["items"][0]["investor"]["name"]
    assert call("/crunchbase/investments/search", company="openai", funding_round="openai-series-unknown--f23ea79d")["items"]
    assert call("/crunchbase/ipos/search", stock_exchange="nasdaq")["items"][0]["went_public_on"]
    assert call("/crunchbase/funds/search", owner="sequoia-capital")["items"][0]["name"]


def test_events_press_news():
    events = call("/crunchbase/events/search", starts_after="2026-10-01", sort="starts")
    assert events["items"][0]["starts_on"]
    event_id = events["items"][0]["id"]
    assert call("/crunchbase/events/details", event=event_id)["name"]
    assert "items" in call("/crunchbase/events/participants", event="microsoft-build-2024", role="speaker")
    assert call("/crunchbase/events/appearances", entity="stripe")["items"][0]["event"]["name"]
    assert call("/crunchbase/press/search", entity="stripe")["items"][0]["title"]
    assert call("/crunchbase/press/search", query="acquisition")["items"]
    assert call("/crunchbase/news/latest")["items"][0]["link"]
    assert call("/crunchbase/news/search", query="stripe", page=2)["items"]
