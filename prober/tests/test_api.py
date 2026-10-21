"""The prober: one probe start to finish (tunnel session, exit check, engines, policy) and
its HTTP API, with fake parts."""

import httpx
import pytest

from prober.api import create_app
from prober.model import Outcome, Target
from prober.probe import needs_browser

from conftest import FakeExitCheck, FakeTunnel, browser, http, prober

BODY = {"server": {"name": "US-NY#1", "ip": "198.51.100.1", "pubkey": "pk=", "country_code": "US"},
        "url": "https://www.example.com/"}
TARGET = Target("US-NY#1", "198.51.100.1", "pk=", "US")


async def call(p, body=BODY, path="/probe"):
    app = create_app(p, p.browser)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return (await c.post(path, json=body)).json()


@pytest.mark.parametrize("http_kw,expect_browser,final", [
    (dict(), False, "ok"),                                              # HTTP ok: done
    (dict(status=403), False, "blocked"),                               # IP-level block: done
    (dict(title="Just a moment..."), True, "ok"),                       # challenge: ask the browser
    (dict(title="", text="", html="<div id=root>", html_len=200), True, "ok"),  # inconclusive
])
async def test_auto_escalation(http_kw, expect_browser, final):
    t, b = FakeTunnel(), browser()
    r = await call(prober(t, http_engine=http(**http_kw), browser_engine=b))
    assert (b.calls == 1) is expect_browser
    assert r["outcome"] == final and t.attached == [("pk=", "198.51.100.1")] and t.detached == 1
    assert r["exit_loc"] == "US"


def test_escalation_policy():
    assert needs_browser("browser", False, None) and not needs_browser("http", True, None)
    assert needs_browser("auto", True, Outcome.OK)
    for o in (Outcome.INCONCLUSIVE, Outcome.CHALLENGE, Outcome.SUSPICIOUS):
        assert needs_browser("auto", False, o)
    for o in (Outcome.OK, Outcome.BLOCKED, Outcome.REDIRECTED, Outcome.ERROR):
        assert not needs_browser("auto", False, o)


async def test_js_sites_always_use_the_browser():
    b = browser()
    r = await call(prober(browser_engine=b), {**BODY, "js": True})
    assert b.calls == 1 and [t["engine"] for t in r["tiers"]] == ["http", "browser"]


async def test_exit_check_failure_is_an_error_and_never_loads_the_page():
    h, b = http(), browser()
    t = FakeTunnel()
    r = await call(prober(t, FakeExitCheck(fail="exit IP 1.2.3.4 is a forbidden (non-VPN) address"), h, b))
    assert r["outcome"] == "error" and "forbidden" in r["signals"][0] and "exit_ip" not in r
    assert h.calls == b.calls == 0 and t.detached == 1


async def test_attach_failure_is_an_error():
    r = await call(prober(FakeTunnel(attach_fails="wg set: no such device")))
    assert r["outcome"] == "error" and r["signals"] == ["tunnel: wg set: no such device"]


async def test_an_engine_crash_is_an_error_result_and_the_tunnel_is_detached():
    """The browser used to open its context outside the try: a dead Chromium made the probe
    a 500 and left the peer on wg-probe."""
    t = FakeTunnel()
    p = prober(t, http_engine=http(title="Just a moment..."),
               browser_engine=browser(raises=RuntimeError("Target closed")))
    res = await p.run(TARGET, "https://www.example.com/")
    assert res.verdict.outcome == Outcome.ERROR and "Target closed" in res.verdict.signals[0]
    assert t.detached == 1 and len(res.tiers) == 2


async def test_healthz_and_key_reload(monkeypatch, tmp_path):
    p = prober()
    app = create_app(p, p.browser)
    (tmp_path / "b.conf").write_text("[Interface]\nPrivateKey = abc=\nAddress = 10.2.0.2/32\n")
    monkeypatch.setenv("PROBE_VPN_CONF", str(tmp_path / "b.conf"))
    monkeypatch.setenv("KEY_DIR_B", str(tmp_path / "none"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).json() == {"ok": True, "busy": False, "browser": "155.0"}
        assert (await c.post("/key/reload")).json() == {"ok": True}
    assert p.tunnel.keys[0].private_key == "abc="
