"""A quality survey: many servers measured through the prober's tunnel (never the exit's),
each judged by the same quality model as the exit's own tunnels, so the thresholds that
tell Healthy, Degraded and Critical apart can be calibrated on more than the two servers
the exit happens to use. Every result goes to the quality log (role `survey`). The owner
starts a survey; nothing is switched by it. Job view: contracts/controller/survey_job.json.

The prober has one tunnel: surveys and find jobs share `prober_lock`. A find job holds it
for the whole job; a survey only per server, so a find job waits at most one measurement."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import PolicyError, SwitchError
from .events import EventBus
from .persistence import QualityLog
from .prober_client import ProberClient
from .quality import QualityMeter, Thresholds
from .servers import Server
from .service import ExitService

log = logging.getLogger("exit_vpn")
MAX_JOBS = 10
MAX_SERVERS = 100
STATES = ("healthy", "degraded", "critical", "measuring", "error")   # a result's `state`


@dataclass
class SurveyJob:
    id: str
    candidates: list[Server]
    seconds: int
    created: float
    state: str = "running"          # running | done | cancelled | failed
    results: list[dict] = field(default_factory=list)
    error: str = ""
    task: asyncio.Task | None = None

    def view(self) -> dict:
        return {"job_id": self.id, "state": self.state, "error": self.error, "seconds": self.seconds,
                "progress": {"done": len(self.results), "total": len(self.candidates)},
                "results": self.results}


def judge(m: dict, th: Thresholds) -> dict:
    """A prober measurement (contracts/prober/measure_response.json) → the quality model's
    verdict and summary, as if the samples had been taken on an exit tunnel."""
    meter = QualityMeter(th)
    step = float(m.get("interval") or 1.0)
    every = int(m.get("https_every") or 5)
    pings, https = m.get("pings") or [], m.get("https") or []
    for i, rtt in enumerate(pings):
        meter.ping(i * step, rtt)
    for j, ms in enumerate(https):
        meter.cycle(j * every * step, ms, None)
    q, reasons, summary = meter.assess(max(0.0, (len(pings) - 1) * step))
    return {"state": str(q), "reasons": reasons, **summary.view()}


class SurveyService:
    def __init__(self, service: ExitService, client: ProberClient, events: EventBus,
                 prober_lock: asyncio.Lock, code_for: Callable[[str], str],
                 forbid_ips: Callable[[], list[str]], quality_log: QualityLog | None,
                 thresholds: Thresholds, clock: Callable[[], float] = time.time):
        self.service, self.client, self.events, self.lock = service, client, events, prober_lock
        self.code_for, self.forbid_ips, self.log, self.th = code_for, forbid_ips, quality_log, thresholds
        self.clock = clock
        self.jobs: dict[str, SurveyJob] = {}
        self._ids = itertools.count(1)

    def start(self, countries: list[str], servers: int, seconds: int, who: str) -> SurveyJob:
        if not 1 <= servers <= MAX_SERVERS:
            raise PolicyError(f"servers must be 1..{MAX_SERVERS}")
        if not self.th.min_pings <= seconds <= 300:
            raise PolicyError(f"seconds must be {self.th.min_pings}..300 (a verdict needs "
                              f"{self.th.min_pings} pings)")
        if any(j.state == "running" for j in self.jobs.values()):
            raise SwitchError("a survey is already running")
        cands = self.service.survey_candidates(countries, servers)
        if not cands:
            raise SwitchError("no eligible servers to survey")
        job = SurveyJob(f"s{next(self._ids)}-{int(self.clock())}", cands, seconds, self.clock())
        self.jobs[job.id] = job
        for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-MAX_JOBS]:
            self.jobs.pop(old.id, None)
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        self.events.publish("survey_started", by=who, job=job.id, servers=len(cands),
                            seconds=seconds)
        return job

    def cancel(self, job_id: str) -> SurveyJob:
        job = self.jobs[job_id]
        if job.state == "running" and job.task:
            job.task.cancel()
            job.state = "cancelled"
        return job

    async def _run(self, job: SurveyJob) -> None:
        try:
            for srv in job.candidates:
                async with self.lock:
                    result = await self._measure(srv, job.seconds)
                job.results.append(result)
                if self.log:
                    self.log.append({"ts": round(self.clock()), "role": "survey", "job": job.id,
                                     **result})
            job.state = "done"
            states = [r["state"] for r in job.results]
            self.events.publish("survey_done", job=job.id, servers=len(states),
                                **{s: states.count(s) for s in STATES})
        except asyncio.CancelledError:
            job.state = "cancelled"
            raise
        except Exception as e:   # never leave a job 'running' forever
            log.exception("survey %s failed", job.id)
            job.state, job.error = "failed", str(e)

    async def _measure(self, srv: Server, seconds: int) -> dict:
        """One server's result; a failure is its own result (state `error`, empty summary)."""
        out = {"server": srv.name, "ip": srv.ip, "country": srv.country, "city": srv.city,
               "load": srv.load, "features": srv.features(), "exit_ip": None, "error": ""}
        try:
            m = await self.client.measure(srv, country_code=self.code_for(srv.country),
                                          seconds=seconds, forbid_ips=self.forbid_ips())
            out["exit_ip"], out["error"] = m.get("exit_ip"), m.get("error") or ""
        except Exception as e:
            m, out["error"] = {}, f"prober: {type(e).__name__}: {e}"[:200]
        verdict = judge({} if out["error"] else m, self.th)
        if out["error"]:
            verdict["state"] = "error"
        return {**out, **verdict}
