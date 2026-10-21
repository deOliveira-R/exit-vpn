"""Phase 5a API: exact select, pinned regions, Secure Core mode, feature filters."""

from fastapi.testclient import TestClient

from exit_vpn.api import create_app
from exit_vpn.catalog import parse_pushed

from test_keys import AllowAll, env  # noqa: F401  (fixture)
from test_pushed_catalog import names, rows


async def test_api_exact_select_regions_and_secure_core(env):  # noqa: F811
    ctl, wg, store, slots, _, _ = env
    await ctl.init()
    app = create_app(ctl, AllowAll(), ["o@github"], slots=slots)
    p2p = next(s for s in ctl.catalog.filter("United States", "Chicago") if s.port_forward)
    with TestClient(app) as c:
        r = c.post("/v1/select", json={"server": p2p.name, "ip": p2p.ip})
        assert r.status_code == 200, r.text
        st = r.json()
        assert (st["server"], st["endpoint"].split(":")[0]) == (p2p.name, p2p.ip)
        assert "port_forward" in st["profile"] and "port_forward" in st["features"]
        assert c.post("/v1/select", json={"server": p2p.name, "ip": "203.0.113.7"}).status_code == 400
        assert c.post("/v1/select", json={"country": "Switzerland"}).json()["profile"] == []

        r = c.put("/v1/prefs", json={"regions": [{"country": "germany"},
                                                 {"country": "United States", "city": "dallas"}]})
        assert r.status_code == 200, r.text
        assert r.json()["regions"] == [{"country": "Germany", "city": None},
                                       {"country": "United States", "city": "Dallas"}]
        assert r.json()["countries"] == ["Germany", "United States"]
        assert c.put("/v1/prefs", json={"regions": [{"country": "Atlantis"}]}).status_code == 400
        assert c.put("/v1/prefs", json={"regions": [{"country": "Germany", "city": "Paris"}]}).status_code == 400
        assert c.put("/v1/prefs", json={"regions": []}).status_code == 400
        r = c.put("/v1/prefs", json={"countries": ["Switzerland"]})  # older clients
        assert r.json()["regions"] == [{"country": "Switzerland", "city": None}]
        assert c.put("/v1/prefs", json={"require": ["teleport"]}).status_code == 422
        assert c.put("/v1/prefs", json={"require": ["port_forward"]}).json()["require"] == ["port_forward"]
        c.put("/v1/prefs", json={"require": []})
        # the fixture has no Secure Core exit in Switzerland: nothing to switch to
        r = c.put("/v1/prefs", json={"secure_core": True})
        assert r.status_code == 409 and "pinned regions" in r.text and not ctl.state.prefs.secure_core
        c.put("/v1/prefs", json={"countries": ["Switzerland", "United States"]})

        n = len(ctl.state.events)
        r = c.put("/v1/prefs", json={"secure_core": True})
        assert r.status_code == 200 and r.json()["secure_core"], r.text
        # one prefs event, after the switch, with the new value
        prefs_events = [e for e in list(ctl.state.events)[n:] if e["kind"] == "prefs"]
        assert [e["prefs"]["secure_core"] for e in prefs_events] == [True]
        st = c.get("/v1/status").json()
        assert st["entry"] in ("CH", "SE", "IS") and ctl.current_server().secure_core
        assert c.post("/v1/select", json={"server": p2p.name}).status_code == 400  # mode mismatch
        assert c.put("/v1/prefs", json={"secure_core": False}).json()["secure_core"] is False
        assert not ctl.current_server().secure_core

        assert c.get("/v1/servers", params={"feature": "ipv6"}).status_code == 200
        assert c.get("/v1/servers", params={"feature": "nope"}).status_code == 400


def test_pushed_ipv6_flag():
    s = parse_pushed(rows(60, ipv6=True), ".protonvpn.net", names)
    assert s[0].ipv6 and "ipv6" in s[0].features() and s[0].to_dict()["entry"] == ""
