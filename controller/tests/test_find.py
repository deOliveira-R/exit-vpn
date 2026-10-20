import asyncio
import random

from exit_vpn.failover import find_candidates
from exit_vpn.finder import Finder
from exit_vpn.state import Prefs

PREFS = Prefs(["United States", "Switzerland"])


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

    async def probe(self, srv, code, url, mode, js, forbid):
        self.calls.append((srv.name, code, tuple(forbid)))
        return {"server": srv.name, "outcome": self.outcomes.pop(0), "signals": []}


async def test_job_stops_after_two_ok(catalog):
    cands = catalog.filter("United States")[:6]
    client = FakeClient(["blocked", "ok", "challenge", "ok", "ok", "ok"])
    f = Finder(client, lambda c: "US", lambda: ["192.0.2.9"])
    job = f.start("https://x.example/", cands, "auto", False)
    for _ in range(50):
        if job.state != "running":
            break
        await asyncio.sleep(0.01)
    v = job.view()
    assert v["state"] == "done" and v["progress"]["done"] == 4 and len(v["ok"]) == 2
    assert client.calls[0][1:] == ("US", ("192.0.2.9",))
