"""The controller's side of the contracts (PLAN R1): its API responses must match the samples
in contracts/controller/, the requests it sends the prober must match the prober's request
sample, and it must handle the prober's response samples. Events are checked by the
`event_contract` fixture in conftest.py, for every test."""

import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from exit_vpn.api import create_app
from exit_vpn.finder import Finder, ProberClient

from conftest import KINDS, shape
from test_keys import AllowAll, conf, env, new_key  # noqa: F401  (fixture)


class SampleProber:
    """A prober that answers with the prober's own response sample."""

    async def probe(self, srv, code, url, mode, js, forbid_ips):
        return {**shape.load("prober/probe_response"), "server": srv.name}


def test_every_event_kind_has_a_sample():
    assert KINDS == set(shape.load("controller/events")), "contracts/controller/events.json"


async def test_api_responses(env):  # noqa: F811
    ctl, _, _, slots, _, _ = env
    await ctl.init()
    finder = Finder(SampleProber(), ctl.catalog.code_for_country, lambda: ["192.0.2.9"])
    app = create_app(ctl, AllowAll(), ["o@github"], finder=finder, slots=slots)
    with TestClient(app) as c:
        st = c.get("/v1/status").json()
        shape.check("controller/status", st, maps={"$.key_options"})

        shape.check("controller/servers", c.get("/v1/servers").json())
        shape.check("controller/prefs", c.get("/v1/prefs").json())

        r = c.put("/v1/keys/b", json={"conf": conf(new_key()), "meta": {"provider": "proton"}})
        assert r.status_code == 200, r.text
        # meta holds what exitctl recorded (KeyMeta in api.py): empty for a bootstrap key
        shape.check("controller/key_slot", r.json(), maps={"$.options", "$.meta.options"},
                    optional={"$.meta"})
        shape.check("controller/keys", c.get("/v1/keys").json(),
                    maps={"$.slots[].options", "$.slots[].meta.options"},
                    optional={"$.slots[].meta"})

        r = c.post("/v1/find", json={"url": "https://www.example.com/", "max_candidates": 2})
        assert r.status_code == 202, r.text
        job = r.json()
        for _ in range(100):
            job = c.get(f"/v1/find/{job['job_id']}").json()
            if job["state"] != "running":
                break
            await asyncio.sleep(0.01)
        assert job["state"] == "done" and job["results"], job
        shape.check("controller/find_job", job)


async def test_prober_requests_match_the_sample(catalog, monkeypatch):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=shape.load("prober/probe_response"))
    real = httpx.AsyncClient
    monkeypatch.setattr("exit_vpn.finder.httpx.AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    srv = catalog.filter("United States")[0]
    r = await ProberClient("http://prober").probe(srv, "US", "https://www.example.com/", "auto",
                                                   False, ["192.0.2.9"])
    shape.check("prober/probe_request", sent[0])
    assert r["outcome"] == "ok"
