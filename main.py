"""Use the scraper straight from Python — no server needed.

    python main.py

Every function returns the same JSON the API does; results are written to
output/*.json. The endpoint functions live in crunchbase/companies.py
(company profiles), crunchbase/people.py, crunchbase/discover.py (search and
lookups), crunchbase/deals.py (rounds, acquisitions, IPOs, funds) and
crunchbase/events.py (events, press, Crunchbase News).
"""
import json
import os

from crunchbase import companies, deals, discover, refs

os.makedirs("output", exist_ok=True)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


if __name__ == "__main__":
    # a permalink, a UUID or any crunchbase.com/organization/ link
    save("company_openai.json", companies.details(refs.resolve_company("openai")))

    # AI companies in San Francisco, 15 per page, best-ranked first
    save("search_ai_san_francisco.json", discover.search_companies(
        category="artificial-intelligence", location="san-francisco-california", sort="rank"))

    # every funding round Anthropic raised
    save("funding_rounds_anthropic.json", deals.search_funding_rounds(
        company=refs.resolve_company("anthropic"), sort="rank"))
