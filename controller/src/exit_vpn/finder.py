"""'Find a server this URL accepts': jobs that walk candidates through the prober
(PLAN Phase 4). The active exit is never touched; `apply` is a normal select."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field

import httpx

from .catalog import Server

log = logging.getLogger("exit_vpn")
OK_TO_STOP = 2   # stop after this many 'ok' results
MAX_JOBS = 20


@dataclass
class FindJob:
    id: str
    url: str
    candidates: list[Server]
    created: float
    state: str = "running"          # running | done | failed
    results: list[dict] = field(default_factory=list)
    error: str = ""

    def view(self) -> dict:
        return {"job_id": self.id, "url": self.url, "state": self.state, "error": self.error,
                "progress": {"done": len(self.results), "total": len(self.candidates)},
                "ok": [r["server"] for r in self.results if r.get("outcome") == "ok"],
                "results": self.results}


class ProberClient:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    async def probe(self, srv: Server, code: str, url: str, mode: str, js: bool,
                    forbid_ips: list[str]) -> dict:
        body = {"server": {"name": srv.name, "ip": srv.ip, "pubkey": srv.pubkey,
                           "country_code": code},
                "url": url, "mode": mode, "js": js, "forbid_ips": forbid_ips}
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/probe", json=body)
            r.raise_for_status()
            return r.json()

    async def reload_key(self) -> dict:
        """The prober re-reads its key (slot b) and sets it on wg-probe."""
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/key/reload")
            r.raise_for_status()
            return r.json()


class Finder:
    def __init__(self, client: ProberClient, code_for: callable, forbid_ips: callable):
        self.client, self.code_for, self.forbid_ips = client, code_for, forbid_ips
        self.jobs: dict[str, FindJob] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()   # one job at a time: the prober has one tunnel

    def start(self, url: str, candidates: list[Server], mode: str, js: bool) -> FindJob:
        job = FindJob(f"f{next(self._ids)}-{int(time.time())}", url, candidates, time.time())
        self.jobs[job.id] = job
        for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-MAX_JOBS]:
            self.jobs.pop(old.id, None)
        asyncio.get_running_loop().create_task(self._run(job, mode, js))
        return job

    async def _run(self, job: FindJob, mode: str, js: bool) -> None:
        async with self._lock:
            try:
                for srv in job.candidates:
                    try:
                        r = await self.client.probe(srv, self.code_for(srv.country), job.url,
                                                    mode, js, self.forbid_ips())
                    except httpx.HTTPError as e:
                        r = {"server": srv.name, "outcome": "error",
                             "signals": [f"prober: {type(e).__name__}"]}
                    r.update(server=srv.name, ip=srv.ip, country=srv.country, city=srv.city)
                    job.results.append(r)
                    if sum(1 for x in job.results if x.get("outcome") == "ok") >= OK_TO_STOP:
                        break
                job.state = "done"
            except Exception as e:  # never leave a job 'running' forever
                log.exception("find job %s failed", job.id)
                job.state, job.error = "failed", str(e)
