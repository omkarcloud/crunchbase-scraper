"""The 47 Crunchbase endpoints. Every path is served with and without the
`/crunchbase` prefix, so code generated against the hosted API (paths like
/companies/details) runs unchanged against this server.

Params are validated by the same marshmallow schemas the hosted API uses
(crunchbase/schemas.py), so defaults, enums and error messages are
identical: unknown params are rejected, and ONE param per input accepts a
Crunchbase permalink, a UUID or a pasted crunchbase.com link. List
endpoints page with `page` and return count / per_page / current_page /
total_pages / next / previous first, exactly like the hosted API.
"""
import json
from urllib.parse import urlencode

from bottle import request, response, route

from schema_fields import load_query
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from crunchbase import companies, deals, discover, events, people, schemas

PREFIX = "/crunchbase"


def json_response(data, status=200):
    response.status = status
    response.content_type = "application/json"
    return json.dumps(data, ensure_ascii=False)


def query_dict():
    """The request query as unicode strings (bottle 0.12's .get() hands back
    latin-1 decoded bytes, so a UTF-8 "Zürich" would arrive as "ZÃ¼rich")."""
    return {key: request.query.getunicode(key) for key in request.query.keys()}


def _page_link(params, page):
    if not page:
        return None
    query = {k: v for k, v in params.items() if v not in (None, "")}
    query["page"] = page
    return f"{request.urlparts.scheme}://{request.urlparts.netloc}{request.path}?{urlencode(query)}"


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def paginate(result, params):
    """Lift the scraper's `pagination` block into the flat shape the hosted
    API returns: count / per_page / current_page / total_pages / next /
    previous first, then the data."""
    pagination = result.pop("pagination", None) or {}
    result.pop("count", None)
    page = _as_int(pagination.get("page")) or _as_int(params.get("page")) or 1
    total_pages = max(_as_int(pagination.get("total_pages")), 0)
    out = {
        "count": pagination.get("total_count"),
        "per_page": pagination.get("items_per_page"),
        "current_page": page,
        "total_pages": total_pages,
        "next": _page_link(params, page + 1 if page < total_pages else None),
        "previous": _page_link(params, page - 1 if page > 1 else None),
    }
    out.update(result)
    return out


def call(path, schema, impl, paginated=False):
    """Validate the query with the endpoint's schema, run it, and map every
    failure the way the hosted API does: bad params -> 400, missing entity
    -> 404, upstream failure or block -> 502, anything else -> 500."""
    params = query_dict()
    data, error = load_query(schema, params)
    if error:
        return json_response(error, 400)
    try:
        result = impl(**data)
    except ValueError as e:
        return json_response({"error": str(e)}, 400)
    except BadRequest as e:
        return json_response({"error": f"crunchbase rejected the request: {e}"}, 400)
    except NotFound as e:
        return json_response({"error": str(e) or "not found"}, 404)
    except Blocked as e:
        return json_response({"error": f"crunchbase blocked the request, retry later: {e}"}, 502)
    except UpstreamError as e:
        return json_response({"error": f"crunchbase {path} failed: {e}"}, 502)
    except Exception as e:
        return json_response({"error": f"crunchbase {path} failed: {type(e).__name__}: {e}"}, 500)
    return json_response(paginate(result, params) if paginated else result)


P = True   # paginated (page)

# (public path, request schema, endpoint function, paginated) — the order of the docs.
ENDPOINTS = [
    ("/companies/details", schemas.CompanyDetailsSchema, companies.details, False),
    ("/companies/funding", schemas.CompanyFundingSchema, companies.funding, P),
    ("/companies/investors", schemas.CompanyPagedSchema, companies.investors, P),
    ("/companies/people", schemas.CompanyPeopleSchema, companies.people, P),
    ("/companies/acquisitions", schemas.CompanyAcquisitionsSchema, companies.acquisitions, P),
    ("/companies/investments", schemas.CompanyPagedSchema, companies.investments, P),
    ("/companies/similar", schemas.CompanySchema, companies.similar, False),
    ("/companies/technology", schemas.CompanySchema, companies.technology, False),
    ("/companies/growth", schemas.CompanySchema, companies.growth, False),
    ("/companies/signals", schemas.CompanySignalsSchema, companies.signals, False),
    ("/companies/subsidiaries", schemas.CompanySchema, companies.subsidiaries, False),
    ("/companies/offices", schemas.CompanySchema, companies.offices, False),
    ("/companies/news", schemas.CompanyPagedSchema, companies.news, P),
    ("/companies/timeline", schemas.CompanyPagedSchema, companies.timeline, P),
    ("/companies/events", schemas.CompanyEventsSchema, companies.events, P),
    ("/companies/hubs", schemas.CompanySchema, companies.hubs, False),
    ("/autocomplete", schemas.AutocompleteSchema, discover.autocomplete, False),
    ("/companies/search", schemas.CompanySearchSchema, discover.search_companies, P),
    ("/companies/by-domain", schemas.CompanyByDomainSchema, discover.company_by_domain, False),
    ("/investors/search", schemas.InvestorSearchSchema, discover.search_investors, P),
    ("/people/details", schemas.PersonSchema, people.details, False),
    ("/people/jobs", schemas.PersonJobsSchema, people.jobs, False),
    ("/people/investments", schemas.PersonPagedSchema, people.investments, P),
    ("/people/news", schemas.PersonPagedSchema, people.news, P),
    ("/people/events", schemas.PersonEventsSchema, people.events, P),
    ("/people/search", schemas.PeopleSearchSchema, discover.search_people, P),
    ("/funding-rounds/search", schemas.FundingRoundSearchSchema, deals.search_funding_rounds, P),
    ("/funding-rounds/details", schemas.FundingRoundSchema, deals.funding_round_details, False),
    ("/acquisitions/search", schemas.AcquisitionSearchSchema, deals.search_acquisitions, P),
    ("/acquisitions/details", schemas.AcquisitionSchema, deals.acquisition_details, False),
    ("/investments/search", schemas.InvestmentSearchSchema, deals.search_investments, P),
    ("/ipos/search", schemas.IpoSearchSchema, deals.search_ipos, P),
    ("/ipos/details", schemas.IpoSchema, deals.ipo_details, False),
    ("/funds/search", schemas.FundSearchSchema, deals.search_funds, P),
    ("/funds/details", schemas.FundSchema, deals.fund_details, False),
    ("/events/search", schemas.EventSearchSchema, events.search_events, P),
    ("/events/details", schemas.EventSchema, events.event_details, False),
    ("/events/participants", schemas.EventParticipantsSchema, events.event_participants, P),
    ("/events/appearances", schemas.EntityEventsSchema, events.entity_events, P),
    ("/hubs/search", schemas.HubSearchSchema, discover.search_hubs, P),
    ("/hubs/details", schemas.HubSchema, discover.hub_details, False),
    ("/schools/search", schemas.SchoolSearchSchema, discover.search_schools, P),
    ("/categories/search", schemas.CategorySearchSchema, discover.search_categories, False),
    ("/locations/search", schemas.LocationSearchSchema, discover.search_locations, False),
    ("/press/search", schemas.PressSearchSchema, events.search_press, P),
    ("/news/latest", schemas.NewsLatestSchema, events.latest_news, P),
    ("/news/search", schemas.NewsSearchSchema, events.search_news, P),
]


def mount(path, schema, impl, paginated):
    """Serve one endpoint at /path and /crunchbase/path."""
    def handler():
        return call(path, schema, impl, paginated)
    handler.__name__ = "crunchbase_" + path.strip("/").replace("/", "_").replace("-", "_")
    route(path, method="GET")(handler)
    route(PREFIX + path, method="GET")(handler)


for _path, _schema, _impl, _paginated in ENDPOINTS:
    mount(_path, _schema, _impl, _paginated)


@route("/", method="GET")
@route("/health", method="GET")
def health():
    return json_response({"status": "ok", "endpoints": [p for p, *_ in ENDPOINTS]})
