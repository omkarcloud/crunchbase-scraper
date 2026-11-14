"""Crunchbase JSON normalizers: the v4 API's entity payloads ({properties,
cards}) and search rows ({uuid, properties}) -> the flat, readable shapes
the /crunchbase/* routes return.

Conventions (shared by every parser here):
  * an entity reference {uuid, value, permalink, image_id, entity_def_id}
    becomes {id, uuid, name, type, link, image} via ref()
  * money {value, currency, value_usd} becomes {amount, currency, amount_usd}
  * dates are ISO strings; a {value, precision} date keeps just the value
    (precision is exposed only where the site shows it, job start/end)
  * enum tokens are mapped to their public values (num_employees_enum
    c_00011_00050 -> "11-50", revenue_range r_00010000 -> "$10M to $50M")
  * Crunchbase's anonymous entitlement hides some numbers behind
    `preview_properties` ({field: {is_present: true}}) instead of returning
    them; those come back as null here (the search collections return the
    same fields in the clear and the routes merge them in)
  * missing / gated data is null, never "" or omitted

Intentionally skipped from the raw payloads: `image_id` of press references
(no public image), `control_tags`, `created_at`/`updated_at` of Crunchbase's
own records, `layout_id`/`facet_ids`/`is_locked` (UI state), the FAQ cards
(duplicates of the summary numbers), `recommended_search` org previews
(hubs come from the hubs list), and every `preview_properties` placeholder.
"""
import re

from crunchbase import refs

IMAGE_BASE = "https://images.crunchbase.com/image/upload/c_pad,h_256,w_256,f_auto,b_white,q_auto:eco,dpr_1/"

ENUM_LABELS = {
    "c200_evidence_present": "evidence_present",
    "c200_evidence_absent": "evidence_absent",
    "c200_improving": "improving",
    "c200_declining": "declining",
    "c200_stable": "stable",
    "c200_positive": "positive",
    "c200_negative": "negative",
    "c200_neutral": "neutral",
}


# ---- primitives -------------------------------------------------------------------

def _get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def image(image_id):
    if not image_id:
        return None
    return IMAGE_BASE + image_id


def _clean_enum(value):
    """c200_evidence_present -> evidence_present, p100_positive_high -> positive_high."""
    if not isinstance(value, str):
        return value
    if value in ENUM_LABELS:
        return ENUM_LABELS[value]
    return re.sub(r"^[cp]\d{3}_", "", value)


def ref(item, with_image=True):
    """{uuid, value, permalink, image_id, entity_def_id} -> {id, uuid, name, type, link, image}."""
    if not isinstance(item, dict):
        return None
    ident = item.get("identifier") if isinstance(item.get("identifier"), dict) and "value" not in item else item
    if not isinstance(ident, dict):
        return None
    kind = ident.get("entity_def_id")
    permalink = ident.get("permalink")
    out = {
        "id": permalink or ident.get("uuid"),
        "uuid": ident.get("uuid"),
        "name": ident.get("value"),
        "type": kind,
        "link": refs.link(kind, permalink) if kind in refs.SECTIONS.values() or kind in refs.SECTIONS else None,
    }
    if kind == "location":
        out["location_type"] = ident.get("location_type")
    if with_image:
        out["image"] = image(ident.get("image_id"))
    if out["id"] is None and out["name"] is None:
        return None
    return out


def refs_list(items, with_image=False):
    return [r for r in (ref(i, with_image) for i in (items or []) if i) if r]


def money(item):
    if not isinstance(item, dict):
        return None
    return {"amount": item.get("value"), "currency": item.get("currency"), "amount_usd": item.get("value_usd")}


def date(item):
    if isinstance(item, dict):
        return item.get("value")
    return item if isinstance(item, str) and item else None


def date_precision(item):
    return item.get("precision") if isinstance(item, dict) else None


def email(value):
    """A contact email as Crunchbase stores it, minus what a sentence left on
    it ("support@openrouter.ai." -> "support@openrouter.ai")."""
    if not isinstance(value, str):
        return None
    edge = ".,;:<>()[]\"' \t\r\n"
    value = value.strip(edge)
    if value[:7].lower() == "mailto:":          # any case; what it wrapped is trimmed again ("mailto:<a@b.c>")
        value = value[7:].strip(edge)
    return value or None


def link_value(item):
    if isinstance(item, dict):
        return item.get("value") or None
    return item or None


def location(items):
    """location_identifiers -> {city, region, country, continent, groups}."""
    out = {"city": None, "region": None, "country": None, "continent": None}
    groups = []
    for loc in items or []:
        kind = loc.get("location_type")
        if kind in out and out[kind] is None:
            out[kind] = loc.get("value")
        elif kind == "group":
            groups.append(loc.get("value"))
    out["groups"] = groups
    out["ids"] = [loc.get("permalink") for loc in (items or []) if loc.get("permalink")]
    return out if any(out[k] for k in ("city", "region", "country", "continent")) or groups else None


def press(item):
    if not isinstance(item, dict):
        return None
    return {
        "id": item.get("uuid"),
        "title": item.get("value") or item.get("title"),
        "link": item.get("source_url") or link_value(item.get("url")),
        "publisher": item.get("publisher"),
        "author": item.get("author"),
        "posted_on": item.get("posted_on"),
    }


def press_list(items):
    return [p for p in (press(i) for i in (items or [])) if p]


def employees(token):
    return refs.EMPLOYEE_LABELS.get(token, token) if token else None


def revenue(token):
    return refs.REVENUE_LABELS.get(token, token) if token else None


def categories(items):
    return [{"id": c.get("permalink"), "name": c.get("value")} for c in (items or []) if isinstance(c, dict)]


def _series(block):
    """{periods: {i1: {values, interval}, i2: …}} -> the finest series."""
    periods = _get(block, "periods") or {}
    if not periods:
        return None
    best = periods.get("i1") or next(iter(periods.values()))
    return best.get("values") if isinstance(best, dict) else None


def _social(card):
    return {k: link_value((card or {}).get(k)) for k in ("linkedin", "twitter", "facebook")}


def _founder(items):
    return [ref(i) for i in (items or []) if i]


# ---- key events (layoffs, hires, launches, partnerships, legal, awards) ------------

def key_event(item, kind):
    if not isinstance(item, dict):
        return None
    out = {
        "id": _get(item, "identifier", "uuid"),
        "type": kind,
        "title": _get(item, "identifier", "value") if kind in ("award", "legal_proceeding") else None,
        "date": item.get("key_event_date"),
        "description": item.get("description"),
        "press": press_list(item.get("press_references")),
    }
    if kind == "layoff":
        out["employees_laid_off"] = item.get("num_employees_laid_off") or item.get("num_laid_off")
    if kind == "key_employee_change":
        out["person"] = ref(item.get("person_identifier"))
    return out


# ---- organization ------------------------------------------------------------------

def _prediction(rows, kind):
    row = rows[0] if isinstance(rows, list) and rows else None
    if not row:
        return None
    return {
        "type": kind,
        "generated_on": row.get("generated_on"),
        "probability": row.get("probability_score"),
        "tier": _clean_enum(row.get("probability_tier")),
        "unicorn_status": _clean_enum(row.get("unicorn_status")),
        "media_sentiment": _clean_enum(row.get("media_sentiment")),
        "timeline": row.get("probability_score_timeseries"),
    }


def _category_ranks(card, cats):
    ranks = _get(card, "category_ranks") or {}
    by_uuid = {c.get("uuid"): c for c in cats or []}
    out = []
    for uid, r in ranks.items():
        cat = by_uuid.get(uid) or {}
        out.append({"category": cat.get("value"), "category_id": cat.get("permalink") or uid,
                    "rank": r.get("rank"), "companies_in_category": r.get("count")})
    out.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0))
    return out


def organization(data, extra=None):
    """The organization entity ({properties, cards} from layout
    organization/view_v3) -> the /companies/details shape. `extra` is the
    same company's row from the search collection (parsed by
    organization_row) and fills the numbers the cards hide."""
    props = data.get("properties") or {}
    cards = data.get("cards") or {}
    about = cards.get("company_about_fields2") or {}
    ext = cards.get("overview_fields_extended") or {}
    gh = cards.get("growth_and_heat") or {}
    fin = cards.get("company_financials_highlights") or {}
    people = cards.get("people_highlights") or {}
    tech = cards.get("technology_highlights") or {}
    ident = ref(props.get("identifier"))
    cats = about.get("categories") or ext.get("categories") or props.get("categories") or []
    extra = extra or {}
    loc = location(about.get("location_identifiers") or props.get("location_identifiers"))
    if loc:
        loc["groups"] = [g.get("value") for g in ext.get("location_group_identifiers") or []] or loc["groups"]
    hubs = [{"id": h.get("permalink"), "name": h.get("value")} for h in ext.get("hubs") or []]
    out = {
        "id": ident["id"] if ident else None,
        "uuid": ident["uuid"] if ident else None,
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "image": ident["image"] if ident else None,
        "legal_name": ext.get("legal_name"),
        "aliases": extra.get("aliases") or props.get("aliases") or None,
        "short_description": _get(cards, "about_short_description", "short_description") or props.get("short_description"),
        "description": _get(cards, "overview_description", "description"),
        "website": link_value(about.get("website")) or extra.get("website"),
        "email": email(_get(cards, "contact_fields", "contact_email")),
        "phone": _get(cards, "contact_fields", "phone_number"),
        "social": _social(cards.get("social_fields")),
        "founded_on": extra.get("founded_on"),
        "closed_on": extra.get("closed_on"),
        "operating_status": ext.get("operating_status") or extra.get("operating_status"),
        "company_type": _get(cards, "overview_company_fields", "company_type"),
        "ipo_status": about.get("ipo_status") or extra.get("ipo_status"),
        "employee_count": employees(about.get("num_employees_enum") or extra.get("employee_count_token")),
        "revenue_range": extra.get("revenue_range"),
        "categories": categories(cats) or extra.get("categories"),
        "category_groups": extra.get("category_groups") or categories(props.get("category_groups")),
        "location": loc,
        "hub_tags": ext.get("hub_tags") or [],
        "hubs": hubs,
        "founders": _founder(ext.get("founder_identifiers")),
        "rank": {
            "overall": extra.get("rank") if extra.get("rank") is not None else props.get("rank_org"),
            "company": extra.get("rank_company") if extra.get("rank_company") is not None else props.get("rank_org_company"),
            "delta_90d": props.get("rank_delta_d90") if props.get("rank_delta_d90") is not None else extra.get("rank_delta_90d"),
            "by_category": _category_ranks(cards.get("org_category_ranks"), cats),
        },
        "funding": {
            "total": extra.get("funding_total"),
            "equity_total": extra.get("equity_funding_total"),
            "stage": extra.get("funding_stage"),
            "rounds_count": fin.get("num_funding_rounds") or _get(cards, "funding_rounds_headline", "num_funding_rounds"),
            "last_round_type": about.get("last_funding_type") or _get(cards, "funding_rounds_summary", "last_funding_type"),
            "last_round_at": extra.get("last_funding_at"),
            "investors_count": fin.get("num_investors") or _get(cards, "investors_headline", "num_investors"),
            "lead_investors_count": fin.get("num_lead_investors") or _get(cards, "investors_headline", "num_lead_investors"),
            "valuation": _valuation(cards.get("current_valuation")),
        },
        "investing": {
            "investor_type": extra.get("investor_type"),
            "investor_stage": extra.get("investor_stage"),
            "investments_count": fin.get("num_investments") or _get(cards, "investments_headline", "num_investments"),
            "lead_investments_count": fin.get("num_lead_investments") or _get(cards, "investments_headline", "num_lead_investments"),
            "portfolio_count": _get(cards, "investments_headline", "num_portfolio_organizations"),
            "exits_count": extra.get("exits_count"),
            "funds_count": len(cards.get("funds_list") or []) or None,
            "diversity_spotlight_investments_count": _get(cards, "diversity_spotlight_investments_headline", "num_diversity_spotlight_investments"),
        },
        "acquisitions_count": _get(cards, "acquisitions_headline", "num_acquisitions"),
        "subsidiaries_count": _get(cards, "sub_organizations_headline", "num_sub_organizations"),
        "acquired_by": _acquired_by(cards.get("acquired_by_fields")),
        "ipo": _ipo_fields(cards.get("ipo_fields"), extra),
        "people": {
            "employees_count": people.get("num_current_positions") or _get(cards, "current_employees_summary", "num_current_positions"),
            "advisors_count": people.get("num_current_advisor_positions") or _get(cards, "advisors_summary", "num_current_advisor_positions"),
            "contacts_count": _get(cards, "contacts", "num_contacts") or people.get("num_contacts"),
            "contact_emails_count": _get(cards, "contacts", "num_contact_emails"),
            "contact_phones_count": _get(cards, "contacts", "num_contact_phones"),
        },
        "growth": {
            "growth_score": gh.get("growth_score") if gh.get("growth_score") is not None else extra.get("growth_score"),
            "growth_score_delta_90d": gh.get("growth_score_delta_d90"),
            "heat_score": gh.get("heat_score") if gh.get("heat_score") is not None else extra.get("heat_score"),
            "heat_score_delta_90d": gh.get("heat_score_delta_d90"),
        },
        "technology": {
            "technologies_count": tech.get("builtwith_num_technologies_used") or _get(cards, "builtwith_summary", "builtwith_num_technologies_used"),
            "products_count": tech.get("siftery_num_products") or _get(cards, "siftery_summary", "siftery_num_products"),
            "apps_count": _get(cards, "apptopia_summary", "apptopia_total_apps"),
            "app_downloads_monthly": tech.get("apptopia_total_downloads") or _get(cards, "apptopia_summary", "apptopia_total_downloads"),
            "app_downloads_change_monthly": _get(cards, "apptopia_app_headline", "apptopia_total_downloads_mom_pct"),
            "web_visits_monthly": tech.get("semrush_visits_latest_month") or _get(cards, "semrush_rank_headline", "semrush_visits_latest_month"),
            "web_visits_change_monthly": _get(cards, "semrush_rank_headline", "semrush_visits_mom_pct"),
            "patents_count": _get(cards, "ipqwery_summary", "ipqwery_num_patent_granted"),
            "trademarks_count": _get(cards, "ipqwery_summary", "ipqwery_num_trademark_registered"),
            "it_spend": money(_get(cards, "aberdeen_summary", "aberdeen_site_it_spend")),
        },
        "signals_count": {
            "layoffs": len(cards.get("layoff_list") or []),
            "key_employee_changes": len(cards.get("key_employee_change_list") or []),
            "product_launches": len(cards.get("product_launches") or []),
            "partnerships": len(cards.get("partnership_announcements") or []),
            "legal_proceedings": len(cards.get("legal_proceedings") or []),
            "awards": len(cards.get("awards") or []),
        },
        "articles_count": _get(cards, "overview_timeline", "count"),
        "products": [{"name": p.get("name") or _get(p, "identifier", "value"), "description": p.get("description")}
                     for p in cards.get("product") or []],
    }
    return out


def _valuation(rows):
    row = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(row, dict):
        return None
    return {"amount": money(row.get("valuation")), "date": row.get("valuation_date")}


def _acquired_by(card):
    if not card:
        return None
    acq = card.get("acquirer_identifier")
    if not acq:
        return None
    return {"acquirer": ref(acq), "announced_on": date(card.get("announced_on")),
            "price": money(card.get("price")), "acquisition": ref(card.get("identifier")) if card.get("identifier") else None}


def _ipo_fields(card, extra):
    if not card and not extra.get("stock_symbol"):
        return None
    return {
        "went_public_on": date((card or {}).get("went_public_on")) or extra.get("went_public_on"),
        "stock_exchange": (card or {}).get("stock_exchange_symbol") or extra.get("stock_exchange"),
        "stock_symbol": (card or {}).get("stock_symbol") or extra.get("stock_symbol"),
        "delisted_on": date((card or {}).get("delisted_on")) or extra.get("delisted_on"),
    }


def organization_row(row):
    """A row of the organization.companies / investors / schools search
    (the anonymous field set) -> flat company summary."""
    p = row.get("properties") or {}
    ident = ref(p.get("identifier"))
    out = {
        "id": ident["id"] if ident else row.get("uuid"),
        "uuid": ident["uuid"] if ident else row.get("uuid"),
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "image": ident["image"] if ident else None,
        "short_description": p.get("short_description"),
        "website": link_value(p.get("website_url")),
        "aliases": p.get("aliases"),
        "founded_on": date(p.get("founded_on")),
        "closed_on": date(p.get("closed_on")),
        "operating_status": p.get("operating_status"),
        "ipo_status": p.get("ipo_status"),
        "employee_count": employees(p.get("num_employees_enum")),
        "employee_count_token": p.get("num_employees_enum"),
        "revenue_range": revenue(p.get("revenue_range")),
        "categories": categories(p.get("categories")),
        "category_groups": categories(p.get("category_groups")),
        "location": location(p.get("location_identifiers")),
        "rank": p.get("rank_org"),
        "rank_company": p.get("rank_org_company"),
        "rank_school": p.get("rank_org_school"),
        "rank_delta_90d": p.get("rank_delta_d90"),
        "funding_total": money(p.get("funding_total")),
        "equity_funding_total": money(p.get("last_equity_funding_total")),
        "funding_stage": p.get("funding_stage"),
        "last_funding_type": p.get("last_funding_type"),
        "last_funding_at": date(p.get("last_funding_at")),
        "investors": refs_list(p.get("investor_identifiers")),
        "founders": refs_list(p.get("founder_identifiers")),
        "acquirer": ref(p.get("acquirer_identifier")),
        "growth_score": p.get("growth_score"),
        "heat_score": p.get("heat_score"),
        "investor_type": p.get("investor_type"),
        "investor_stage": p.get("investor_stage"),
        "exits_count": p.get("num_exits"),
        "investments_count": p.get("num_investments_funding_rounds"),
        "contacts_count": p.get("num_contacts"),
        "went_public_on": date(p.get("went_public_on")),
        "delisted_on": date(p.get("delisted_on")),
        "stock_exchange": p.get("stock_exchange_symbol"),
        "stock_symbol": p.get("stock_symbol"),
        "exited_on": date(p.get("exited_on")),
        "diversity_spotlight_investments_count": p.get("num_diversity_spotlight_investments"),
        "facets": p.get("facet_ids"),
    }
    return out


def organization_summary(row):
    """The row shape used in search listings (drops the merge-only keys)."""
    out = organization_row(row)
    for k in ("employee_count_token", "rank_school", "facets", "exited_on"):
        out.pop(k, None)
    return out


# ---- organization list cards ---------------------------------------------------------

def investment_row(item):
    """investors_list / investments_list / partner_investments_list row."""
    if not isinstance(item, dict):
        return None
    return {
        "id": _get(item, "identifier", "permalink") or _get(item, "identifier", "uuid"),
        "name": _get(item, "identifier", "value"),
        "investor": ref(item.get("investor_identifier")),
        "company": ref(item.get("organization_identifier")),
        "funding_round": ref(item.get("funding_round_identifier"), with_image=False),
        "is_lead_investor": item.get("is_lead_investor"),
        "partners": refs_list(item.get("partner_identifiers")),
        "announced_on": date(item.get("announced_on")),
        "amount_invested": money(item.get("money_invested")),
        "round_amount": money(item.get("funding_round_money_raised")),
        "round_type": item.get("funding_round_investment_type"),
        "diversity_spotlights": [d.get("value") for d in item.get("organization_diversity_spotlights") or []] or None,
    }


def job_row(item):
    if not isinstance(item, dict):
        return None
    return {
        "id": _get(item, "identifier", "permalink") or _get(item, "identifier", "uuid"),
        "person": ref(item.get("person_identifier")),
        "company": ref(item.get("organization_identifier")),
        "title": item.get("title") or _title_from_name(_get(item, "identifier", "value")),
        "job_type": item.get("job_type"),
        "started_on": date(item.get("started_on")),
        "started_on_precision": date_precision(item.get("started_on")),
        "ended_on": date(item.get("ended_on")),
        "ended_on_precision": date_precision(item.get("ended_on")),
        "is_current": item.get("is_current"),
        "featured_order": item.get("employee_featured_order"),
    }


def _title_from_name(name):
    """'Steffan Tomlinson Chief Financial Officer @ Stripe' -> 'Chief Financial Officer' is
    not recoverable without the person name, so only the '@'-less part is kept."""
    if not name or " @ " not in name:
        return None
    return name.split(" @ ")[0] or None


def acquisition_row(item):
    if not isinstance(item, dict):
        return None
    return {
        "id": _get(item, "identifier", "permalink") or _get(item, "identifier", "uuid"),
        "name": _get(item, "identifier", "value"),
        "link": refs.link("acquisition", _get(item, "identifier", "permalink")),
        "acquirer": ref(item.get("acquirer_identifier")),
        "acquiree": ref(item.get("acquiree_identifier")),
        "announced_on": date(item.get("announced_on")),
        "completed_on": date(item.get("completed_on")),
        "price": money(item.get("price")),
        "acquisition_type": item.get("acquisition_type"),
        "status": item.get("status") or item.get("acquisition_status"),
        "terms": item.get("terms"),
    }


def similar_row(item):
    """org_similarity_list row -> {company, score, reasons, micro_categories}."""
    if not isinstance(item, dict):
        return None
    target = ref(item.get("target"))
    return {
        "company": target,
        "score": item.get("score"),
        "reasons": item.get("additional_reasons") or [],
        "micro_categories": [c.get("value") for c in item.get("target_micro_categories") or []],
        "location": location(item.get("target_locations")),
        "contacts_count": item.get("target_num_contacts"),
    }


def office_row(item):
    if not isinstance(item, dict):
        return None
    return {
        "id": _get(item, "identifier", "uuid"),
        "street": " ".join(s for s in (item.get("street_1"), item.get("street_2")) if s) or None,
        "city": item.get("city"),
        "region": item.get("region"),
        "country": item.get("country"),
        "postal_code": item.get("postal_code"),
        "location": location(item.get("location_identifiers")),
        "is_headquarters": item.get("is_headquarters"),
        "opened_on": item.get("opening_date"),
        "opening_description": item.get("opening_description"),
        "opening_press": press(item.get("opening_press_reference")),
    }


def technology_row(item):
    return {
        "name": _get(item, "identifier", "value"),
        "categories": item.get("technology_category") or [],
        "companies_using": item.get("num_companies_using"),
    }


def product_row(item):
    return {
        "name": _get(item, "product_identifier", "value"),
        "image": image(_get(item, "product_identifier", "image_id")),
        "customers_count": item.get("product_num_customers"),
        "status": item.get("status"),
    }


def app_row(item):
    return {
        "name": _get(item, "identifier", "value"),
        "image": image(_get(item, "identifier", "image_id")),
        "stores": item.get("stores") or [],
        "downloads_monthly": item.get("monthly_downloads"),
    }


def intent_row(item):
    return {
        "topic": _get(item, "topic_identifier", "value"),
        "category": item.get("category"),
        "surge_score": item.get("surge_score"),
        "weeks_surging": item.get("weeks_surging"),
    }


def timeline_row(item):
    """overview_timeline / timeline entity -> {type, date, title, link, publisher, author, entities}."""
    p = (item or {}).get("properties") or {}
    ap = p.get("activity_properties") or {}
    kind = p.get("entity_def_id") or _get(p, "identifier", "entity_def_id")
    return {
        "id": _get(p, "identifier", "uuid"),
        "type": kind,
        "date": p.get("activity_date"),
        "title": ap.get("title") or _get(p, "identifier", "value"),
        "link": link_value(ap.get("url")),
        "publisher": ap.get("publisher"),
        "author": ap.get("author"),
        "entities": refs_list(p.get("activity_entities")),
    }


def growth(cards):
    gh = cards.get("growth_and_heat") or {}
    knowledge = (_get(cards, "growth_knowledge", "lookup") or [None])[0] or {}
    thesis = (_get(cards, "investment_thesis", "lookup") or [None])[0] or {}
    bucket = _get(cards, "investment_thesis", "bucket") or {}
    portfolio_status = None
    if bucket.get("values") and bucket.get("series"):
        portfolio_status = dict(zip(bucket["values"], (bucket["series"][0] or {}).get("values") or []))
    return {
        "growth_score": gh.get("growth_score"),
        "growth_score_delta_90d": gh.get("growth_score_delta_d90"),
        "growth_score_history": _series(gh.get("growth_score_history")),
        "growth_trend_365d": _series(gh.get("growth_trend_365")),
        "heat_score": gh.get("heat_score"),
        "heat_score_delta_90d": gh.get("heat_score_delta_d90"),
        "heat_score_history": _series(gh.get("heat_score_history")),
        "heat_trend_365d": _series(gh.get("heat_trend_365")),
        "predictions": [p for p in (
            _prediction(cards.get("funding_prediction"), "funding"),
            _prediction(cards.get("ipo_prediction"), "ipo"),
            _prediction(cards.get("acquisition_prediction"), "acquisition"),
            _prediction(cards.get("growth_prediction"), "growth"),
        ) if p],
        "growth_insight": {
            "generated_on": knowledge.get("generated_on"),
            "finance": _clean_enum(knowledge.get("finance")),
            "customers": _clean_enum(knowledge.get("customers")),
            "press": press_list(knowledge.get("press_references")),
        } if knowledge else None,
        "investor_insight": {
            "generated_on": _get(thesis, "identifier", "value"),
            "categories": categories(thesis.get("categories")),
            "locations": [l.get("value") for l in thesis.get("locations") or []],
            "portfolio_status_counts": portfolio_status,
        } if thesis else None,
    }


# ---- person -----------------------------------------------------------------------

def person(data, extra=None):
    props = data.get("properties") or {}
    cards = data.get("cards") or {}
    ov = cards.get("overview_fields") or {}
    ov2 = cards.get("overview_fields_v2") or {}
    social = cards.get("overview_fields2") or {}
    inv = cards.get("investor_overview_headline") or {}
    ident = ref(props.get("identifier"))
    extra = extra or {}
    loc = location(ov.get("location_identifiers") or props.get("location_identifiers"))
    if loc:
        loc["groups"] = [g.get("value") for g in ov.get("location_group_identifiers") or []] or loc["groups"]
    return {
        "id": ident["id"] if ident else None,
        "uuid": ident["uuid"] if ident else None,
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "image": ident["image"] if ident else None,
        "first_name": props.get("first_name"),
        "last_name": props.get("last_name"),
        "aliases": extra.get("aliases"),
        "title": ov2.get("primary_job_title") or props.get("primary_job_title") or extra.get("title"),
        "company": ref(ov2.get("primary_organization") or props.get("primary_organization")) or extra.get("company"),
        "short_description": props.get("short_description") or extra.get("short_description"),
        "description": _get(cards, "overview_description", "description"),
        "gender": ov.get("gender") or props.get("gender"),
        "born_on": date(ov.get("born_on") or props.get("born_on")),
        "location": loc,
        "website": link_value(social.get("website")),
        "social": _social(social),
        "rank": extra.get("rank"),
        "is_investor": "investor" in (props.get("facet_ids") or extra.get("facets") or []),
        "investor_type": extra.get("investor_type"),
        "investor_stage": extra.get("investor_stage"),
        "investor_rank": inv.get("rank_principal_investor"),
        "jobs": {
            "current_count": _get(cards, "jobs_headline", "num_current_jobs"),
            "past_count": _get(cards, "jobs_headline", "num_past_jobs"),
            "current_advisor_count": _get(cards, "board_and_advisory_roles_headline", "num_current_advisor_jobs"),
            "past_advisor_count": _get(cards, "board_and_advisory_roles_headline", "num_past_advisor_jobs"),
        },
        "investing": {
            "investments_count": _get(cards, "investments_headline", "num_investments") or extra.get("investments_count"),
            "partner_investments_count": _get(cards, "partner_investments_headline", "num_partner_investments") or extra.get("partner_investments_count"),
            "portfolio_count": inv.get("num_portfolio_organizations"),
            "founded_companies_count": inv.get("num_founded_organizations"),
            "exits_count": extra.get("exits_count"),
            "ipo_exits_count": extra.get("ipo_exits_count"),
        },
        "event_appearances_count": _get(cards, "event_appearances_headline", "num_event_appearances"),
        "articles_count": _get(cards, "news_headline", "num_articles") or _get(cards, "timeline", "count"),
        "hubs": [{"id": _get(h, "identifier", "permalink"), "name": _get(h, "identifier", "value"), "rank": h.get("rank_hub")}
                 for h in cards.get("hubs_list") or []],
        "current_jobs": [job_row(j) for j in cards.get("current_jobs_image_list") or []],
        "past_jobs": [job_row(j) for j in cards.get("past_jobs_list") or []],
        "board_and_advisor_roles": [job_row(j) for j in (cards.get("current_board_and_advisory_roles_image_list") or [])
                                    + (cards.get("past_board_and_advisory_roles_list") or [])],
        "education": [degree_row(d) for d in cards.get("education_image_list") or []],
    }


def person_row(row):
    p = row.get("properties") or {}
    ident = ref(p.get("identifier"))
    return {
        "id": ident["id"] if ident else row.get("uuid"),
        "uuid": ident["uuid"] if ident else row.get("uuid"),
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "image": ident["image"] if ident else None,
        "title": p.get("primary_job_title"),
        "company": ref(p.get("primary_organization")),
        "short_description": p.get("short_description"),
        "aliases": p.get("aliases"),
        "location": location(p.get("location_identifiers")),
        "rank": p.get("rank_person"),
        "investor_type": p.get("investor_type"),
        "investor_stage": p.get("investor_stage"),
        "investments_count": p.get("num_investments_funding_rounds"),
        "partner_investments_count": p.get("num_partner_investments"),
        "exits_count": p.get("num_exits"),
        "ipo_exits_count": p.get("num_exits_ipo"),
        "diversity_spotlight_investments_count": p.get("num_diversity_spotlight_investments"),
        "facets": p.get("facet_ids"),
    }


def person_summary(row):
    out = person_row(row)
    out["is_investor"] = "investor" in (out.pop("facets", None) or [])
    return out


def degree_row(item):
    if not isinstance(item, dict):
        return None
    return {
        "id": _get(item, "identifier", "uuid"),
        "school": ref(item.get("school_identifier")),
        "degree": item.get("type_name") or item.get("degree_type"),
        "subject": item.get("subject"),
        "started_on": date(item.get("started_on")),
        "completed_on": date(item.get("completed_on")),
        "is_completed": item.get("is_completed"),
    }


def investor_row(row):
    """principal.investors row: an organization OR a person."""
    p = row.get("properties") or {}
    ident = ref(p.get("identifier"))
    return {
        "id": ident["id"] if ident else row.get("uuid"),
        "uuid": ident["uuid"] if ident else row.get("uuid"),
        "name": ident["name"] if ident else None,
        "type": ident["type"] if ident else None,
        "link": ident["link"] if ident else None,
        "image": ident["image"] if ident else None,
        "short_description": p.get("short_description"),
        "investor_type": p.get("investor_type"),
        "investor_stage": p.get("investor_stage"),
        "categories": categories(p.get("categories")),
        "location": location(p.get("location_identifiers")),
        "founded_on": date(p.get("founded_on")),
        "last_funding_type": p.get("last_funding_type"),
        "rank": p.get("rank_principal_investor"),
        "investments_count": p.get("num_investments_funding_rounds"),
        "exits_count": p.get("num_exits"),
        "ipo_exits_count": p.get("num_exits_ipo"),
        "diversity_spotlight_investments_count": p.get("num_diversity_spotlight_investments"),
        "facets": p.get("facet_ids"),
    }


# ---- deals -------------------------------------------------------------------------

def _investors_count(row, p):
    """A round's investor count. `num_investors` is not selectable
    anonymously and `investor_identifiers` comes back as a preview
    placeholder, but that placeholder still carries the list's `length`
    ({is_present: true, length: 16} for a 16-investor round). Search rows
    keep preview_properties beside properties, card rows inside them."""
    if p.get("num_investors") is not None:
        return p["num_investors"]
    if p.get("investor_identifiers"):
        return len(p["investor_identifiers"])
    preview = row.get("preview_properties") or p.get("preview_properties") or {}
    return _get(preview, "investor_identifiers", "length")


def funding_round_row(row):
    p = row.get("properties") or row
    ident = ref(p.get("identifier"), with_image=False)
    return {
        "id": ident["id"] if ident else row.get("uuid"),
        "uuid": ident["uuid"] if ident else row.get("uuid"),
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "company": ref(p.get("funded_organization_identifier")),
        "round_type": p.get("investment_type"),
        "announced_on": date(p.get("announced_on")),
        "closed_on": date(p.get("closed_on")),
        "amount": money(p.get("money_raised")),
        "target_amount": money(p.get("target_money_raised")),
        "pre_money_valuation": money(p.get("pre_money_valuation")),
        "post_money_valuation": money(p.get("post_money_valuation")),
        "is_equity": p.get("is_equity"),
        "investors_count": _investors_count(row, p),
        "lead_investors": refs_list(p.get("lead_investor_identifiers")),
        "investors": refs_list(p.get("investor_identifiers")),
        "company_funding_total": money(p.get("funded_organization_funding_total")),
        "company_diversity_spotlights": [d.get("value") for d in p.get("funded_organization_diversity_spotlights") or []] or None,
        "rank": p.get("rank_funding_round"),
        "short_description": p.get("short_description"),
    }


def funding_round(data, extra=None, investments=None):
    """funding_round entity (layout funding_round) + its search row.
    `investments` = every investors_list row (the entity embeds only the
    first 10; deals._round_investments walks the rest)."""
    props = data.get("properties") or {}
    cards = data.get("cards") or {}
    out = funding_round_row({"properties": {**props, **(cards.get("overview_fields_v2") or {})}})
    if extra:
        for k, v in extra.items():
            if out.get(k) in (None, [], {}) and v not in (None, [], {}):
                out[k] = v
    rows = investments if investments is not None else cards.get("investors_list") or []
    out["investments"] = [x for x in (investment_row(i) for i in rows) if x]
    # investor_identifiers is gated in the search row: list them from the investments
    out["investors"] = out["investors"] or refs_list([i.get("investor_identifier") for i in rows if isinstance(i, dict)])
    card_leads = refs_list(cards.get("lead_investors_image_list"))
    out["lead_investors"] = out["lead_investors"] or card_leads
    # The round's investors_list rows carry no is_lead_investor: flag them
    # from the lead list. The search row lists a round's leads in the clear
    # and in full (all 10 of a 10-lead round), so with it in hand an investor
    # missing from the list is not a lead; without it, only the listed leads
    # are known and everyone else stays null.
    search_leads = (extra or {}).get("lead_investors") or []
    complete = extra is not None and (bool(search_leads) or not card_leads)
    lead_ids = {r.get("uuid") for r in out["lead_investors"] if r.get("uuid")}
    for inv in out["investments"]:
        if inv.get("is_lead_investor") is None and inv.get("investor"):
            if inv["investor"].get("uuid") in lead_ids:
                inv["is_lead_investor"] = True
            elif complete:
                inv["is_lead_investor"] = False
    out["investors_count"] = _get(cards, "investors_headline", "num_investors") or out["investors_count"]
    out["press"] = [timeline_row(e) for e in _get(cards, "timeline", "entities") or []]
    out["articles_count"] = _get(cards, "timeline", "count")
    return out


def acquisition_search_row(row):
    p = row.get("properties") or {}
    out = acquisition_row({**p})
    out.update({
        "uuid": row.get("uuid"),
        "acquirer_categories": categories(p.get("acquirer_categories")),
        "acquirer_location": location(p.get("acquirer_locations")),
        "acquirer_funding_total": money(p.get("acquirer_funding_total")),
        "acquiree_categories": categories(p.get("acquiree_categories")),
        "acquiree_location": location(p.get("acquiree_locations")),
        "acquiree_funding_total": money(p.get("acquiree_funding_total")),
        "acquiree_last_funding_type": p.get("acquiree_last_funding_type"),
        "rank": p.get("rank_acquisition"),
        "short_description": p.get("short_description"),
    })
    return out


def acquisition(data, extra=None):
    props = data.get("properties") or {}
    cards = data.get("cards") or {}
    out = acquisition_search_row({"uuid": _get(props, "identifier", "uuid"),
                                  "properties": {**props, **(cards.get("overview_fields") or {})}})
    if extra:
        for k, v in extra.items():
            if out.get(k) in (None, [], {}) and v not in (None, [], {}):
                out[k] = v
    for key, card_id in (("acquirer", "acquirer_image_list"), ("acquiree", "acquiree_image_list")):
        rows = cards.get(card_id) or []
        if rows and not out.get(key):
            out[key] = ref(rows[0])
        if rows:
            out[key + "_short_description"] = rows[0].get("short_description")
    out["press"] = [timeline_row(e) for e in _get(cards, "timeline", "entities") or []]
    out["articles_count"] = _get(cards, "timeline", "count")
    return out


_IPO_TEXT = re.compile(r"went public on (\d{4}-\d{2}-\d{2})(?: and raised \$(\d[\d,]*(?:\.\d+)?))?(?:\. .*? was valued at \$(\d[\d,]*(?:\.\d+)?))?")


def _ipo_from_text(text):
    """The anonymous IPO row hides the date and amounts but its
    short_description spells them out ("X went public on 2026-09-21 and
    raised $13088478. X was valued at $52249622")."""
    m = _IPO_TEXT.search(text or "")
    if not m:
        return None, None, None
    raised = float(m.group(2).replace(",", "")) if m.group(2) else None
    valued = float(m.group(3).replace(",", "")) if m.group(3) else None
    usd = lambda v: {"amount": v, "currency": "USD", "amount_usd": v} if v is not None else None
    return m.group(1), usd(raised), usd(valued)


def ipo_row(row):
    p = row.get("properties") or {}
    ident = ref(p.get("identifier"), with_image=False)
    text_date, text_raised, text_valuation = _ipo_from_text(p.get("short_description"))
    return {
        "id": ident["id"] if ident else row.get("uuid"),
        "uuid": ident["uuid"] if ident else row.get("uuid"),
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "company": ref(p.get("organization_identifier")),
        "company_website": link_value(p.get("organization_website")),
        "went_public_on": date(p.get("went_public_on")) or text_date,
        "delisted_on": date(p.get("delisted_on")),
        "stock_exchange": p.get("stock_exchange_symbol"),
        "stock_symbol": p.get("stock_symbol"),
        "stock_full_symbol": p.get("stock_full_symbol"),
        "amount_raised": money(p.get("amount_raised")) or text_raised,
        "valuation": money(p.get("valuation") or p.get("valuation_at_ipo")) or text_valuation,
        "share_price": money(p.get("share_price")),
        "shares_sold": p.get("shares_sold"),
        "shares_outstanding": p.get("shares_outstanding"),
        "rank": p.get("rank_ipo"),
        "short_description": p.get("short_description"),
    }


def ipo(data, extra=None):
    props = data.get("properties") or {}
    cards = data.get("cards") or {}
    out = ipo_row({"uuid": _get(props, "identifier", "uuid"), "properties": {**props, **(cards.get("overview_fields") or {})}})
    if extra:
        for k, v in extra.items():
            if out.get(k) in (None, [], {}) and v not in (None, [], {}):
                out[k] = v
    out["press"] = [timeline_row(e) for e in _get(cards, "timeline", "entities") or []]
    return out


def fund_row(row):
    p = row.get("properties") or {}
    ident = ref(p.get("identifier"), with_image=False)
    return {
        "id": ident["id"] if ident else row.get("uuid"),
        "uuid": ident["uuid"] if ident else row.get("uuid"),
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "owner": ref(p.get("owner_identifier")),
        "announced_on": date(p.get("announced_on")),
        "amount": money(p.get("money_raised")),
        "investors_count": p.get("num_investors"),
        "investors": refs_list(p.get("investor_identifiers")),
        "short_description": p.get("short_description"),
    }


def fund(data, extra=None):
    props = data.get("properties") or {}
    cards = data.get("cards") or {}
    out = fund_row({"uuid": _get(props, "identifier", "uuid"), "properties": {**props, **(cards.get("overview_fields") or {})}})
    if extra:
        for k, v in extra.items():
            if out.get(k) in (None, [], {}) and v not in (None, [], {}):
                out[k] = v
    out["investors"] = out["investors"] or refs_list(cards.get("investors_image_list"))
    out["investors_count"] = _get(cards, "investors_headline", "num_investors") or out["investors_count"]
    out["press"] = [timeline_row(e) for e in _get(cards, "timeline", "entities") or []]
    return out


_INVESTMENT_PERMALINK = re.compile(r"^(?P<investor>.+?)-invested-in-(?P<round>.+--[0-9a-f]{8})--[0-9a-f]{8}$")
_INVESTMENT_NAME = re.compile(r"^(?P<investor>.+?) investment in (?P<round>.+?) - (?P<company>.+)$")


def investment_search_row(row):
    """An investments search row: the anonymous entitlement hides the
    investor / company / round references, but the row's permalink and
    name carry them ("aaron-levie-invested-in-stripe-series-b--6cf56239--0dd371ba",
    "Aaron Levie investment in Series B - Stripe"), so they are rebuilt."""
    p = row.get("properties") or {}
    out = investment_row({**p, "identifier": p.get("identifier")})
    out["uuid"] = row.get("uuid")
    permalink = _get(p, "identifier", "permalink") or ""
    name = _get(p, "identifier", "value") or ""
    m_link = _INVESTMENT_PERMALINK.match(permalink)
    m_name = _INVESTMENT_NAME.match(name)
    if m_link and not out.get("investor"):
        out["investor"] = {"id": m_link.group("investor"), "uuid": None,
                           "name": m_name.group("investor") if m_name else None, "type": None,
                           "link": None, "image": None}
    if m_link and not out.get("funding_round"):
        round_link = m_link.group("round")
        out["funding_round"] = {"id": round_link, "uuid": None,
                                "name": f"{m_name.group('round')} - {m_name.group('company')}" if m_name else None,
                                "type": "funding_round", "link": refs.link("funding_round", round_link)}
    if m_name and not out.get("company"):
        out["company"] = {"id": None, "uuid": None, "name": m_name.group("company"), "type": "organization",
                          "link": None, "image": None}
    out["investor_type"] = p.get("investor_type")
    return out


# ---- events / hubs / places -------------------------------------------------------------

def event_row(row):
    p = row.get("properties") or {}
    ident = ref(p.get("identifier"))
    return {
        "id": ident["id"] if ident else row.get("uuid"),
        "uuid": ident["uuid"] if ident else row.get("uuid"),
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "image": ident["image"] if ident else None,
        "short_description": p.get("short_description"),
        "starts_on": date(p.get("starts_on")),
        "ends_on": date(p.get("ends_on")),
        "location": location(p.get("location_identifiers")),
        "categories": categories(p.get("categories")),
        "category_groups": categories(p.get("category_groups")),
        "rank": p.get("rank_event"),
    }


def event(data):
    props = data.get("properties") or {}
    cards = data.get("cards") or {}
    ov = cards.get("overview_fields_v2") or cards.get("overview_fields") or {}
    out = event_row({"uuid": _get(props, "identifier", "uuid"), "properties": {**props, **ov}})
    out.update({
        "description": _get(cards, "overview_description", "description"),
        "event_type": ov.get("event_type"),
        "venue": ov.get("venue_name"),
        "website": link_value(ov.get("event_url")),
        "registration_link": link_value(ov.get("registration_url")),
        "organizers": refs_list(ov.get("organizer_identifiers")),
        "participants_count": {
            "sponsors": _get(cards, "sponsors_headline", "num_sponsors"),
            "speakers": _get(cards, "speakers_headline", "num_speakers"),
            "exhibitors": _get(cards, "exhibitors_headline", "num_exhibitors"),
            "contestants": _get(cards, "contestants_headline", "num_contestants"),
        },
        "sponsors": [appearance_row(a) for a in cards.get("sponsors_image_list") or []],
        "speakers": [appearance_row(a) for a in cards.get("speakers_image_list") or []],
        "exhibitors": [appearance_row(a) for a in cards.get("exhibitors_image_list") or []],
        "contestants": [appearance_row(a) for a in cards.get("contestants_image_list") or []],
        "press": [timeline_row(e) for e in _get(cards, "timeline", "entities") or []],
    })
    return out


def appearance_row(item):
    p = item.get("properties") if isinstance(item, dict) and "properties" in item else item
    if not isinstance(p, dict):
        return None
    return {
        "id": _get(p, "identifier", "permalink") or _get(p, "identifier", "uuid"),
        "participant": ref(p.get("participant_identifier")),
        "event": ref(p.get("event_identifier"), with_image=False),
        "role": p.get("appearance_type"),
        "event_starts_on": date(p.get("event_starts_on")),
        "event_location": location(p.get("event_location_identifiers")),
        "short_description": p.get("short_description"),
    }


def hub_row(row):
    p = row.get("properties") or {}
    ident = ref(p.get("identifier"))
    return {
        "id": ident["id"] if ident else row.get("uuid"),
        "uuid": ident["uuid"] if ident else row.get("uuid"),
        "name": ident["name"] if ident else None,
        "link": ident["link"] if ident else None,
        "image": ident["image"] if ident else None,
        "short_description": p.get("short_description"),
        "description": p.get("description"),
        "hub_type": p.get("type"),
        "companies_count": p.get("org_num"),
        "people_count": p.get("person_num"),
        "events_count": p.get("event_num"),
        "rank": p.get("rank_hub"),
        "categories": categories(p.get("categories")),
        "location": location(p.get("location_identifiers")),
    }


def hub(data):
    props = data.get("properties") or {}
    cards = data.get("cards") or {}
    ov = cards.get("overview_fields") or {}
    stats = cards.get("overview_org_stat_fields") or {}
    out = hub_row({"uuid": _get(props, "identifier", "uuid"), "properties": {**props, **ov}})
    out.update({
        "description": _get(cards, "overview_description", "description") or out["description"],
        "stats": {k: v for k, v in stats.items() if not isinstance(v, (dict, list))} or None,
        "funding_rounds_count": _get(cards, "org_funding_headline", "num_funding_rounds"),
        "investors_count": _get(cards, "org_investors_headline", "num_investors"),
        "acquisitions_count": _get(cards, "org_acquisitions_headline", "num_acquisitions"),
        "ipos_count": _get(cards, "org_ipos_headline", "num_ipos"),
        "people_count": _get(cards, "person_headline", "num_people") or out["people_count"],
        "events_count": _get(cards, "event_headline", "num_events") or out["events_count"],
        "trending_companies": [ref(r.get("identifier") or r) for r in cards.get("org_top_rank_delta_d30_list") or []],
        "recent_funding_rounds": [funding_round_row({"properties": r}) for r in cards.get("org_recent_funding_rounds_list") or cards.get("org_funding_rounds_list") or []],
        "investors": [ref(r.get("identifier") or r) for r in cards.get("org_investors_list") or []],
        "acquisitions": [acquisition_row(r) for r in cards.get("org_acquisitions_list") or []],
        "people": [ref(r.get("identifier") or r) for r in cards.get("person_list") or []],
        "events": [ref(r.get("identifier") or r) for r in cards.get("event_list") or []],
        "press": [timeline_row(e) for e in _get(cards, "timeline", "entities") or []],
    })
    return out


def location_row(row):
    p = row.get("properties") if isinstance(row, dict) and "properties" in row else row
    ident = ref(p.get("identifier"), with_image=False) if isinstance(p, dict) else None
    if not ident:
        return None
    return {
        "id": ident["id"],
        "uuid": ident["uuid"],
        "name": ident["name"],
        "link": ident["link"],
        "location_type": ident.get("location_type") or (p or {}).get("location_type"),
        "short_description": (p or {}).get("short_description"),
        "country_code": (p or {}).get("country_code"),
        "region_code": (p or {}).get("region_code"),
        "groups": [g.get("value") for g in (p or {}).get("groups") or []] or None,
        "companies_count": (p or {}).get("num_organizations"),
    }


def category_row(row):
    p = row.get("properties") if isinstance(row, dict) and "properties" in row else row
    ident = ref((p or {}).get("identifier"))
    if not ident:
        return None
    return {
        "id": ident["id"],
        "uuid": ident["uuid"],
        "name": ident["name"],
        "type": ident["type"],
        "link": ident["link"],
        "image": ident["image"],
        "short_description": (p or {}).get("short_description"),
        "groups": categories((p or {}).get("category_groups")) or None,
        "categories": categories((p or {}).get("categories")) or None,
    }


# ---- autocomplete / press --------------------------------------------------------------

def autocomplete_row(item):
    ident = ref(item.get("identifier"))
    if not ident:
        return None
    ident["short_description"] = item.get("short_description")
    ident["facets"] = item.get("facet_ids") or None
    if ident.get("type") == "organization":
        ident["is_investor"] = "investor" in (item.get("facet_ids") or [])
    return ident


def press_row(row):
    p = row.get("properties") or {}
    return {
        "id": row.get("uuid") or _get(p, "identifier", "uuid"),
        "title": p.get("title") or _get(p, "identifier", "value"),
        "link": link_value(p.get("url")),
        "publisher": p.get("publisher"),
        "author": p.get("author"),
        "posted_on": p.get("posted_on"),
        "thumbnail": link_value(p.get("thumbnail_url")),
        "entities": refs_list(p.get("activity_entities")),
    }


def news_post(post):
    """news.crunchbase.com WordPress post -> article."""
    import html as htmllib
    title = htmllib.unescape(_get(post, "title", "rendered") or "")
    excerpt = re.sub(r"<[^>]+>", "", _get(post, "excerpt", "rendered") or "").strip()
    embedded = post.get("_embedded") or {}
    authors = [a.get("name") for a in embedded.get("author") or [] if isinstance(a, dict) and a.get("name")]
    if not authors:
        # the co-authors plugin lists ids (ints) or {display_name} dicts; ids carry no name
        co = post.get("coauthors") or []
        authors = [a.get("display_name") or a.get("name") for a in co if isinstance(a, dict)]
        authors += [a for a in co if isinstance(a, str)]
        authors = [a for a in authors if a]
    if not authors and _get(post, "yoast_head_json", "author"):
        authors = [_get(post, "yoast_head_json", "author")]
    media = (embedded.get("wp:featuredmedia") or [None])[0] or {}
    terms = embedded.get("wp:term") or []
    cats = [t.get("name") for group in terms for t in group if t.get("taxonomy") == "category"]
    tags = [t.get("name") for group in terms for t in group if t.get("taxonomy") == "post_tag"]
    return {
        "id": post.get("id"),
        "title": title or None,
        "link": post.get("link"),
        "excerpt": htmllib.unescape(excerpt) or None,
        "published_at": post.get("date_gmt") or post.get("date"),
        "modified_at": post.get("modified_gmt") or post.get("modified"),
        "authors": authors,
        "categories": cats,
        "tags": tags,
        "image": media.get("source_url"),
    }
