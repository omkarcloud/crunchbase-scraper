# 🚀 Crunchbase Scraper

Crunchbase Scraper is a **free and open-source** scraper that gets you **unlimited** detailed Crunchbase data for free.

## ✨ What Can I Get?

- 🏢 **Full profiles of 6.4M+ companies** — website, email, phone, socials, HQ, founders, headcount, rank, growth & heat scores
- 💰 **822K+ funding rounds & 212K+ acquisitions** — round types, lead investors, every investor in a round, acquirers and targets
- 👤 **2.2M+ people & 349K+ investors** — jobs, board seats, education, portfolios, exits and every investment they made
- 📡 **Signals developers pay extra for** — tech stack, web traffic, layoffs, key hires, product launches, lawsuits and IPO / acquisition predictions

## 🏢 Example: A Full Crunchbase Company

```json
{
  "id": "openai",
  "name": "OpenAI",
  "link": "https://www.crunchbase.com/organization/openai",
  "legal_name": "OpenAI OpCo, LLC",
  "short_description": "OpenAI is an AI research and deployment company that develops advanced AI models, including ChatGPT.",
  "website": "https://openai.com",
  "email": "support@openai.com",
  "phone": "+1 800-242-8478",
  "social": { "linkedin": "https://www.linkedin.com/company/openai", "twitter": "https://x.com/OpenAI" },
  "operating_status": "active",
  "ipo_status": "private",
  "employee_count": "1001-5000",
  "location": { "city": "San Francisco", "region": "California", "country": "United States" },
  "hub_tags": ["unicorn"],
  "founders": [
    { "id": "sam-altman", "name": "Sam Altman", "link": "https://www.crunchbase.com/person/sam-altman" },
    { "id": "greg-brockman", "name": "Greg Brockman", "link": "https://www.crunchbase.com/person/greg-brockman" }
  ],
  "rank": { "overall": 4, "company": 3, "delta_90d": 2.8 },
  "funding": { "rounds_count": 14, "last_round_type": "series_unknown", "investors_count": 114, "lead_investors_count": 18 },
  "acquisitions_count": 25,
  "people": { "employees_count": 82, "advisors_count": 13, "contacts_count": 1363 },
  "growth": { "growth_score": 67, "heat_score": 90 },
  "technology": { "technologies_count": 94, "web_visits_monthly": 542685661, "patents_count": 88 },
  "products": [{ "name": "ChatGPT", "description": "A conversational AI service for generating and analyzing text, images, code, and other content through natural-language interaction." }]
}
```

*Trimmed for readability.*

## 🚀 Unlimited Free Crunchbase Data — Get It in 60 Seconds

1️⃣ Clone and install:
```bash
git clone https://github.com/omkarcloud/crunchbase-scraper
cd crunchbase-scraper
python -m pip install -r requirements.txt
```

2️⃣ Start the API:
```bash
python run.py
```

Crunchbase sits behind Cloudflare, so the first request opens Chrome for a few seconds to clear it. Every request after that is plain HTTP.

Running thousands of calls from one IP? Cloudflare can block that IP for a few hours. Set `CRUNCHBASE_PROXY=http://user:pass@host:port` and keep going.

3️⃣ Get your first data:
```bash
curl "http://localhost:8000/companies/details?company=openai"
```

```json
{
  "id": "openai",
  "uuid": "cf2c678c-b81a-80c3-10d1-9c5e76448e51",
  "name": "OpenAI",
  "link": "https://www.crunchbase.com/organization/openai",
  "legal_name": "OpenAI OpCo, LLC",
  "website": "https://openai.com",
  "email": "support@openai.com",
  "phone": "+1 800-242-8478",
  "operating_status": "active",
  "ipo_status": "private",
  "employee_count": "1001-5000",
  "location": { "city": "San Francisco", "region": "California", "country": "United States" },
  "founders": [
    { "id": "sam-altman", "name": "Sam Altman", "link": "https://www.crunchbase.com/person/sam-altman" }
  ],
  "rank": { "overall": 4, "company": 3 },
  "funding": { "rounds_count": 14, "investors_count": 114, "lead_investors_count": 18 },
  "growth": { "growth_score": 67, "heat_score": 90 }
}
```

All 47 endpoints are now live at `http://localhost:8000`.

## 📚 Endpoints

47 endpoints cover everything you need.

| Endpoint | Path | Returns |
|---|---|---|
| Company Details | `/companies/details` | The whole company profile in a single call |
| Funding Rounds / Investors | `/companies/funding`, `/companies/investors` | Every round and every investor behind a company |
| People | `/companies/people` | Employees, advisors, board and alumni with titles |
| Acquisitions / Investments | `/companies/acquisitions`, `/companies/investments` | What a company bought and what it backed |
| Similar Companies | `/companies/similar` | Competitors with a similarity score and category ranks |
| Tech Stack | `/companies/technology` | Technologies, SaaS tools, apps, web traffic and patents |
| Growth Signals | `/companies/growth` | Growth and heat score history plus IPO and funding predictions |
| News Signals | `/companies/signals` | Layoffs, key hires, launches, partnerships, lawsuits and awards |
| Subsidiaries / Offices / Lists | `/companies/subsidiaries`, `/companies/offices`, `/companies/hubs` | Corporate tree, office openings and curated lists |
| Company News / Timeline / Events | `/companies/news`, `/companies/timeline`, `/companies/events` | Every article and event a company shows up in |
| Autocomplete | `/autocomplete` | Crunchbase IDs for any company, person, round or place |
| Search Companies | `/companies/search` | 6.4M+ companies with 30+ filters, 15 per page |
| Company by Domain | `/companies/by-domain` | The company behind any website |
| Search Investors | `/investors/search` | VCs, angels and funds by type, stage and deal count |
| Person Details / Jobs / Investments | `/people/details`, `/people/jobs`, `/people/investments` | A full person profile, career and portfolio |
| Person News / Events | `/people/news`, `/people/events` | Articles and stages a person appeared on |
| Search People | `/people/search` | Founders, executives and investors with rich filters |
| Funding Rounds | `/funding-rounds/search`, `/funding-rounds/details` | Rounds by investor, industry, place, type and size |
| Acquisitions | `/acquisitions/search`, `/acquisitions/details` | Deals by buyer, target, industry and price |
| Investments | `/investments/search` | Every single investment into a company |
| IPOs / Funds | `/ipos/search`, `/ipos/details`, `/funds/search`, `/funds/details` | Listings with ticker and raise, venture funds by firm |
| Events | `/events/search`, `/events/details`, `/events/participants`, `/events/appearances` | Conferences, speakers, sponsors and exhibitors |
| Lists | `/hubs/search`, `/hubs/details` | Curated lists like Unicorn Startups with their top companies |
| Reference | `/schools/search`, `/categories/search`, `/locations/search` | School, industry and location IDs for filtering |
| Press & News | `/press/search`, `/news/latest`, `/news/search` | Tagged press coverage and Crunchbase News articles |


## 🔍 Exploring Parameters

The same API is published on RapidAPI, and its playground is the easiest place to try parameters and see raw responses. Once a request looks right, run it locally for **unlimited free** data.

1. [Subscribe to the free plan](https://rapidapi.com/OmkarCloud/api/best-crunchbase-scraper-free-1000-calls/pricing) — 1,000 calls/month, no credit card.
2. [Try the endpoints in the playground](https://rapidapi.com/OmkarCloud/api/best-crunchbase-scraper-free-1000-calls/playground) — every param is pre-filled, so you see real data in one click.
3. Copy the generated code and replace `https://best-crunchbase-scraper-free-1000-calls.p.rapidapi.com` with `http://localhost:8000`. It will now run against your local API.

```python
import requests

# generated by the playground, host swapped for the local API
response = requests.get(
    "http://localhost:8000/companies/details",
    params={"company": "openai"},
)
print(response.json())
```

## 💬 Have Questions? We Have Answers.

You're a developer — we know how hard completing a project can be. So we offer full support: just message us and we'll reply ✅ with a solution within 1 working day.

[![Message Us on WhatsApp about Crunchbase Scraper](https://raw.githubusercontent.com/omkarcloud/assets/master/images/whatsapp-us.png)](https://api.whatsapp.com/send?phone=918178804274&text=I%20need%20help%20using%20the%20Crunchbase%20Scraper%20API.)

[![Ask Us by Email about Crunchbase Scraper](https://raw.githubusercontent.com/omkarcloud/assets/master/images/ask-on-email.png)](mailto:happy.to.help@omkar.cloud?subject=Help%20with%20Crunchbase%20Scraper%20API&body=I%20need%20help%20using%20the%20Crunchbase%20Scraper%20API.)

## ⚡ Popular Scrapers by Omkar Cloud

- [**Google Maps Scraper (3,100+ GitHub Stars)**](https://github.com/omkarcloud/google-maps-scraper) — type "dentists in New York", get every business as a ready-to-call lead list: phones, emails, websites & reviews. Up to 100K free leads/month.
- [**G2 Scraper**](https://www.omkar.cloud/tools/g2-scraper) — G2 product details, ratings & AI-found contacts
- [**Website Email Contact Scraper**](https://www.omkar.cloud/tools/website-email-contact-scraper) — emails, phones & socials from any website
- [**AliExpress Scraper**](https://www.omkar.cloud/tools/aliexpress-scraper-api) — live product details, SKU variants, stock & shipping
- [**Booking Scraper**](https://www.omkar.cloud/tools/booking-scraper) — Booking.com hotels: prices, ratings, rooms & amenities
- [**Etsy Scraper**](https://www.omkar.cloud/tools/etsy-scraper) — Etsy products: prices, discounts, shops & variations

## ⭐ Love It? [Star It ⭐!](https://github.com/omkarcloud/crunchbase-scraper)

Star the repo ⭐ and become my star hero!

It's just 1 click, but it means the world to me.

[![Star us on GitHub](https://raw.githubusercontent.com/omkarcloud/google-maps-scraper/master/screenshots/star-us.png)](https://github.com/omkarcloud/crunchbase-scraper)
