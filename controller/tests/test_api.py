"""The controller's HTTP layer: who may call it, status codes, request validation, and what it
never echoes (component: the real node behind the real app)."""

from exit_vpn.catalog import Catalog
from exit_vpn.persistence import CatalogCache

from harness import Stranger, client, conf, new_key, pushed_rows


class Down:
    async def lookup(self, ip, port):
        raise OSError("tailscaled is not answering")


# Who may call it -----------------------------------------------------------------------
async def test_only_owners_may_call_it(rig):
    await rig.svc.start()
    with client(rig, Stranger()) as c:
        r = c.get("/v1/status")
        assert r.status_code == 403 and "eve@github" in r.text


async def test_without_whois_it_answers_503(rig):
    await rig.svc.start()
    with client(rig, Down()) as c:
        assert c.get("/v1/status").status_code == 503


# Switching -----------------------------------------------------------------------------
async def test_select_answers_with_the_new_status(rig, catalog):
    await rig.svc.start()
    p2p = next(s for s in catalog.filter("United States", "Chicago") if s.port_forward)
    with client(rig) as c:
        r = c.post("/v1/select", json={"server": p2p.name, "ip": p2p.ip})
    assert r.status_code == 200, r.text
    st = r.json()
    assert (st["server"], st["endpoint"].split(":")[0]) == (p2p.name, p2p.ip)
    assert "port_forward" in st["profile"] and "port_forward" in st["features"]


async def test_select_needs_a_server_or_a_place(rig):
    await rig.svc.start()
    with client(rig) as c:
        assert c.post("/v1/select", json={}).status_code == 400


async def test_a_refused_request_is_400_with_the_reason(rig, catalog):
    await rig.svc.start()
    with client(rig) as c:
        r = c.post("/v1/select", json={"server": catalog.servers[0].name, "ip": "203.0.113.7"})
    assert r.status_code == 400 and "no server" in r.json()["detail"]


async def test_a_secure_core_switch_that_cannot_happen_is_409(rig):
    """The fixture has no Secure Core exit in Switzerland."""
    await rig.svc.start()
    with client(rig) as c:
        c.put("/v1/prefs", json={"regions": [{"country": "Switzerland"}]})
        c.post("/v1/select", json={"country": "Switzerland"})
        r = c.put("/v1/prefs", json={"secure_core": True})
    assert r.status_code == 409 and "Secure Core unchanged" in r.text and "pinned regions" in r.text


# Preferences ---------------------------------------------------------------------------
async def test_put_prefs_answers_with_the_normalised_prefs(rig):
    await rig.svc.start()
    with client(rig) as c:
        r = c.put("/v1/prefs", json={"regions": [{"country": "germany"},
                                                 {"country": "United States", "city": "dallas"}]})
    assert r.status_code == 200, r.text
    assert r.json()["regions"] == [{"country": "Germany", "city": None},
                                   {"country": "United States", "city": "Dallas"}]
    assert r.json()["countries"] == ["Germany", "United States"]


async def test_older_clients_may_give_countries_instead_of_regions(rig):
    await rig.svc.start()
    with client(rig) as c:
        r = c.put("/v1/prefs", json={"countries": ["Switzerland"]})
        assert r.json()["regions"] == [{"country": "Switzerland", "city": None}]
        both = c.put("/v1/prefs", json={"countries": ["Switzerland"], "regions": [{"country": "Germany"}]})
        assert both.status_code == 400


async def test_an_unknown_feature_is_a_422_schema_error(rig):
    await rig.svc.start()
    with client(rig) as c:
        assert c.put("/v1/prefs", json={"require": ["teleport"]}).status_code == 422
        assert c.put("/v1/prefs", json={"require": ["port_forward"]}).json()["require"] == ["port_forward"]


async def test_the_server_list_filters_by_known_features_only(rig):
    await rig.svc.start()
    with client(rig) as c:
        assert c.get("/v1/servers", params={"feature": "ipv6"}).status_code == 200
        assert c.get("/v1/servers", params={"feature": "nope"}).status_code == 400


# Keys ----------------------------------------------------------------------------------
async def test_a_key_is_never_echoed(rig):
    await rig.svc.start()
    k = new_key()
    with client(rig) as c:
        # schema violation (meta wrong type): default FastAPI would echo the body
        r = c.put("/v1/keys/a", json={"conf": conf(k), "meta": "x"})
        assert r.status_code == 422 and k not in r.text
        r = c.put("/v1/keys/a", json={"conf": conf(k, addr="10.9.9.9/32")})
        assert r.status_code == 400 and k not in r.text
        r = c.put("/v1/keys/z", json={"conf": conf(k)})
        assert r.status_code == 400
        r = c.get("/v1/keys")
        assert r.status_code == 200 and k not in r.text
        assert [s["source"] for s in r.json()["slots"]] == ["bootstrap", "bootstrap"]


# Catalog -------------------------------------------------------------------------------
async def test_a_pushed_catalog_replaces_the_list_and_is_saved(rig):
    svc = rig.svc
    await svc.start()
    with client(rig) as c:
        r = c.put("/v1/catalog", json={"provider": "proton", "servers": pushed_rows(80)})
    assert r.status_code == 200, r.text
    assert r.json()["pushed"] and r.json()["servers"] == 80
    assert svc.catalog.source.startswith("push:proton api by o@github")
    assert CatalogCache(rig.settings.catalog_file).load().pushed      # survives a restart


async def test_a_refused_push_changes_nothing(rig):
    svc = rig.svc
    await svc.start()
    before = list(svc.catalog.servers)
    with client(rig) as c:
        assert c.put("/v1/catalog", json={"provider": "mullvad", "servers": pushed_rows(80)}).status_code == 400
        assert c.put("/v1/catalog", json={"provider": "proton",
                                          "servers": pushed_rows(80, ip="10.1.1.1")}).status_code == 400
    assert svc.catalog.servers == before


async def test_reverting_returns_to_the_pinned_catalog(rig, monkeypatch):
    svc = rig.svc
    await svc.start()
    snapshot = list(svc.catalog.servers)

    async def pinned():
        return Catalog(list(snapshot), "pinned-url", 0.0)
    monkeypatch.setattr(rig.node.catalogs, "fetch_pinned", pinned)
    with client(rig) as c:
        c.put("/v1/catalog", json={"provider": "proton", "servers": pushed_rows(80)})
        r = c.delete("/v1/catalog")
    assert r.status_code == 200 and not r.json()["pushed"]
    assert svc.catalog.servers == snapshot
