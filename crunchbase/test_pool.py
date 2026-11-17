"""The identity pool (crunchbase/fetch.py): parallel mints under a burst,
organization quota reserved at pick time, general work spread over the
live identities, the pool grown in the background under traffic."""
import threading
import time

import pytest

import browser_client
import config
from crunchbase import fetch as cb
from scraper_utils.testing import hosted_only

PROXY = "http://u:p@gw:10001"


@pytest.fixture
def pool(monkeypatch):
    cb._identities.clear(); cb._egress_cooling.clear(); cb._failed_until = 0.0
    cb._spare_thread["t"] = None
    monkeypatch.setattr(cb, "_candidate_exits", lambda: [PROXY])
    monkeypatch.setattr(cb, "_curl_validates", lambda cookies, ua, proxy: True)
    monkeypatch.setattr(config, "CRUNCHBASE_ORG_QUOTA", 10)
    monkeypatch.setattr(config, "CRUNCHBASE_IDENTITIES", 3)
    state = {"mints": 0, "in_flight": 0, "peak": 0, "lock": threading.Lock()}

    def mint(site, proxy=None, variant=""):
        with state["lock"]:
            state["mints"] += 1; state["in_flight"] += 1; state["peak"] = max(state["peak"], state["in_flight"])
        time.sleep(0.05)
        with state["lock"]:
            state["in_flight"] -= 1
        return {"cookies": [{"name": "cf_clearance", "value": "c"}], "user_agent": "UA"}
    monkeypatch.setattr(browser_client, "mint", mint)
    yield state
    t = cb._spare_thread["t"]
    if t is not None:
        t.join(2)
    cb._identities.clear()


def test_org_pick_reserves_its_unit_atomically(pool):
    a = cb._mint()
    picks = [cb._pick(True) for _ in range(10)]
    assert all(p is a for p in picks) and a.org_uses == 10
    assert cb._pick(True) is None                           # spent: acquire() mints the next cookie


def test_org_burst_mints_in_parallel_and_every_caller_gets_a_unit(pool):
    got, errors = [], []

    def worker():
        try:
            got.append(cb.acquire(True))
        except Exception as e:
            errors.append(e)
    ths = [threading.Thread(target=worker) for _ in range(25)]
    for t in ths: t.start()
    for t in ths: t.join(5)
    assert not errors and len(got) == 25
    assert sum(i.org_uses for i in cb._identities) == 25        # one reserved unit per caller, none lost to a race
    assert all(i.org_uses <= config.CRUNCHBASE_ORG_QUOTA for i in cb._identities)
    assert 2 <= pool["peak"] <= config.CRUNCHBASE_PARALLEL_MINTS   # mints overlapped, bounded by the semaphore
    assert 3 <= pool["mints"] <= config.CRUNCHBASE_PARALLEL_MINTS + 3


def test_general_work_spreads_over_live_identities_least_recently_used_first(pool):
    a, b, c = cb._mint(), cb._mint(), cb._mint()
    picks = [cb._pick(False).key for _ in range(6)]
    assert picks == [a.key, b.key, c.key] * 2
    assert all(i.org_uses == 0 for i in (a, b, c))               # general work spends no organization quota


def test_pool_grows_in_the_background_under_general_traffic(pool):
    cb._mint()
    for expected in (2, 3, 3):
        assert cb.acquire(False) is not None
        t = cb._spare_thread["t"]
        if t is not None:
            t.join(2)
        assert len(cb._identities) == expected                   # one background mint per call, up to the target
    assert pool["mints"] == 3


@hosted_only
def test_browser_mode_offers_burst_sessions_keyed_by_the_identity(pool, monkeypatch):
    monkeypatch.setattr(cb, "_curl_validates", lambda cookies, ua, proxy: False)
    monkeypatch.setattr(config, "INPAGE_BROWSER_SLOTS", 2)
    monkeypatch.setattr(config, "BROWSER_BURST_FACTOR", 4)
    ident = cb._mint()
    assert ident.mode == "browser"
    assert cb._box_candidates(ident) == [(PROXY, f"{ident.key}#{i}") for i in range(8)]
