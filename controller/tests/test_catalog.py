import copy

import pytest

from exit_vpn.catalog import Catalog, CatalogError, parse_gluetun


def test_parses_only_wireguard(raw_catalog):
    servers = parse_gluetun(raw_catalog)
    assert len(servers) == 55  # the 3 OpenVPN entries are skipped
    s = servers[0]
    assert s.pubkey.endswith("=") and s.ip.count(".") == 3 and s.country and s.city


def test_features_parsed(catalog):
    assert any(s.secure_core for s in catalog.servers)
    assert any(s.tor for s in catalog.servers)
    assert any(s.free for s in catalog.servers)


def test_rejects_foreign_hostnames(raw_catalog):
    bad = copy.deepcopy(raw_catalog)
    next(s for s in bad["servers"] if s["vpn"] == "wireguard")["hostname"] = "evil.example.com"
    with pytest.raises(CatalogError, match="unexpected hostname"):
        parse_gluetun(bad, ".protonvpn.net")


def test_rejects_truncated(raw_catalog):
    short = {"servers": raw_catalog["servers"][:10]}
    with pytest.raises(CatalogError, match="only"):
        parse_gluetun(short)


def test_rejects_non_catalog():
    with pytest.raises(CatalogError):
        parse_gluetun({"nope": 1})


def test_filter_and_lookup(catalog):
    us = catalog.filter(country="united states")
    assert us and all(s.country == "United States" for s in us)
    ny = catalog.filter(country="United States", city="New York")
    assert ny and all(s.city == "New York" for s in ny)
    s = ny[0]
    assert catalog.by_peer(s.pubkey, s.ip) == s
    assert catalog.by_peer(s.pubkey, "192.0.2.1") is None
    assert s in catalog.by_name(s.name.lower())
    assert "Switzerland" in catalog.countries()


def test_save_load_roundtrip(catalog, tmp_path):
    p = tmp_path / "catalog.json"
    catalog.save(p)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    again = Catalog.load(p)
    assert again.servers == catalog.servers and again.source == "fixture"


def test_shared_pubkey_identity_is_pubkey_and_ip(raw_catalog):
    """Secure Core entries reuse the exit server's key with another (entry) IP."""
    import copy
    d = copy.deepcopy(raw_catalog)
    wg = [x for x in d["servers"] if x["vpn"] == "wireguard"]
    regular = next(x for x in wg if not x.get("secure_core"))
    sc = copy.deepcopy(regular)
    sc.update(server_name="SE-US#99", secure_core=True, ips=["198.51.100.7"])
    d["servers"].append(sc)
    cat = Catalog(parse_gluetun(d))
    assert cat.by_peer(regular["wgpubkey"], regular["ips"][0]).name == regular["server_name"]
    assert cat.by_peer(regular["wgpubkey"], "198.51.100.7").name == "SE-US#99"
