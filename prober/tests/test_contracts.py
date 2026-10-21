"""The prober's side of its contracts (PLAN R1): what it emits must match the samples in
contracts/prober/, and it must accept the request sample the controller is checked against."""

import sys
from pathlib import Path

import httpx

from prober import api as api_mod

from test_api import BODY, FakeBrowser, FakeTunnel, http_returning

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "contracts"))
import shape  # noqa: E402


async def post(app, path, body=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await (c.post(path, json=body) if body is not None else c.get(path))
        return r.status_code, r.json()


async def test_probe_response(monkeypatch):
    monkeypatch.setattr(api_mod, "http_probe", http_returning(title="Just a moment..."))
    code, r = await post(api_mod.create_app(FakeTunnel(), FakeBrowser()), "/probe", BODY)
    assert code == 200 and len(r["tiers"]) == 2     # HTTP, then the browser
    shape.check("prober/probe_response", r)


async def test_probe_response_when_the_tunnel_fails(monkeypatch):
    monkeypatch.setattr(api_mod, "http_probe", http_returning())
    code, r = await post(api_mod.create_app(FakeTunnel(fail="no handshake"), FakeBrowser()), "/probe", BODY)
    assert code == 200 and r["outcome"] == "error"
    shape.check("prober/probe_response_error", r)


async def test_healthz():
    code, r = await post(api_mod.create_app(FakeTunnel(), FakeBrowser()), "/healthz")
    assert code == 200
    shape.check("prober/healthz", r)


def test_accepts_the_request_sample():
    req = api_mod.ProbeRequest.model_validate(shape.load("prober/probe_request"))
    assert req.server.pubkey and req.url.startswith("https://")
