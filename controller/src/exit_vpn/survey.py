"""A quality survey: many servers measured through the prober's tunnel (never the exit's),
each judged by the same quality model as the exit's own tunnels, so the thresholds that
tell Healthy, Degraded and Critical apart can be calibrated on more than the two servers
the exit happens to use. Every result goes to the survey log (role `survey`). The owner
starts a survey, or the schedule does; nothing is switched by it. Job view:
contracts/controller/survey_job.json.

The prober has one tunnel: surveys and find jobs share `prober_lock`. A find job holds it
for the whole job; a survey only per server, so a find job waits at most one measurement.

A `SurveySchedule` runs surveys by itself at set UTC hours (the owner sets it through the API;
it is kept in the state volume), so the servers' quality is sampled at busy and quiet hours
alike without anyone starting it. Results go to the survey log (`survey.jsonl`), which the
owner reads back for analysis (`/v1/survey/log`); selection never reads it (no server
history). Provider-neutral: the prober pings whatever DNS server the key's config names."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .errors import PolicyError, SwitchError
from .events import EventBus
from .persistence import QualityLog, SurveyScheduleStore
from .prober_client import ProberClient
from .quality import QualityMeter, Thresholds
from .servers import Server
from .service import ExitService

log = logging.getLogger("exit_vpn")
MAX_JOBS = 10
MAX_SERVERS = 100
MAX_TARGETS = 10
MAX_SECONDS = 300
GRACE = 30 * 60      # a scheduled run starts at most this late (e.g. after a restart)
STATES = ("healthy", "degraded", "critical", "measuring", "error")   # a result's `state`


@dataclass(frozen=True)
class SurveyTarget:
    """Where to survey: these countries and/or cities (none: the exit's country), at most
    `servers` of them."""
    countries: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    servers: int = 10


@dataclass(frozen=True)
class SurveySchedule:
    """Surveys run by themselves at `minute` past each of `hours` (UTC): every target, one
    after the other, `seconds` per server."""
    enabled: bool = False
    hours: tuple[int, ...] = ()
    minute: int = 10
    seconds: int = 60
    targets: tuple[SurveyTarget, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> SurveySchedule:
        return cls(bool(d.get("enabled", False)), tuple(sorted(set(d.get("hours") or ()))),
                   int(d.get("minute", 10)), int(d.get("seconds", 60)),
                   tuple(SurveyTarget(tuple(t.get("countries") or ()), tuple(t.get("cities") or ()),
                                      int(t.get("servers", 10)))
                         for t in d.get("targets") or ()))

    def view(self) -> dict:
        d = asdict(self)
        d["hours"] = list(self.hours)
        d["targets"] = [{"countries": list(t.countries), "cities": list(t.cities),
                         "servers": t.servers} for t in self.targets]
        return d

    def slot(self, now: float) -> float | None:
        """The start of the run due at `now` (within GRACE of its time), None if none is."""
        if not self.enabled or not self.targets:
            return None
        start = now - now % 3600 + self.minute * 60      # this hour's time (UTC: whole hours)
        if start > now:
            start -= 3600
        hour = datetime.fromtimestamp(start, timezone.utc).hour
        return start if hour in self.hours and now - start <= GRACE else None


@dataclass
class SurveyJob:
    id: str
    candidates: list[Server]
    seconds: int
    created: float
    by: str = ""                    # who started it ("schedule": the survey schedule)
    state: str = "running"          # running | done | cancelled | failed
    results: list[dict] = field(default_factory=list)
    error: str = ""
    task: asyncio.Task | None = None

    def view(self) -> dict:
        return {"job_id": self.id, "by": self.by, "state": self.state, "error": self.error,
                "seconds": self.seconds,
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
                 forbid_ips: Callable[[], list[str]], survey_log: QualityLog | None,
                 thresholds: Thresholds, schedule_store: SurveyScheduleStore | None = None,
                 clock: Callable[[], float] = time.time):
        self.service, self.client, self.events, self.lock = service, client, events, prober_lock
        self.code_for, self.forbid_ips, self.log, self.th = code_for, forbid_ips, survey_log, thresholds
        self.store, self.clock = schedule_store, clock
        saved, self.last_run = schedule_store.load() if schedule_store else ({}, 0.0)
        try:
            self.schedule = SurveySchedule.from_dict(saved)
        except (TypeError, ValueError, AttributeError) as e:
            log.warning("survey schedule unreadable, starting without one: %s", e)
            self.schedule = SurveySchedule()
        self.jobs: dict[str, SurveyJob] = {}
        self._ids = itertools.count(1)

    def start(self, countries: list[str], servers: int, seconds: int, who: str,
              cities: list[str] = ()) -> SurveyJob:
        return self.start_targets([SurveyTarget(tuple(countries), tuple(cities), servers)],
                                  seconds, who)

    def candidates(self, targets: list[SurveyTarget]) -> list[Server]:
        """Every target's servers, in order, each server once."""
        out: list[Server] = []
        seen: set[tuple[str, str]] = set()
        for tg in targets:
            if not 1 <= tg.servers <= MAX_SERVERS:
                raise PolicyError(f"servers must be 1..{MAX_SERVERS}")
            for s in self.service.survey_candidates(list(tg.countries), tg.servers,
                                                    list(tg.cities)):
                if (s.pubkey, s.ip) not in seen:
                    seen.add((s.pubkey, s.ip))
                    out.append(s)
        return out[:MAX_SERVERS]

    def _check_seconds(self, seconds: int) -> None:
        if not self.th.min_pings <= seconds <= MAX_SECONDS:
            raise PolicyError(f"seconds must be {self.th.min_pings}..{MAX_SECONDS} (a verdict "
                              f"needs {self.th.min_pings} pings)")

    def start_targets(self, targets: list[SurveyTarget], seconds: int, who: str) -> SurveyJob:
        self._check_seconds(seconds)
        cands = self.candidates(targets)
        if any(j.state == "running" for j in self.jobs.values()):
            raise SwitchError("a survey is already running")
        if not cands:
            raise SwitchError("no eligible servers to survey")
        job = SurveyJob(f"s{next(self._ids)}-{int(self.clock())}", cands, seconds, self.clock(),
                        by=who)
        self.jobs[job.id] = job
        for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-MAX_JOBS]:
            self.jobs.pop(old.id, None)
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        if who == "schedule":   # 16 runs a day would push everything else out of the event log
            log.info("scheduled survey %s: %d servers", job.id, len(cands))
        else:
            self.events.publish("survey_started", by=who, job=job.id, servers=len(cands),
                                seconds=seconds)
        return job

    # The schedule ---------------------------------------------------------------------------
    def set_schedule(self, schedule: SurveySchedule) -> SurveySchedule:
        """Checked as a run would be (unknown places fail now, not at 3 am), then kept."""
        if any(not 0 <= h <= 23 for h in schedule.hours) or not 0 <= schedule.minute <= 59:
            raise PolicyError("hours must be 0..23 and minute 0..59 (UTC)")
        if len(schedule.targets) > MAX_TARGETS:
            raise PolicyError(f"at most {MAX_TARGETS} targets")
        if schedule.enabled and not (schedule.hours and schedule.targets):
            raise PolicyError("an enabled schedule needs hours and targets")
        self._check_seconds(schedule.seconds)
        self.candidates(list(schedule.targets))
        self.schedule = schedule
        if self.store:
            self.store.save(schedule.view(), self.last_run)
        return schedule

    def schedule_view(self) -> dict:
        return {**self.schedule.view(), "last_run": self.last_run or None,
                "next_run": self.next_run(self.clock())}

    def next_run(self, now: float) -> float | None:
        s = self.schedule
        if not s.enabled or not s.hours or not s.targets:
            return None
        at = now - now % 3600 + s.minute * 60
        for _ in range(26):
            if at > now and datetime.fromtimestamp(at, timezone.utc).hour in s.hours:
                return at
            at += 3600
        return None

    def tick(self, now: float) -> SurveyJob | None:
        """Starts the scheduled run due now, once; a run still going skips this one."""
        slot = self.schedule.slot(now)
        if slot is None or self.last_run >= slot:
            return None
        self.last_run = slot
        if self.store:
            self.store.save(self.schedule.view(), slot)
        try:
            return self.start_targets(list(self.schedule.targets), self.schedule.seconds,
                                      "schedule")
        except (PolicyError, SwitchError) as e:
            log.warning("scheduled survey skipped: %s", e)
            return None

    async def run_schedule(self, interval: float = 30.0) -> None:
        while True:
            try:
                self.tick(self.clock())
            except Exception:
                log.exception("survey schedule")
            await asyncio.sleep(interval)

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
                                     "by": job.by, **result})
            job.state = "done"
            states = [r["state"] for r in job.results]
            counts = {s: states.count(s) for s in STATES}
            if job.by == "schedule":
                log.info("scheduled survey %s done: %s", job.id, counts)
            else:
                self.events.publish("survey_done", job=job.id, servers=len(states), **counts)
        except asyncio.CancelledError:
            job.state = "cancelled"
            raise
        except Exception as e:   # never leave a job 'running' forever
            log.exception("survey %s failed", job.id)
            job.state, job.error = "failed", str(e)

    async def _measure(self, srv: Server, seconds: int) -> dict:
        """One server's result; a failure is its own result (state `error`, empty summary)."""
        out = {"server": srv.name, "hostname": srv.hostname, "ip": srv.ip,
               "country": srv.country, "city": srv.city,
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
