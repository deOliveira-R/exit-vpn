import httpx
import pytest

from prober import api as api_mod
from prober.classify import Observation
from prober.tunnel import TunnelError


class FakeTunnel:
    def __init__(self, fail=None):
        self.fail, self.used, self.cleared = fail, [], 0

    async def use(self, pubkey, ip):
        self.used.append((pubkey, ip))

    async def verify(self, code, forbid, warmup=20.0):
        if self.fail:
            raise TunnelError(self.fail)
        return {"exit_ip": "203.0.113.7", "exit_loc": code or "US"}

    async def clear(self):
        self.cleared += 1


class FakeBrowser:
    version = "155.0"

    def __init__(self):
        self.calls = 0

    async def probe(self, url, country):
        self.calls += 1
        return Observation(engine="browser", requested_url=url, final_url=url, status=200,
                           title="Real page", text="content", html="<html/>", html_len=90_000)

    async def close(self):
        pass


def http_returning(**kw):
    async def fake(url, timeout=15.0):
        base = dict(engine="http", requested_url=url, final_url=url, status=200,
                    title="T", text="x", html="<html/>", html_len=50_000)
        base.update(kw)
        return Observation(**base)
    return fake


BODY = {"server": {"name": "US-NY#1", "ip": "198.51.100.1", "pubkey": "pk=", "country_code": "US"},
        "url": "https://www.example.com/"}


async def call(app, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return (await c.post("/probe", json=body)).json()


@pytest.mark.parametrize("http_kw,expect_browser,final", [
    (dict(), False, "ok"),                                              # HTTP ok: done
    (dict(status=403), False, "blocked"),                               # IP-level block: done
    (dict(title="Just a moment..."), True, "ok"),                       # challenge: ask the browser
    (dict(title="", text="", html="<div id=root>", html_len=200), True, "ok"),  # inconclusive
])
async def test_auto_escalation(monkeypatch, http_kw, expect_browser, final):
    monkeypatch.setattr(api_mod, "http_probe", http_returning(**http_kw))
    t, b = FakeTunnel(), FakeBrowser()
    r = await call(api_mod.create_app(t, b), BODY)
    assert (b.calls == 1) is expect_browser
    assert r["outcome"] == final and t.used == [("pk=", "198.51.100.1")] and t.cleared == 1
    assert r["exit_loc"] == "US"


async def test_js_sites_always_use_the_browser(monkeypatch):
    monkeypatch.setattr(api_mod, "http_probe", http_returning())
    b = FakeBrowser()
    r = await call(api_mod.create_app(FakeTunnel(), b), {**BODY, "js": True})
    assert b.calls == 1 and [t["engine"] for t in r["tiers"]] == ["http", "browser"]


async def test_tunnel_failure_is_an_error_and_never_probes(monkeypatch):
    called = []

    async def never(url, timeout=15.0):
        called.append(url)
    monkeypatch.setattr(api_mod, "http_probe", never)
    t, b = FakeTunnel(fail="exit IP 1.2.3.4 is a forbidden (non-VPN) address"), FakeBrowser()
    r = await call(api_mod.create_app(t, b), BODY)
    assert r["outcome"] == "error" and "forbidden" in r["signals"][0]
    assert not called and b.calls == 0 and t.cleared == 1
