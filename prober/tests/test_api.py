"""The prober's HTTP layer (component): /probe, /healthz and /key/reload over a faked prober."""

from harness import BODY, FakeTunnel, prober, request


async def test_probe_runs_the_requested_target_and_returns_its_result():
    t = FakeTunnel()
    r = await request(prober(t), "POST", "/probe", BODY)
    assert r.status_code == 200 and t.attached == [("pk=", "198.51.100.1")]
    assert (r.json()["server"], r.json()["outcome"], r.json()["exit_loc"]) == ("US-NY#1", "ok", "US")


async def test_a_malformed_probe_request_is_refused():
    r = await request(prober(), "POST", "/probe", {"url": "https://www.example.com/"})
    assert r.status_code == 422


async def test_healthz_reports_idle_and_the_browser_version():
    r = await request(prober(), "GET", "/healthz")
    assert r.json() == {"ok": True, "busy": False, "browser": "155.0"}


async def test_key_reload_hands_the_current_slot_b_key_to_the_tunnel(monkeypatch, tmp_path):
    (tmp_path / "b.conf").write_text("[Interface]\nPrivateKey = abc=\nAddress = 10.2.0.2/32\n")
    monkeypatch.setenv("PROBE_VPN_CONF", str(tmp_path / "b.conf"))
    monkeypatch.setenv("KEY_DIR_B", str(tmp_path / "none"))
    p = prober()
    r = await request(p, "POST", "/key/reload")
    assert r.json() == {"ok": True} and p.tunnel.keys[0].private_key == "abc="


async def test_a_key_reload_that_fails_is_a_409_and_echoes_no_key(monkeypatch, tmp_path):
    (tmp_path / "b.conf").write_text("[Interface]\nPrivateKey = SECRETKEY=\n")   # no Address
    monkeypatch.setenv("PROBE_VPN_CONF", str(tmp_path / "b.conf"))
    monkeypatch.setenv("KEY_DIR_B", str(tmp_path / "none"))
    p = prober()
    r = await request(p, "POST", "/key/reload")
    assert r.status_code == 409 and "key reload failed" in r.text and "SECRETKEY" not in r.text
    assert p.tunnel.keys == []
