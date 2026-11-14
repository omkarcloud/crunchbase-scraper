"""Marshmallow request schemas for every /crunchbase/* route.

Generic fields come from the shared schema_fields.py; this module adds the
Crunchbase reference fields and the per-route schemas. Every schema's
load() output is the kwargs dict its endpoint function takes.

ONE param per input (tripadvisor QueryOrLinkField convention, never a
sibling `url` / `id` pair) — refs.py lists every accepted form:
  company / person / investor / funding_round / acquisition / ipo / fund /
  event / hub / location / category:  permalink | uuid | crunchbase.com link

Search filters take public values (employee bands "11-50", round types
"series_a", locations and categories as permalinks or names) and are
mapped to Crunchbase's predicates by the endpoint modules. Results are 15
per page (Crunchbase's anonymous ceiling); `page` walks the sort field as
a cursor, so the sort decides how deep paging goes: rank / score / count
sorts page (to page 40), while the sorts on values Crunchbase hides from
anonymous callers (announced / founded dates, amounts) and name sorts
return page 1 only — narrow those with the date / amount filters instead.
"""
from marshmallow import fields, validate

from schema_fields import (BaseSchema, ChoiceField, CommaListField, DateField, Flag, PageField,
                           PositiveInt, QueryField, RefField, StrippedString)
from crunchbase import refs

# Choice tables as literals; the publishing tooling reads enums from this file's AST.
FUNDING_TYPES = ["pre_seed", "seed", "angel", "series_a", "series_b", "series_c", "series_d", "series_e",
                 "series_f", "series_g", "series_h", "series_i", "series_j", "series_unknown", "private_equity",
                 "debt_financing", "convertible_note", "grant", "corporate_round", "equity_crowdfunding",
                 "product_crowdfunding", "secondary_market", "post_ipo_equity", "post_ipo_debt",
                 "post_ipo_secondary", "non_equity_assistance", "initial_coin_offering", "undisclosed"]
FUNDING_STAGES = ["seed", "early_stage_venture", "late_stage_venture", "private_equity", "ipo", "m_and_a"]
EMPLOYEE_BANDS = ["1-10", "11-50", "51-100", "101-250", "251-500", "501-1000", "1001-5000", "5001-10000", "10001+"]
OPERATING_STATUSES = ["active", "closed"]
IPO_STATUSES = ["public", "private", "delisted"]
COMPANY_TYPES = ["for_profit", "non_profit"]
INVESTOR_TYPES = ["accelerator", "angel_group", "co_working_space", "corporate_venture_capital",
                  "entrepreneurship_program", "family_investment_office", "fund_of_funds", "government_office",
                  "hedge_fund", "incubator", "investment_bank", "investment_partner", "micro_vc", "pension_funds",
                  "private_equity_firm", "secondary_purchaser", "startup_competition", "syndicate",
                  "university_program", "venture_capital", "venture_debt", "angel", "individual"]
INVESTOR_STAGES = ["seed", "early_stage_venture", "late_stage_venture", "private_equity", "debt", "convertible_note",
                   "grant", "crowdfunding", "secondary_market", "initial_coin_offering", "post_ipo",
                   "non_equity_assistance"]
GENDERS = ["male", "female", "non_binary", "other", "not_provided"]
ACQUISITION_TYPES = ["acquisition", "merge", "acquihire", "lbo", "management_buyout"]
APPEARANCE_TYPES = ["sponsor", "speaker", "exhibitor", "contestant", "organizer"]
EVENT_TYPES = ["conference", "meetup", "hackathon", "demo_day", "networking", "competition", "seminar",
               "webinar", "expo", "class", "festival", "other"]
AUTOCOMPLETE_TYPES = ["all", "company", "investor", "school", "person", "funding_round", "acquisition", "ipo",
                      "fund", "event", "hub", "category", "category_group", "location", "city", "region",
                      "country", "product"]
LOCATION_TYPES = ["city", "region", "country", "group"]
COMPANY_SORTS = ["rank", "rank_overall", "founded", "last_funding", "employees", "name", "growth_score",
                 "heat_score", "went_public", "exits"]
INVESTOR_SORTS = ["rank", "investments", "exits", "name"]
PERSON_SORTS = ["rank", "investments", "partner_investments", "exits", "name"]
ROUND_SORTS = ["announced", "amount", "rank"]
ACQUISITION_SORTS = ["announced", "price", "rank"]
IPO_SORTS = ["went_public", "rank"]
FUND_SORTS = ["announced", "amount"]
EVENT_SORTS = ["starts", "rank"]
HUB_SORTS = ["rank", "name"]
PEOPLE_ROLES = ["employees", "advisors", "board", "alumni"]
SIGNAL_TYPES = ["layoffs", "key_employee_changes", "product_launches", "partnerships", "legal_proceedings", "awards"]
JOB_KINDS = ["current", "past", "board", "all"]
MAX_PAGE = 100


# ---- reference fields -----------------------------------------------------------------

class CompanyField(RefField):
    resolver = staticmethod(refs.resolve_company)


class CompanyOrNameField(RefField):
    """A company permalink, uuid or link, or its plain name (looked up by the endpoint)."""
    resolver = staticmethod(refs.resolve_company_or_name)


class PersonField(RefField):
    resolver = staticmethod(refs.resolve_person)


class EntityField(RefField):
    """Company or person (investors, event participants)."""
    resolver = staticmethod(refs.resolve_entity)


class FundingRoundField(RefField):
    resolver = staticmethod(refs.resolve_funding_round)


class AcquisitionField(RefField):
    resolver = staticmethod(refs.resolve_acquisition)


class IpoField(RefField):
    resolver = staticmethod(refs.resolve_ipo)


class FundField(RefField):
    resolver = staticmethod(refs.resolve_fund)


class EventField(RefField):
    resolver = staticmethod(refs.resolve_event)


class HubField(RefField):
    resolver = staticmethod(refs.resolve_hub)


class OptionalEntityField(EntityField):
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)


class OptionalInvestorField(OptionalEntityField):
    """An investor filter: a company or person permalink, uuid or link, or its
    plain name ("Sequoia Capital"). Names and permalinks Crunchbase does not
    have are looked up among investors (shared.entity_uuid_any)."""
    resolver = staticmethod(refs.resolve_entity_or_name)


class OptionalCompanyField(CompanyField):
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)


class OptionalCompanyOrNameField(CompanyOrNameField):
    """A company filter that also takes the company's plain name ("Open AI")."""
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)


class OptionalPersonField(PersonField):
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)


class NameOrSlugField(StrippedString):
    """A category / location given as a permalink, a uuid or a display name
    (resolved through autocomplete by the endpoint). Optional."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        kwargs.setdefault("validate", validate.Length(min=1, max=120))
        super().__init__(**kwargs)


class MoneyField(fields.Integer):
    """Whole USD amount (>= 0)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("load_default", None)
        kwargs.setdefault("validate", validate.Range(min=0))
        super().__init__(strict=False, **kwargs)


class ScoreField(fields.Integer):
    def __init__(self, **kwargs):
        kwargs.setdefault("load_default", None)
        kwargs.setdefault("validate", validate.Range(min=0, max=100))
        super().__init__(strict=False, **kwargs)


class OptionalQueryField(QueryField):
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)


def _page():
    return PageField(max_page=MAX_PAGE)


# ---- discovery --------------------------------------------------------------------------

class AutocompleteSchema(BaseSchema):
    query = QueryField(max_length=100)
    type = ChoiceField(AUTOCOMPLETE_TYPES, load_default="all")
    limit = PositiveInt(max_value=25, load_default=10)


class CompanySearchSchema(BaseSchema):
    query = OptionalQueryField(max_length=100)             # name starts with
    description = OptionalQueryField(max_length=100)       # description contains
    category = NameOrSlugField()
    category_group = NameOrSlugField()
    location = NameOrSlugField()
    founded_after = DateField()
    founded_before = DateField()
    closed_after = DateField()
    funding_stage = ChoiceField(FUNDING_STAGES)
    last_funding_type = ChoiceField(FUNDING_TYPES)
    last_funding_after = DateField()
    last_funding_before = DateField()
    employee_count = CommaListField(allowed=EMPLOYEE_BANDS, upper=False, max_items=9)
    operating_status = ChoiceField(OPERATING_STATUSES)
    ipo_status = ChoiceField(IPO_STATUSES)
    company_type = ChoiceField(COMPANY_TYPES)
    min_funding_total = MoneyField()
    max_funding_total = MoneyField()
    min_funding_rounds = PositiveInt()
    min_investors = PositiveInt()
    min_lead_investors = PositiveInt()
    min_acquisitions = PositiveInt()
    min_exits = PositiveInt()
    min_articles = PositiveInt()
    min_employees_listed = PositiveInt()
    min_contacts = PositiveInt()
    min_growth_score = ScoreField()
    min_heat_score = ScoreField()
    investor = OptionalInvestorField()
    founder = OptionalPersonField()
    hub_tag = StrippedString(load_default=None, validate=validate.Length(min=1, max=40))
    investor_type = ChoiceField(INVESTOR_TYPES)
    investor_stage = ChoiceField(INVESTOR_STAGES)
    stock_exchange = StrippedString(load_default=None, validate=validate.Length(min=1, max=20))
    sort = ChoiceField(COMPANY_SORTS, load_default="rank")
    page = _page()


class CompanyByDomainSchema(BaseSchema):
    domain = StrippedString(required=True, validate=validate.Length(min=3, max=200))


class InvestorSearchSchema(BaseSchema):
    query = OptionalQueryField(max_length=100)             # name contains
    investor_type = ChoiceField(INVESTOR_TYPES)
    investor_stage = ChoiceField(INVESTOR_STAGES)
    location = NameOrSlugField()
    category = NameOrSlugField()
    min_investments = PositiveInt()
    min_exits = PositiveInt()
    sort = ChoiceField(INVESTOR_SORTS, load_default="rank")
    page = _page()


class PeopleSearchSchema(BaseSchema):
    query = OptionalQueryField(max_length=100)             # name contains
    company = OptionalCompanyField()                        # current primary organization
    title = OptionalQueryField(max_length=100)             # job title contains
    bio = OptionalQueryField(max_length=100)               # description contains
    location = NameOrSlugField()
    gender = ChoiceField(GENDERS)
    is_investor = Flag()
    investor_type = ChoiceField(INVESTOR_TYPES)
    min_investments = PositiveInt()
    min_partner_investments = PositiveInt()
    min_exits = PositiveInt()
    min_founded = PositiveInt()
    born_after = DateField()
    born_before = DateField()
    max_rank = PositiveInt()
    sort = ChoiceField(PERSON_SORTS, load_default="rank")
    page = _page()


class SchoolSearchSchema(BaseSchema):
    query = OptionalQueryField(max_length=100)
    location = NameOrSlugField()
    founded_after = DateField()
    page = _page()


class CategorySearchSchema(BaseSchema):
    query = QueryField(max_length=60)
    limit = PositiveInt(max_value=25, load_default=15)


class LocationSearchSchema(BaseSchema):
    query = QueryField(max_length=60)
    type = ChoiceField(LOCATION_TYPES)
    limit = PositiveInt(max_value=25, load_default=15)


class HubSearchSchema(BaseSchema):
    query = OptionalQueryField(max_length=100)
    sort = ChoiceField(HUB_SORTS, load_default="rank")
    page = _page()


class HubSchema(BaseSchema):
    hub = HubField()


# ---- companies ---------------------------------------------------------------------------

class CompanySchema(BaseSchema):
    company = CompanyField()


class CompanyDetailsSchema(BaseSchema):
    company = CompanyOrNameField()


class CompanyPagedSchema(CompanySchema):
    page = _page()


class CompanyPeopleSchema(CompanySchema):
    role = ChoiceField(PEOPLE_ROLES, load_default="employees")
    page = _page()


class CompanySignalsSchema(CompanySchema):
    type = ChoiceField(SIGNAL_TYPES)


class CompanyFundingSchema(CompanySchema):
    sort = ChoiceField(ROUND_SORTS, load_default="announced")
    page = _page()


class CompanyAcquisitionsSchema(CompanySchema):
    sort = ChoiceField(ACQUISITION_SORTS, load_default="announced")
    page = _page()


class CompanyEventsSchema(CompanySchema):
    role = ChoiceField(APPEARANCE_TYPES)
    page = _page()


# ---- people ------------------------------------------------------------------------------

class PersonSchema(BaseSchema):
    person = PersonField()


class PersonJobsSchema(PersonSchema):
    kind = ChoiceField(JOB_KINDS, load_default="all")


class PersonPagedSchema(PersonSchema):
    page = _page()


class PersonEventsSchema(PersonSchema):
    role = ChoiceField(APPEARANCE_TYPES)
    page = _page()


# ---- deals -------------------------------------------------------------------------------

class FundingRoundSearchSchema(BaseSchema):
    company = OptionalCompanyOrNameField()
    investor = OptionalInvestorField()
    lead_investor = OptionalInvestorField()
    category = NameOrSlugField()
    location = NameOrSlugField()
    round_type = CommaListField(allowed=FUNDING_TYPES, upper=False, max_items=10)
    funding_stage = ChoiceField(FUNDING_STAGES)
    announced_after = DateField()
    announced_before = DateField()
    min_amount = MoneyField()
    max_amount = MoneyField()
    min_investors = PositiveInt()
    equity_only = Flag()
    sort = ChoiceField(ROUND_SORTS, load_default="announced")
    page = _page()


class FundingRoundSchema(BaseSchema):
    funding_round = FundingRoundField()


class AcquisitionSearchSchema(BaseSchema):
    acquirer = OptionalCompanyField()
    acquiree = OptionalCompanyField()
    category = NameOrSlugField()
    location = NameOrSlugField()
    acquisition_type = ChoiceField(ACQUISITION_TYPES)
    announced_after = DateField()
    announced_before = DateField()
    completed_after = DateField()
    min_price = MoneyField()
    max_price = MoneyField()
    acquiree_last_funding_type = ChoiceField(FUNDING_TYPES)
    sort = ChoiceField(ACQUISITION_SORTS, load_default="announced")
    page = _page()


class AcquisitionSchema(BaseSchema):
    acquisition = AcquisitionField()


class InvestmentSearchSchema(BaseSchema):
    company = CompanyField()                                        # every investment into this company
    funding_round = FundingRoundField(required=False, load_default=None)   # narrow to one of its rounds
    round_type = ChoiceField(FUNDING_TYPES)
    lead_only = Flag()
    page = _page()


class IpoSearchSchema(BaseSchema):
    stock_exchange = StrippedString(load_default=None, validate=validate.Length(min=1, max=20))
    stock_symbol = StrippedString(load_default=None, validate=validate.Length(min=1, max=20))
    went_public_after = DateField()
    went_public_before = DateField()
    sort = ChoiceField(IPO_SORTS, load_default="went_public")
    page = _page()


class IpoSchema(BaseSchema):
    ipo = IpoField()


class FundSearchSchema(BaseSchema):
    owner = OptionalCompanyField()
    announced_after = DateField()
    announced_before = DateField()
    min_amount = MoneyField()
    sort = ChoiceField(FUND_SORTS, load_default="announced")
    page = _page()


class FundSchema(BaseSchema):
    fund = FundField()


# ---- events / press ----------------------------------------------------------------------

class EventSearchSchema(BaseSchema):
    query = OptionalQueryField(max_length=100)
    starts_after = DateField()
    starts_before = DateField()
    location = NameOrSlugField()
    category = NameOrSlugField()
    event_type = ChoiceField(EVENT_TYPES)
    sort = ChoiceField(EVENT_SORTS, load_default="starts")
    page = _page()


class EventSchema(BaseSchema):
    event = EventField()


class EventParticipantsSchema(EventSchema):
    role = ChoiceField(APPEARANCE_TYPES)
    page = _page()


class EntityEventsSchema(BaseSchema):
    entity = EntityField()
    role = ChoiceField(APPEARANCE_TYPES)
    page = _page()


class PressSearchSchema(BaseSchema):
    query = OptionalQueryField(max_length=100)             # title contains
    entity = OptionalEntityField()
    posted_after = DateField()
    posted_before = DateField()
    page = _page()


class NewsLatestSchema(BaseSchema):
    page = _page()


class NewsSearchSchema(BaseSchema):
    query = OptionalQueryField(max_length=100)
    page = _page()
