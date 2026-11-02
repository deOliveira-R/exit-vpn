"""A quality survey: servers measured through the prober, judged by the exit's quality model,
logged for calibration; one at a time, sharing the prober with find jobs (component: the real
node with a fake prober client)."""

import json

import pytest

from exit_vpn.errors import PolicyError, SwitchError
from exit_vpn.quality import Thresholds
from exit_vpn.survey import judge

from harness import client, job_done, make


@pytest.fixture
def rig(catalog, tmp_path, monkeypatch):
    return make(catalog, tmp_path, monkeypatch)


def test_a_measurement_is_judged_as_the_exits_tunnels_are():
    steady = {"interval": 1.0, "https_every": 5, "pings": [20.0] * 60, "https": [150.0] * 12}
    assert judge(steady, Thresholds())["state"] == "healthy"
    lossy = {**steady, "pings": ([20.0] * 19 + [None]) * 3}
    v = judge(lossy, Thresholds())
    assert v["state"] == "degraded" and v["reasons"] == ["5.0% loss"] and v["https_p50"] == 150.0
    assert judge({**steady, "pings": [20.0] * 20}, Thresholds())["state"] == "measuring"


async def test_a_survey_measures_each_server_once_and_logs_every_result(rig):
    await rig.svc.start()
    rig.prober.measures = {}
    job = rig.node.survey.start(["United States"], 4, 60, "o@github")
    target = job.candidates[1].name
    rig.prober.measures[target] = {"pings": [20.0, None] * 30}
    await job_done(job)
    assert job.state == "done" and len(job.results) == 4
    assert [m[0] for m in rig.prober.measured] == [c.name for c in job.candidates]
    assert rig.svc.state.active.pubkey not in {c.pubkey for c in job.candidates}, "never the exit's server"
    bad = next(r for r in job.results if r["server"] == target)
    assert bad["state"] == "critical"
    lines = [json.loads(x) for x in rig.svc.quality_log.path.read_text().splitlines()]
    assert [x["role"] for x in lines] == ["survey"] * 4 and lines[0]["job"] == job.id
    done = [e for e in rig.node.events.recent() if e["kind"] == "survey_done"][-1]
    assert done["servers"] == 4 and done["critical"] == 1 and done["healthy"] == 3
    assert not rig.wg.swaps, "a survey never touches the exit"


async def test_a_servers_failure_is_its_own_result(rig):
    await rig.svc.start()
    job = rig.node.survey.start(["United States"], 2, 60, "o@github")
    rig.prober.measures[job.candidates[0].name] = {"error": "no traffic through the tunnel within 20 s"}
    rig.prober.measures[job.candidates[1].name] = RuntimeError("prober down")
    await job_done(job)
    assert job.state == "done"
    assert [r["state"] for r in job.results] == ["error", "error"]
    assert "prober down" in job.results[1]["error"]


async def test_survey_limits_and_one_at_a_time(rig):
    await rig.svc.start()
    sv = rig.node.survey
    with pytest.raises(PolicyError):
        sv.start([], 10, 10, "o")        # under the pings a verdict needs
    with pytest.raises(PolicyError):
        sv.start(["Atlantis"], 10, 60, "o")
    job = sv.start([], 2, 60, "o")
    with pytest.raises(SwitchError):
        sv.start([], 2, 60, "o")
    await job_done(job)


async def test_a_survey_over_http_can_be_followed_and_cancelled(rig):
    await rig.svc.start()
    c = client(rig)
    r = c.post("/v1/survey", json={"servers": 3, "seconds": 60})
    assert r.status_code == 202 and r.json()["progress"]["total"] == 3
    job_id = r.json()["job_id"]
    assert c.delete(f"/v1/survey/{job_id}").json()["state"] in ("cancelled", "done")
    assert c.get(f"/v1/survey/{job_id}").status_code == 200
    assert c.get("/v1/survey/nope").status_code == 404
