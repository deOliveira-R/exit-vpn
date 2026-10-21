"""The server catalog, from either source (the pinned gluetun list, a pushed list): parsing,
validation and lookups (logic)."""

import copy

import pytest

from exit_vpn.catalog import Catalog, CatalogError, parse_gluetun, parse_pushed

from harness import country_names, pushed_rows


# The pinned gluetun list -----------------------------------------------------------------
def test_only_wireguard_entries_are_kept(raw_catalog):
    servers = parse_gluetun(raw_catalog)
    assert len(servers) == 55  # the 3 OpenVPN entries are skipped
    s = servers[0]
    assert s.pubkey.endswith("=") and s.ip.count(".") == 3 and s.country and s.city


def test_secure_core_tor_and_free_flags_are_read(raw_catalog):
    raw = {(x["wgpubkey"], x["ips"][0]): x for x in raw_catalog["servers"] if x["vpn"] == "wireguard"}
    servers = parse_gluetun(raw_catalog)
    for s in servers:
        r = raw[(s.pubkey, s.ip)]
        assert (s.secure_core, s.tor, s.free) == (bool(r.get("secure_core")), bool(r.get("tor")),
                                                  bool(r.get("free")))
    assert any(s.secure_core for s in servers) and any(s.tor for s in servers) and any(s.free for s in servers)


def test_a_hostname_outside_the_provider_domain_is_refused(raw_catalog):
    bad = copy.deepcopy(raw_catalog)
    next(s for s in bad["servers"] if s["vpn"] == "wireguard")["hostname"] = "evil.example.com"
    with pytest.raises(CatalogError, match="unexpected hostname"):
        parse_gluetun(bad, ".protonvpn.net")


def test_a_truncated_list_is_refused(raw_catalog):
    with pytest.raises(CatalogError, match="only"):
        parse_gluetun({"servers": raw_catalog["servers"][:10]})


def test_something_that_is_not_a_catalog_is_refused():
    with pytest.raises(CatalogError):
        parse_gluetun({"nope": 1})


# A pushed list -------------------------------------------------------------------------
def test_pushed_rows_get_country_names_load_and_maintenance():
    s = parse_pushed(pushed_rows(), ".protonvpn.net", country_names)
    assert len(s) == 60 and s[1].country == "United States" and s[1].load == 7
    assert not s[0].enabled and s[1].enabled
    assert parse_pushed(pushed_rows(country_code="XX"), "", country_names)[0].country == "XX"


def test_pushed_ipv6_is_a_feature():
    s = parse_pushed(pushed_rows(60, ipv6=True), ".protonvpn.net", country_names)
    assert s[0].ipv6 and "ipv6" in s[0].features() and s[0].to_dict()["entry"] == ""


@pytest.mark.parametrize("over, msg", [
    ({"hostname": "evil.example.com"}, "hostname"),
    ({"ip": "10.0.0.1"}, "public IPv4"),
    ({"ip": "2001:db8::1"}, "public IPv4"),
    ({"pubkey": "short"}, "public key"),
])
def test_a_pushed_row_with_a_bad_field_is_refused(over, msg):
    with pytest.raises(CatalogError, match=msg):
        parse_pushed(pushed_rows(**over), ".protonvpn.net", country_names)


def test_a_pushed_list_much_shorter_than_the_current_one_is_refused():
    with pytest.raises(CatalogError, match="only 60"):
        parse_pushed(pushed_rows(60), ".protonvpn.net", country_names, current_size=1000)
    with pytest.raises(CatalogError):
        parse_pushed(pushed_rows(10), ".protonvpn.net", country_names)


# Lookups -------------------------------------------------------------------------------
def test_filter_by_country_and_city_ignores_case(catalog):
    us = catalog.filter(country="united states")
    assert us and all(s.country == "United States" for s in us)
    ny = catalog.filter(country="United States", city="New York")
    assert ny and all(s.city == "New York" for s in ny)
    assert "Switzerland" in catalog.countries()


def test_a_server_is_found_by_key_and_ip_or_by_name(catalog):
    s = catalog.filter(country="United States", city="New York")[0]
    assert catalog.by_id(s.pubkey, s.ip) == s
    assert catalog.by_id(s.pubkey, "192.0.2.1") is None
    assert s in catalog.by_name(s.name.lower())


def test_servers_sharing_a_key_are_told_apart_by_ip(raw_catalog):
    """Secure Core entries reuse the exit server's key with another (entry) IP."""
    d = copy.deepcopy(raw_catalog)
    wg = [x for x in d["servers"] if x["vpn"] == "wireguard"]
    regular = next(x for x in wg if not x.get("secure_core"))
    sc = copy.deepcopy(regular)
    sc.update(server_name="SE-US#99", secure_core=True, ips=["198.51.100.7"])
    d["servers"].append(sc)
    cat = Catalog(parse_gluetun(d))
    assert cat.by_id(regular["wgpubkey"], regular["ips"][0]).name == regular["server_name"]
    assert cat.by_id(regular["wgpubkey"], "198.51.100.7").name == "SE-US#99"


def test_an_iso_code_maps_to_the_catalog_country(catalog):
    assert catalog.country_for_code("US") == "United States"
    assert catalog.country_for_code("ch") == "Switzerland"
    assert catalog.country_for_code("zz") is None
