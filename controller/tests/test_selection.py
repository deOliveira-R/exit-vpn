"""Candidate selection: which servers failover, rotate, a mode switch and find try, in
which order (logic)."""

import random
from dataclasses import replace

import pytest

from exit_vpn.errors import PolicyError
from exit_vpn.selection import choose_candidates, exact, find_candidates
from exit_vpn.servers import Region, Server

from harness import prefs_for

PREFS = prefs_for(["United States", "Switzerland"])


def srv(i, load, enabled=True, **kw):
    return Server(f"US-NY#{i}", "United States", "New York", f"h{i}", f"192.0.2.{i}",
                  f"k{i}", load=load, enabled=enabled, **kw)

def _cur(catalog, city="New York"):
    return catalog.filter("United States", city)[0]


def test_failover_tries_the_same_city_then_the_country_then_the_pinned_regions(catalog):
    cur = _cur(catalog)
    c = choose_candidates(catalog.servers, cur, PREFS, set(), random.Random(1))
    assert cur not in c
    countries_cities = [(s.country, s.city) for s in c]
    # same city first, then other US cities, then Switzerland
    first_other = next(i for i, (_, city) in enumerate(countries_cities) if city != "New York")
    assert all(city == "New York" for _, city in countries_cities[:first_other])
    first_ch = next(i for i, (country, _) in enumerate(countries_cities) if country == "Switzerland")
    assert all(country == "United States" for country, _ in countries_cities[:first_ch])
    assert all(country in ("United States", "Switzerland") for country, _ in countries_cities)


def test_each_tier_is_capped_and_no_server_repeats(catalog):
    c = choose_candidates(catalog.servers, _cur(catalog), PREFS, set(), random.Random(2), per_tier=3)
    assert len(c) == len({s.pubkey for s in c}) <= 9


def test_secure_core_tor_free_and_blacklisted_servers_are_excluded(catalog):
    cur = _cur(catalog)
    black = {s.pubkey for s in catalog.filter("Switzerland")[:3]}
    c = choose_candidates(catalog.servers, cur, PREFS, black, random.Random(3), per_tier=100)
    assert not any(s.secure_core or s.tor or s.free for s in c)
    assert not black & {s.pubkey for s in c}


def test_secure_core_mode_offers_only_secure_core_servers(catalog):
    """On: only Secure Core servers, through failover too."""
    p = prefs_for(["United States"], secure_core=True)
    c = choose_candidates(catalog.servers, None, p, set(), random.Random(4), scope="any", per_tier=100)
    assert c and all(s.secure_core for s in c)
    cur = next(s for s in catalog.servers if s.secure_core and s.country == "United States")
    c = choose_candidates(catalog.servers, cur, p, set(), random.Random(4), per_tier=100)
    assert c and all(s.secure_core for s in c)
    assert cur.entry in ("CH", "SE", "IS")


def test_a_p2p_profile_is_required_and_streaming_only_preferred(catalog):
    cur = next(s for s in catalog.filter("United States", "New York") if s.port_forward)
    c = choose_candidates(catalog.servers, cur, PREFS, set(), random.Random(8), per_tier=100,
                          profile=["port_forward", "stream"])
    assert c and all(s.port_forward for s in c), "never fail over from P2P to non-P2P"
    assert c[0].city == "New York"
    # streaming is soft: preferred within a tier, but non-streaming servers stay allowed
    chi = catalog.filter("United States", "Chicago")
    pool = [replace(x, stream=i % 2 == 0, port_forward=False) for i, x in enumerate(chi)]
    c = choose_candidates(pool, pool[0], PREFS, set(), random.Random(9), per_tier=100,
                          profile=["stream"])
    flags = [x.stream for x in c]
    assert True in flags and False in flags and flags == sorted(flags, reverse=True)


def test_a_p2p_profile_with_no_p2p_server_left_gives_no_candidate(catalog):
    cur = next(s for s in catalog.filter("United States", "New York") if s.port_forward)
    black = {s.pubkey for s in catalog.servers if s.port_forward}
    assert not choose_candidates(catalog.servers, cur, PREFS, black, random.Random(1),
                                 profile=["port_forward"])


def test_required_features_apply_without_a_profile(catalog):
    p = prefs_for(["United States"], require=["port_forward"])
    c = choose_candidates(catalog.servers, None, p, set(), random.Random(2), "any", per_tier=100)
    assert c and all(s.port_forward for s in c)


def test_pinned_regions_are_tried_in_priority_order(catalog):
    p = prefs_for([], regions=(Region("Switzerland"), Region("United States", "Dallas")))
    c = choose_candidates(catalog.servers, None, p, set(), random.Random(3), "any", per_tier=100)
    first_us = next(i for i, s in enumerate(c) if s.country == "United States")
    assert all(s.country == "Switzerland" for s in c[:first_us])
    assert all(s.city == "Dallas" for s in c[first_us:])
    # failover from Germany walks the pinned regions in order
    cur = catalog.filter("Germany", "Berlin")[0]
    c = choose_candidates(catalog.servers, cur, p, set(), random.Random(3), per_tier=100)
    assert [s.country for s in c if s.country != "Germany"][0] == "Switzerland"


def test_rotating_within_the_country_includes_the_same_city(catalog):
    cur = _cur(catalog)
    c = choose_candidates(catalog.servers, cur, PREFS, set(), random.Random(5), "country", per_tier=100)
    assert {s.city for s in c} >= {"New York", "Chicago"}
    assert all(s.country == "United States" for s in c)


def test_rotating_within_the_city_stays_in_the_city(catalog):
    cur = _cur(catalog, "Chicago")
    c = choose_candidates(catalog.servers, cur, PREFS, set(), random.Random(6), "city")
    assert c and all(s.city == "Chicago" for s in c)


def test_with_no_known_current_server_the_preferences_decide(catalog):
    c = choose_candidates(catalog.servers, None, PREFS, set(), random.Random(7))
    assert c and c[0].country == "United States"


def test_an_unknown_scope_is_an_error(catalog):
    with pytest.raises(ValueError):
        choose_candidates(catalog.servers, None, PREFS, set(), random.Random(), "nope")


def test_favourites_come_first_within_a_tier(catalog):
    cur = _cur(catalog, "Chicago")
    pool = [replace(s, load=10) for s in catalog.servers]
    others = [s for s in pool if s.city == "Chicago" and s.pubkey != cur.pubkey and eligible_regular(s)]
    fav = replace(others[-1], load=90)   # busiest in the city, still first: it is a favourite
    pool = [fav if s is others[-1] else s for s in pool]
    prefs = prefs_for(["United States"], favorites=[fav.name.lower()])
    for seed in range(5):
        c = choose_candidates(pool, cur, prefs, set(), random.Random(seed), "city")
        assert c[0].name == fav.name
    # not a favourite: load decides
    c = choose_candidates(pool, cur, PREFS, set(), random.Random(0), "city", per_tier=100)
    assert c[-1] == fav


def eligible_regular(s):
    return s.enabled and not (s.secure_core or s.tor or s.free)


def test_find_candidates_start_in_the_current_country_with_cities_interleaved(catalog):
    cur = catalog.filter("United States", "New York")[0]
    c = find_candidates(catalog.servers, cur, PREFS, set(), [], 30, random.Random(1))
    assert cur.pubkey not in {s.pubkey for s in c}
    us = [s for s in c if s.country == "United States"]
    assert c[:len(us)] == us, "current country first"
    assert len({s.city for s in us[:4]}) == 4, "cities interleaved"
    assert not any(s.secure_core or s.tor or s.free for s in c)


def test_find_candidates_honour_given_countries_and_the_limit(catalog):
    c = find_candidates(catalog.servers, None, PREFS, set(), ["Switzerland"], 3, random.Random(2))
    assert len(c) == 3 and all(s.country == "Switzerland" for s in c)


def test_failover_skips_servers_in_maintenance_and_prefers_low_load():
    cur = srv(0, 50)
    pool = [cur, srv(1, 95), srv(2, 10, enabled=False), srv(3, 15), srv(4, 55), srv(5, 5)]
    c = choose_candidates(pool, cur, prefs_for(["United States"]), set(), random.Random(1), "city")
    assert [s.name for s in c] == ["US-NY#5", "US-NY#3", "US-NY#4", "US-NY#1"]


# Exact picks ---------------------------------------------------------------------------
def test_an_exact_pick_lists_its_servers_least_loaded_first():
    found = [srv(1, 80), srv(2, 20), srv(3, 50)]
    assert [s.load for s in exact(found, PREFS, "US-NY#1")] == [20, 50, 80]
    assert [s.ip for s in exact(found, PREFS, "US-NY#1", "192.0.2.3")] == ["192.0.2.3"]


def test_an_exact_pick_in_maintenance_is_refused():
    with pytest.raises(PolicyError, match="maintenance"):
        exact([srv(1, 10, enabled=False)], PREFS, "US-NY#1")
