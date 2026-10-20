import random

import pytest

from exit_vpn.failover import choose_candidates
from exit_vpn.state import Prefs

PREFS = Prefs(["United States", "Switzerland"])


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


def test_secure_core_opt_in(catalog):
    p = Prefs(["United States"], secure_core=True)
    c = choose_candidates(catalog.servers, None, p, set(), random.Random(4), scope="any", per_tier=100)
    assert any(s.secure_core for s in c)


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
