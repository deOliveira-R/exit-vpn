import random

import pytest

from exit_vpn.selection import choose_candidates
from exit_vpn.servers import Region

from harness import prefs_for

PREFS = prefs_for(["United States", "Switzerland"])


def _cur(catalog, city="New York"):
    return catalog.filter("United States", city)[0]


def test_failover_tier_order(catalog):
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


def test_per_tier_limit_and_no_duplicates(catalog):
    c = choose_candidates(catalog.servers, _cur(catalog), PREFS, set(), random.Random(2), per_tier=3)
    assert len(c) == len({s.pubkey for s in c}) <= 9


def test_excludes_secure_core_tor_free_and_blacklisted(catalog):
    cur = _cur(catalog)
    black = {s.pubkey for s in catalog.filter("Switzerland")[:3]}
    c = choose_candidates(catalog.servers, cur, PREFS, black, random.Random(3), per_tier=100)
    assert not any(s.secure_core or s.tor or s.free for s in c)
    assert not black & {s.pubkey for s in c}


def test_secure_core_is_a_mode(catalog):
    """On: only Secure Core servers, through failover too; off: none (PLAN Phase 5)."""
    p = prefs_for(["United States"], secure_core=True)
    c = choose_candidates(catalog.servers, None, p, set(), random.Random(4), scope="any", per_tier=100)
    assert c and all(s.secure_core for s in c)
    cur = next(s for s in catalog.servers if s.secure_core and s.country == "United States")
    c = choose_candidates(catalog.servers, cur, p, set(), random.Random(4), per_tier=100)
    assert c and all(s.secure_core for s in c)
    assert cur.entry in ("CH", "SE", "IS")


def test_p2p_profile_is_hard_streaming_is_soft(catalog):
    cur = next(s for s in catalog.filter("United States", "New York") if s.port_forward)
    c = choose_candidates(catalog.servers, cur, PREFS, set(), random.Random(8), per_tier=100,
                          profile=["port_forward", "stream"])
    assert c and all(s.port_forward for s in c), "never fail over from P2P to non-P2P"
    assert c[0].city == "New York"
    # streaming is soft: preferred within a tier, but non-streaming servers stay allowed
    from dataclasses import replace
    chi = catalog.filter("United States", "Chicago")
    pool = [replace(x, stream=i % 2 == 0, port_forward=False) for i, x in enumerate(chi)]
    c = choose_candidates(pool, pool[0], PREFS, set(), random.Random(9), per_tier=100,
                          profile=["stream"])
    flags = [x.stream for x in c]
    assert True in flags and False in flags and flags == sorted(flags, reverse=True)


def test_p2p_profile_with_no_p2p_left_gives_nothing(catalog):
    cur = next(s for s in catalog.filter("United States", "New York") if s.port_forward)
    black = {s.pubkey for s in catalog.servers if s.port_forward}
    assert not choose_candidates(catalog.servers, cur, PREFS, black, random.Random(1),
                                 profile=["port_forward"])


def test_require_applies_without_profile(catalog):
    p = prefs_for(["United States"], require=["port_forward"])
    c = choose_candidates(catalog.servers, None, p, set(), random.Random(2), "any", per_tier=100)
    assert c and all(s.port_forward for s in c)


def test_city_regions_in_priority_order(catalog):
    p = prefs_for([], regions=(Region("Switzerland"), Region("United States", "Dallas")))
    c = choose_candidates(catalog.servers, None, p, set(), random.Random(3), "any", per_tier=100)
    first_us = next(i for i, s in enumerate(c) if s.country == "United States")
    assert all(s.country == "Switzerland" for s in c[:first_us])
    assert all(s.city == "Dallas" for s in c[first_us:])
    # failover from Germany walks the pinned regions in order
    cur = catalog.filter("Germany", "Berlin")[0]
    c = choose_candidates(catalog.servers, cur, p, set(), random.Random(3), per_tier=100)
    assert [s.country for s in c if s.country != "Germany"][0] == "Switzerland"


def test_rotate_country_includes_same_city(catalog):
    cur = _cur(catalog)
    c = choose_candidates(catalog.servers, cur, PREFS, set(), random.Random(5), "country", per_tier=100)
    assert {s.city for s in c} >= {"New York", "Chicago"}
    assert all(s.country == "United States" for s in c)


def test_rotate_city(catalog):
    cur = _cur(catalog, "Chicago")
    c = choose_candidates(catalog.servers, cur, PREFS, set(), random.Random(6), "city")
    assert c and all(s.city == "Chicago" for s in c)


def test_unknown_current_uses_preferences(catalog):
    c = choose_candidates(catalog.servers, None, PREFS, set(), random.Random(7))
    assert c and c[0].country == "United States"


def test_bad_scope(catalog):
    with pytest.raises(ValueError):
        choose_candidates(catalog.servers, None, PREFS, set(), random.Random(), "nope")


def test_favorites_come_first_within_a_tier(catalog):
    from dataclasses import replace
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
