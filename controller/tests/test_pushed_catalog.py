"""Live catalog pushed by exitctl (PLAN Phase 3b.2)."""

import base64
import os
import random

import pytest
from fastapi.testclient import TestClient

from exit_vpn.api import create_app
from exit_vpn.catalog import Catalog, CatalogError, Server, parse_pushed
from exit_vpn.failover import choose_candidates
from exit_vpn.state import Prefs, regions_from_countries

from test_keys import AllowAll, env  # noqa: F401  (fixture)


def key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def rows(n=60, **over):
    out = []
    for i in range(n):
        r = {"name": f"US-NY#{i}", "country_code": "US", "city": "New York",
             "hostname": f"node-us-{i:02d}.protonvpn.net", "ip": f"185.159.157.{i + 1}",
             "pubkey": key(), "load": (i * 7) % 100, "enabled": i % 10 != 0}
        r.update(over)
        out.append(r)
    return out


def names(code):
    return {"US": "United States"}.get(code)


def test_parse_pushed_resolves_names_and_fields():
    s = parse_pushed(rows(), ".protonvpn.net", names)
    assert len(s) == 60 and s[1].country == "United States" and s[1].load == 7
    assert not s[0].enabled and s[1].enabled
    assert parse_pushed(rows(country_code="XX"), "", names)[0].country == "XX"


@pytest.mark.parametrize("over, msg", [
    ({"hostname": "evil.example.com"}, "hostname"),
    ({"ip": "10.0.0.1"}, "public IPv4"),
    ({"ip": "2001:db8::1"}, "public IPv4"),
    ({"pubkey": "short"}, "public key"),
])
def test_parse_pushed_rejects(over, msg):
    with pytest.raises(CatalogError, match=msg):
        parse_pushed(rows(**over), ".protonvpn.net", names)


def test_parse_pushed_refuses_a_truncated_list():
    with pytest.raises(CatalogError, match="only 60"):
        parse_pushed(rows(60), ".protonvpn.net", names, current_size=1000)
    with pytest.raises(CatalogError):
        parse_pushed(rows(10), ".protonvpn.net", names)


def test_failover_skips_disabled_and_prefers_low_load():
    def srv(i, load, enabled=True):
        return Server(f"US-NY#{i}", "United States", "New York", f"h{i}", f"192.0.2.{i}",
                      f"k{i}", load=load, enabled=enabled)
    cur = srv(0, 50)
    pool = [cur, srv(1, 95), srv(2, 10, enabled=False), srv(3, 15), srv(4, 55), srv(5, 5)]
    c = choose_candidates(pool, cur, Prefs(regions_from_countries(["United States"])), set(), random.Random(1), "city")
    assert [s.name for s in c] == ["US-NY#5", "US-NY#3", "US-NY#4", "US-NY#1"]


async def test_api_push_and_revert(env):  # noqa: F811
    ctl, wg, store, slots, _, _ = env
    await ctl.init()
    snapshot = list(ctl.catalog.servers)

    async def pinned():
        return Catalog(list(snapshot), "pinned-url", 0.0)
    ctl.s.catalog_file.parent.mkdir(parents=True, exist_ok=True)
    app = create_app(ctl, AllowAll(), ["o@github"], slots=slots, pinned=pinned)
    with TestClient(app) as c:
        r = c.put("/v1/catalog", json={"provider": "proton", "servers": rows(80)})
        assert r.status_code == 200, r.text
        assert r.json()["pushed"] and r.json()["servers"] == 80
        assert ctl.catalog.source.startswith("push:proton api by o@github")
        assert Catalog.load(ctl.s.catalog_file).pushed      # survives a restart
        assert c.put("/v1/catalog", json={"provider": "mullvad", "servers": rows(80)}).status_code == 400
        assert c.put("/v1/catalog", json={"provider": "proton",
                                          "servers": rows(80, ip="10.1.1.1")}).status_code == 400
        assert len(ctl.catalog) == 80                       # refused pushes change nothing
        r = c.delete("/v1/catalog")
        assert r.status_code == 200 and not r.json()["pushed"]
        assert ctl.catalog.servers == snapshot


def test_country_names_fall_back_to_iso():
    # an older push stored the bare code as the name: it must not win the vote
    cn = Server("CN#11", "CN", "Beijing", "node-cn-01.protonvpn.net", "192.0.2.9", key())
    us = Server("US-NY#1", "United States", "New York", "node-us-01.protonvpn.net", "192.0.2.1", key())
    cat = Catalog([cn, us])
    assert cat.country_for_code("CN") == "China" and cat.country_for_code("us") == "United States"
    assert cat.country_for_code("ZZ") is None and cat.code_for_country("china") == "CN"
    s = parse_pushed(rows(60, country_code="CN"), ".protonvpn.net", cat.country_for_code)
    assert s[0].country == "China"
