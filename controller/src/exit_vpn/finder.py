"""'Find a server this URL accepts' (PLAN Phase 4): a job walks candidates through the
prober, which tests each through its own tunnel; the active exit is never touched. Applying
a result is an ordinary switch. Job view: contracts/controller/find_job.json."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import PolicyError, SwitchError
from .events import EventBus
from .prober_client import ProberClient
from .servers import Server
from .service import ExitService

log = logging.getLogger("exit_vpn")
OK_TO_STOP = 2   # stop after this many 'ok' results
MAX_JOBS = 20
MAX_CANDIDATES = 30


@dataclass
class FindJob:
    id: str
    url: str
    candidates: list[Server]
    created: float
    state: str = "running"          # running | done | failed
    results: list[dict] = field(default_factory=list)   # prober responses + where
    error: str = ""

    def candidate(self, name: str, ip: str | None) -> Server | None:
        """A probed candidate by name (and IP), or None if this job did not probe it."""
        probed = {(r["server"], r["ip"]) for r in self.results}
        return next((s for s in self.candidates if s.name == name and (ip is None or s.ip == ip)
                     and (s.name, s.ip) in probed), None)

    def view(self) -> dict:
        return {"job_id": self.id, "url": self.url, "state": self.state, "error": self.error,
                "progress": {"done": len(self.results), "total": len(self.candidates)},
                "ok": [r["server"] for r in self.results if r.get("outcome") == "ok"],
                "results": self.results}


class FindService:
    def __init__(self, service: ExitService, client: ProberClient, events: EventBus,
                 code_for: Callable[[str], str], forbid_ips: Callable[[], list[str]]):
        self.service, self.client, self.events = service, client, events
        self.code_for, self.forbid_ips = code_for, forbid_ips
        self.jobs: dict[str, FindJob] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()   # one job at a time: the prober has one tunnel

    def start(self, url: str, countries: list[str], max_candidates: int, mode: str, js: bool,
              who: str) -> FindJob:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise PolicyError("url must be http(s)://host/...")
        if not 1 <= max_candidates <= MAX_CANDIDATES:
            raise PolicyError(f"max_candidates must be 1..{MAX_CANDIDATES}")
        cands = self.service.find_candidates(countries, max_candidates)
        if not cands:
            raise SwitchError("no eligible candidates")
        job = FindJob(f"f{next(self._ids)}-{int(time.time())}", url, cands, time.time())
        self.jobs[job.id] = job
        for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-MAX_JOBS]:
            self.jobs.pop(old.id, None)
        asyncio.get_running_loop().create_task(self._run(job, mode, js))
        self.events.publish("find_started", by=who, url=url, job=job.id, candidates=len(cands))
        return job

    async def apply(self, job_id: str, name: str, ip: str | None, who: str) -> Server:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        srv = job.candidate(name, ip)
        if not srv:
            raise PolicyError("server was not probed in this job")
        return await self.service.switch_to(srv, f"find {job_id} apply by {who}")

    async def _run(self, job: FindJob, mode: str, js: bool) -> None:
        async with self._lock:
            try:
                for srv in job.candidates:
                    try:
                        r = await self.client.probe(srv, country_code=self.code_for(srv.country),
                                                    url=job.url, mode=mode, js=js,
                                                    forbid_ips=self.forbid_ips())
                    except Exception as e:   # one candidate's failure is its own result
                        r = {"server": srv.name, "url": job.url, "outcome": "error",
                             "signals": [f"prober: {type(e).__name__}"], "tiers": [],
                             "elapsed_ms": 0}
                    job.results.append({**r, "server": srv.name, "ip": srv.ip,
                                        "country": srv.country, "city": srv.city})
                    if sum(1 for x in job.results if x.get("outcome") == "ok") >= OK_TO_STOP:
                        break
                job.state = "done"
            except Exception as e:  # never leave a job 'running' forever
                log.exception("find job %s failed", job.id)
                job.state, job.error = "failed", str(e)
