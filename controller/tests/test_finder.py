"""Find jobs: probing candidates for a URL through the prober (component)."""

import pytest

from exit_vpn.errors import PolicyError
from exit_vpn.finder import FindService

from harness import ScriptedProber, job_done


async def test_a_job_stops_after_two_servers_pass(rig):
    await rig.svc.start()
    client = ScriptedProber(["blocked", "ok", "challenge", "ok", "ok", "ok"])
    f = FindService(rig.svc, client, rig.node.events, lambda c: "US", lambda: ["192.0.2.9"])
    job = f.start("https://x.example/", ["United States"], 6, "auto", False, "o@github")
    await job_done(job)
    v = job.view()
    assert v["state"] == "done" and v["progress"]["done"] == 4 and len(v["ok"]) == 2
    assert client.calls[0][1:] == ("US", ("192.0.2.9",))
    assert "find_started" in rig.kinds()


async def test_an_unusable_prober_answer_fails_that_candidate_not_the_job(rig):
    """A prober answer the controller cannot use is that candidate's error, not the job's."""
    await rig.svc.start()

    class Broken(ScriptedProber):
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


async def test_a_job_needs_an_http_url_and_known_countries(rig):
    await rig.svc.start()
    f = FindService(rig.svc, ScriptedProber([]), rig.node.events, lambda c: "US", lambda: [])
    for url in ("ftp://x.example/", "https:///nohost"):
        with pytest.raises(PolicyError):
            f.start(url, [], 3, "auto", False, "o@github")
    with pytest.raises(PolicyError, match="unknown countries"):
        f.start("https://x.example/", ["Atlantis"], 3, "auto", False, "o@github")
