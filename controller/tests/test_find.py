import asyncio
import random

import pytest

from exit_vpn.selection import find_candidates
from exit_vpn.errors import PolicyError
from exit_vpn.finder import FindService
from harness import prefs_for

REAL_SLEEP = asyncio.sleep   # the rig makes asyncio.sleep return at once
PREFS = prefs_for(["United States", "Switzerland"])


def test_find_candidates_order_and_exclusions(catalog):
    cur = catalog.filter("United States", "New York")[0]
    c = find_candidates(catalog.servers, cur, PREFS, set(), [], 30, random.Random(1))
    assert cur.pubkey not in {s.pubkey for s in c}
    us = [s for s in c if s.country == "United States"]
    assert c[:len(us)] == us, "current country first"
    assert len({s.city for s in us[:4]}) == 4, "cities interleaved"
    assert not any(s.secure_core or s.tor or s.free for s in c)


def test_find_candidates_explicit_countries_and_limit(catalog):
    c = find_candidates(catalog.servers, None, PREFS, set(), ["Switzerland"], 3, random.Random(2))
    assert len(c) == 3 and all(s.country == "Switzerland" for s in c)


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    async def probe(self, srv, *, country_code, url, mode="auto", js=False, forbid_ips=()):
        self.calls.append((srv.name, country_code, tuple(forbid_ips)))
        return {"server": srv.name, "outcome": self.outcomes.pop(0), "signals": []}


async def test_job_stops_after_two_ok(rig):
    await rig.svc.start()
    client = FakeClient(["blocked", "ok", "challenge", "ok", "ok", "ok"])
    f = FindService(rig.svc, client, rig.node.events, lambda c: "US", lambda: ["192.0.2.9"])
    job = f.start("https://x.example/", ["United States"], 6, "auto", False, "o@github")
    await job_done(job)
    v = job.view()
    assert v["state"] == "done" and v["progress"]["done"] == 4 and len(v["ok"]) == 2
    assert client.calls[0][1:] == ("US", ("192.0.2.9",))
    assert "find_started" in rig.kinds()


async def test_one_candidate_failing_does_not_fail_the_job(rig):
    """A prober answer the controller cannot use is that candidate's error, not the job's."""
    await rig.svc.start()

    class Broken(FakeClient):
        async def probe(self, srv, **kw):
            if not self.calls:
                self.calls.append(srv.name)
                raise KeyError("outcome")
            return await super().probe(srv, **kw)
    f = FindService(rig.svc, Broken(["ok", "ok"]), rig.node.events, lambda c: "US", lambda: [])
    job = f.start("https://x.example/", ["United States"], 3, "auto", False, "o@github")
    await job_done(job)
    assert job.state == "done" and job.results[0]["outcome"] == "error"
    assert [r["outcome"] for r in job.results[1:]] == ["ok", "ok"]


async def test_find_input_rules(rig):
    await rig.svc.start()
    f = FindService(rig.svc, FakeClient([]), rig.node.events, lambda c: "US", lambda: [])
    for url in ("ftp://x.example/", "https:///nohost"):
        with pytest.raises(PolicyError):
            f.start(url, [], 3, "auto", False, "o@github")
    with pytest.raises(PolicyError, match="unknown countries"):
        f.start("https://x.example/", ["Atlantis"], 3, "auto", False, "o@github")


async def job_done(job):
    for _ in range(200):
        if job.state != "running":
            return
        await REAL_SLEEP(0.01)
