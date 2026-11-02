"""A quality survey: servers measured through the prober, judged by the exit's quality model,
logged for calibration; one at a time, sharing the prober with find jobs (component: the real
node with a fake prober client)."""

import json
from datetime import datetime, timezone

import pytest

from exit_vpn.errors import PolicyError, SwitchError
from exit_vpn.quality import Thresholds
from exit_vpn.persistence import QualityLog
from exit_vpn.survey import SurveySchedule, SurveyTarget, judge

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
    lines, more = rig.node.survey.log.read()
    assert [x["role"] for x in lines] == ["survey"] * 4 and not more
    assert lines[0]["job"] == job.id and lines[0]["by"] == "o@github"
    assert not rig.svc.quality_log.path.exists(), "survey results have their own log"
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


async def test_a_survey_can_cover_whole_cities(rig):
    await rig.svc.start()
    job = rig.node.survey.start([], 50, 60, "o", cities=["Los Angeles", "zurich"])
    assert {c.city for c in job.candidates} == {"Los Angeles", "Zurich"}
    assert len(job.candidates) >= 10, "every eligible server there, not one per city"
    with pytest.raises(PolicyError):
        rig.node.survey.start([], 5, 60, "o", cities=["Atlantis"])
    await job_done(job)


def at(hour: int, minute: int = 0, day: int = 2) -> float:
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_a_schedule_is_due_at_its_minute_past_its_hours_and_not_long_after():
    s = SurveySchedule(True, (3, 21), 10, 60, (SurveyTarget(cities=("Los Angeles",)),))
    assert s.slot(at(3, 9)) is None
    assert s.slot(at(3, 10)) == at(3, 10) and s.slot(at(3, 39)) == at(3, 10)
    assert s.slot(at(3, 41)) is None, "too late (e.g. the node was down): wait for the next"
    assert s.slot(at(4, 10)) is None
    assert s.slot(at(22, 5)) is None and s.slot(at(21, 30)) == at(21, 10)
    assert SurveySchedule(False, (3,), 10, 60, s.targets).slot(at(3, 15)) is None


async def test_the_schedule_starts_each_run_once_and_survives_a_restart(rig, catalog):
    await rig.svc.start()
    sv = rig.node.survey
    sv.set_schedule(SurveySchedule(True, (3,), 10, 60,
                                   (SurveyTarget(cities=("Los Angeles",), servers=2),
                                    SurveyTarget(("Switzerland",), servers=2))))
    rig.clock.t = at(3, 12)
    job = sv.tick(rig.clock())
    assert job and job.by == "schedule" and len(job.candidates) == 4
    assert {c.country for c in job.candidates} == {"United States", "Switzerland"}
    assert sv.tick(rig.clock() + 60) is None, "one run per slot"
    await job_done(job)
    assert sv.schedule_view()["next_run"] == at(3, 10, day=3)
    assert not [e for e in rig.node.events.recent() if e["kind"].startswith("survey")], \
        "scheduled runs stay out of the event log (their results are in the survey log)"

    again = rig.rebuild(catalog)
    assert again.node.survey.schedule == sv.schedule and again.node.survey.last_run == at(3, 10)
    assert again.node.survey.tick(at(3, 20)) is None


async def test_a_run_due_while_a_survey_is_going_is_skipped(rig):
    await rig.svc.start()
    sv = rig.node.survey
    sv.set_schedule(SurveySchedule(True, (3,), 10, 60, (SurveyTarget(servers=2),)))
    manual = sv.start([], 2, 60, "o")
    assert sv.tick(at(3, 11)) is None and sv.last_run == at(3, 10)
    await job_done(manual)


async def test_a_bad_schedule_is_refused_when_set(rig):
    await rig.svc.start()
    sv = rig.node.survey
    lax = SurveyTarget(cities=("Los Angeles",))
    for bad in (SurveySchedule(True, (24,), 10, 60, (lax,)),
                SurveySchedule(True, (3,), 60, 60, (lax,)),
                SurveySchedule(True, (3,), 10, 5, (lax,)),
                SurveySchedule(True, (), 10, 60, (lax,)),
                SurveySchedule(True, (3,), 10, 60, (SurveyTarget(cities=("Atlantis",)),))):
        with pytest.raises(PolicyError):
            sv.set_schedule(bad)
    assert sv.schedule == SurveySchedule()


async def test_the_schedule_and_logs_over_http(rig):
    await rig.svc.start()
    c = client(rig)
    assert c.get("/v1/survey/schedule").json()["enabled"] is False
    body = {"enabled": True, "hours": [3, 0], "minute": 10, "seconds": 60,
            "targets": [{"cities": ["Los Angeles"], "servers": 3}]}
    r = c.put("/v1/survey/schedule", json=body)
    assert r.status_code == 200 and r.json()["hours"] == [0, 3] and r.json()["next_run"]
    assert c.put("/v1/survey/schedule", json={**body, "hours": [25]}).status_code == 400
    assert c.get("/v1/survey/log").json() == {"records": [], "more": False}
    assert c.get("/v1/quality/log").status_code == 200


def test_a_log_is_read_back_across_its_rotation_and_in_pages(tmp_path):
    qlog = QualityLog(tmp_path / "survey.jsonl", max_bytes=200)
    for ts in range(1, 11):
        qlog.append({"ts": ts, "pad": "x" * 40})
    assert (tmp_path / "survey.jsonl.1").exists()
    page, more = qlog.read(0, 3)
    assert len(page) == 3 and more and page[0]["ts"] > 1, "the oldest rotated out"
    rest, more = qlog.read(page[-1]["ts"], 100)
    assert rest[0]["ts"] == page[-1]["ts"] + 1 and rest[-1]["ts"] == 10 and not more
