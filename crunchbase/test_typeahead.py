"""The search-backed typeahead (crunchbase/fetch.py autocomplete) and the
name -> uuid lookups built on it (crunchbase/shared.py), with the search
endpoint stubbed: no network."""
import pytest

from crunchbase import fetch, shared


def row(uuid, name, permalink=None, kind="organization", **props):
    return {"uuid": uuid, "properties": dict(
        props, identifier={"uuid": uuid, "value": name, "permalink": permalink or uuid, "entity_def_id": kind})}


@pytest.fixture
def searches(monkeypatch):
    """Stub fetch.search: `answer(collection, field, operator, value)` ->
    rows; every call is recorded."""
    state = {"calls": [], "answer": lambda *a: []}

    def search(collection_id, field_ids, query=None, order=None, limit=fetch.SEARCH_LIMIT, after_id=None):
        state["calls"].append({"collection": collection_id, "fields": list(field_ids), "query": query, "order": order,
                               "limit": limit})
        first = query[0]
        rows = state["answer"](collection_id, first["field_id"], first["operator_id"], first["values"][0])
        return {"count": len(rows), "entities": rows[:limit]}
    monkeypatch.setattr(fetch, "search", search)
    shared._name_cache.data.clear()
    return state


def test_ranked_collection_merges_both_operators_by_rank(searches):
    searches["answer"] = lambda coll, field, op, value: {
        "starts": [row("a", "Altmann Allers", rank_person=900)],
        "contains": [row("s", "Sam Altman", rank_person=3), row("a", "Altmann Allers", rank_person=900)],
    }[op]
    out = fetch.autocomplete("altman", "people", limit=10)
    assert [e["identifier"]["value"] for e in out["entities"]] == ["Sam Altman", "Altmann Allers"]   # deduped, best rank first
    assert out["count"] == 2
    assert {c["query"][0]["operator_id"] for c in searches["calls"]} == {"starts", "contains"}
    assert all(c["order"] == [{"field_id": "rank_person", "sort": "asc"}] for c in searches["calls"])
    assert "properties" not in out["entities"][0]                # the flat autocomplete row shape


def test_unranked_collection_puts_exact_names_then_prefixes_first(searches):
    searches["answer"] = lambda coll, field, op, value: (
        [row("1", "Finance Tech", kind="category"), row("2", "Finance", kind="category")] if op == "starts"
        else [row("3", "Quantitative Finance", kind="category"), row("2", "Finance", kind="category")])
    out = fetch.autocomplete("finance", "categories", limit=10)
    assert [e["identifier"]["value"] for e in out["entities"]] == ["Finance", "Finance Tech", "Quantitative Finance"]
    assert searches["calls"][0]["fields"] == ["identifier"]                       # nothing else is selectable there
    assert searches["calls"][0]["order"] == [{"field_id": "identifier", "sort": "asc"}]


def test_collections_are_interleaved_and_cut_to_the_limit(searches):
    searches["answer"] = lambda coll, field, op, value: (
        [] if op == "contains" else
        [row(f"o{i}", f"Stripe {i}", rank_org=i) for i in range(5)] if coll == "organizations" else
        [row(f"p{i}", f"Stripe Person {i}", kind="person", rank_person=i) for i in range(5)])
    out = fetch.autocomplete("stripe", "organizations,people", limit=4)
    assert [e["identifier"]["uuid"] for e in out["entities"]] == ["o0", "p0", "o1", "p1"]
    assert out["count"] == 10


def test_location_qualifiers_filter_on_the_description(searches):
    fetch.autocomplete("San Francisco, California", "locations", limit=5)
    query = searches["calls"][0]["query"]
    assert query[0]["values"] == ["San Francisco"]
    assert [(p["field_id"], p["operator_id"], p["values"]) for p in query[1:]] == [("short_description", "contains", ["California"])]


def test_lookup_prefers_the_single_exact_name(searches):
    searches["answer"] = lambda coll, field, op, value: (
        [row("fin", "FinTech", "fintech-e067", "category"), row("fs", "FinTech Services", kind="category")] if op == "starts" else [])
    assert shared.category_uuid("fintech") == "fin"
    assert shared.category_uuid("FinTech") == "fin"                # cached per lowered name


def test_namesake_locations_follow_the_best_ranked_hub(searches):
    places = [row(f"l{i}", "Berlin", f"berlin-{i}", "location") for i in range(fetch.SEARCH_LIMIT)]   # a full page of namesakes

    def answer(coll, field, op, value):
        if coll == "hubs":
            return [row("h", "Berlin Companies", kind="hub", rank_hub=1,
                        locations=[{"uuid": "germany", "value": "Germany"}, {"uuid": "berlin-de", "value": "Berlin"}])]
        return places if op == "starts" else []
    searches["answer"] = answer
    assert shared.location_uuid("berlin") == "berlin-de"          # not on the page: the hub knows the one meant


def test_namesake_locations_without_a_hub_go_by_company_count(searches):
    places = [row("ky", "London", "london-kentucky", "location"), row("uk", "London", "london-england", "location")]

    def answer(coll, field, op, value):
        if coll == "hubs":
            return []
        if coll == "organization.companies":                      # the count-only search per candidate
            return [row("c", "x")] * (9 if value == "uk" else 2)
        return places if op == "starts" else []
    searches["answer"] = answer
    assert shared.location_uuid("london") == "uk"


def test_too_many_namesakes_and_no_hub_is_refused_with_a_hint(searches):
    places = [row(f"l{i}", "Santa Rosa", f"santa-rosa-{i}", "location") for i in range(fetch.SEARCH_LIMIT)]
    searches["answer"] = lambda coll, field, op, value: [] if coll == "hubs" or op == "contains" else places
    with pytest.raises(ValueError, match="region or country"):
        shared.location_uuid("santa rosa")


def test_cut_pages_without_a_namesake_are_refused_not_guessed(searches):
    # two cut pages of different rows: their union (30) outnumbers the larger count (16)
    searches["answer"] = lambda coll, field, op, value: [] if coll == "hubs" else [
        row(f"{op}{i}", f"San {op} {i}", kind="location") for i in range(fetch.SEARCH_LIMIT + 1)]
    with pytest.raises(ValueError, match="region or country"):
        shared.location_uuid("san")


def test_failed_company_count_is_an_error_not_a_cached_guess(searches):
    places = [row("ky", "London", "london-kentucky", "location"), row("uk", "London", "london-england", "location")]

    def answer(coll, field, op, value):
        if coll == "hubs":
            return []
        if coll == "organization.companies":
            raise fetch.CrunchbaseUpstreamError("down")
        return places if op == "starts" else []
    searches["answer"] = answer
    with pytest.raises(fetch.CrunchbaseUpstreamError):
        shared.location_uuid("london")
    assert not shared._name_cache.data


def test_hyphenated_name_is_searched_as_written_when_the_spaced_reading_finds_nothing(searches, monkeypatch):
    def entity(collection, ident, field_ids, **kw):
        raise fetch.CrunchbaseNotFound("nope")                     # the permalink is e-commerce-275d
    monkeypatch.setattr(fetch, "entity", entity)
    searches["answer"] = lambda coll, field, op, value: (
        [row("ec", "E-Commerce", "e-commerce-275d", "category"), row("ep", "E-Commerce Platforms", kind="category")]
        if op == "starts" and value.lower().startswith("e-commerce") else [])
    assert shared.category_uuid("e-commerce") == "ec"              # "e commerce" matches nothing: the search is literal
    assert shared.category_uuid("E-Commerce Platforms") == "ep"    # a display name keeps its hyphen
    assert [c["query"][0]["values"][0] for c in searches["calls"] if c["query"][0]["operator_id"] == "starts"] == [
        "e commerce", "e-commerce", "E-Commerce Platforms"]


def test_permalink_resolves_through_the_entity_endpoint(searches, monkeypatch):
    seen = []

    def entity(collection, ident, field_ids, **kw):
        seen.append((collection, ident))
        if ident != "london-england":
            raise fetch.CrunchbaseNotFound("nope")
        return {"properties": {"identifier": {"uuid": "uk"}}}
    monkeypatch.setattr(fetch, "entity", entity)
    assert shared.location_uuid("london-england") == "uk"
    assert seen == [("locations", "london-england")] and not searches["calls"]     # no search spent on a permalink
    with pytest.raises(ValueError, match="Unknown location"):
        shared.location_uuid("nowhere")


def test_curl_validation_probes_a_search_not_the_autocomplete(monkeypatch):
    sent = []

    class Session:
        proxies = None

        def post(self, url, **kw):
            sent.append(("POST", url))
            return type("R", (), {"status_code": 200, "headers": {"content-type": "application/json"}})()

        def get(self, url, **kw):
            sent.append(("GET", url))
            return type("R", (), {"status_code": 403, "headers": {"content-type": "text/html"}})()

        def close(self):
            pass
    monkeypatch.setattr(fetch, "_new_session", lambda proxy: Session())
    assert fetch._curl_validates({"cf_clearance": "c"}, "UA Chrome/156", None) is True
    assert len(sent) == 1 and sent[0][0] == "POST" and "/v4/data/searches/organization.companies" in sent[0][1]
