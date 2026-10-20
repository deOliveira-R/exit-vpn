"""The controller: health loop, failover, rotation and selection (PLAN Phase 3).

All tunnel changes run under one lock, so the health loop, failover and API requests
never swap peers concurrently. While anything is wrong, the kill switch (routing +
nftables, set up before this process starts) keeps exit traffic off the underlay; this
code only decides *which* VPN server wg0 talks to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable

from .catalog import Catalog, Server
from .config import Settings
from .failover import choose_candidates
from .health import Backoff, Health, HealthTracker
from .probes import CycleResult, Prober
from .state import Current, State, StateStore
from .wg import WireGuard, WgError
from .wgconf import WgConf

log = logging.getLogger("exit_vpn")

MAX_SWITCH_TRIES = 5


class SwitchError(RuntimeError):
    pass


class Controller:
    def __init__(self, settings: Settings, conf: WgConf, wg: WireGuard, prober: Prober,
                 catalog: Catalog, store: StateStore,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.s, self.conf, self.wg, self.prober = settings, conf, wg, prober
        self.catalog, self.store, self.clock = catalog, store, clock
        self.rng = rng or random.Random()
        self.state: State = store.load()
        self.tracker = HealthTracker(settings.strikes, settings.warmup)
        self.backoff = Backoff()
        self.lock = asyncio.Lock()
        self.last: CycleResult | None = None
        self.last_ok_at = 0.0
        self._subscribers: set[asyncio.Queue] = set()

    # Events -----------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _event(self, kind: str, **data) -> None:
        ev = self.state.event(kind, self.clock(), **data)
        log.info("%s %s", kind, data)
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(ev)

    def _save(self) -> None:
        self.store.save(self.state)

    # Current server -----------------------------------------------------------------
    def current_server(self) -> Server | None:
        c = self.state.current
        if not c:
            return None
        return self.catalog.by_pubkey(c.pubkey) or Server(
            name=c.name, country=c.country, city=c.city, hostname="",
            ip=c.endpoint.rsplit(":", 1)[0], pubkey=c.pubkey)

    def _cur_name(self) -> str | None:
        return self.state.current.name if self.state.current else None

    async def init(self) -> None:
        """Adopt whatever peer init-wg configured (last-good state, or the config's)."""
        peers = await self.wg.peers()
        if not peers:
            raise WgError(f"{self.s.wg_if} has no peer")
        p = peers[0]
        now = self.clock()
        if not self.state.current or self.state.current.pubkey != p.pubkey:
            srv = self.catalog.by_pubkey(p.pubkey)
            self.state.current = Current(
                name=srv.name if srv else (self.conf.peer_name or "config peer"),
                pubkey=p.pubkey, endpoint=p.endpoint,
                country=srv.country if srv else "", city=srv.city if srv else "",
                since=now)
        self.tracker.start_grace(now)
        self._event("started", server=self._cur_name())
        self._save()

    # Health -------------------------------------------------------------------------
    async def check(self) -> CycleResult:
        try:
            peers = await self.wg.peers()
        except WgError as e:
            return CycleResult(False, False, False, None, errors=[str(e)])
        hs = peers[0].latest_handshake if peers else 0
        res = await self.prober.cycle(hs, self.clock())
        self.last = res
        if res.ok:
            self.last_ok_at = self.clock()
            c = self.state.current
            if c and not c.country and res.loc:
                c.country = res.loc  # unknown server: at least the exit country code
        return res

    async def health_step(self) -> None:
        """One cycle of the health loop (also used by tests with a fake clock)."""
        async with self.lock:
            before = self.tracker.state
            res = await self.check()
            now = self.clock()
            if self.tracker.state == Health.DEGRADED:
                if res.ok:
                    self.tracker.feed(True, now)
                    self.backoff.reset()
                    self._event("recovered", server=self._cur_name())
                return
            need = self.tracker.feed(res.ok, now)
            if self.tracker.state != before:
                self._event("health", state=str(self.tracker.state), errors=res.errors)
            if need and self.state.prefs.auto_failover:
                await self._failover("health checks failed: " + "; ".join(res.errors))
            elif need:
                self._event("unhealthy_no_failover", errors=res.errors)

    async def run(self) -> None:
        while True:
            try:
                if self.tracker.state == Health.DEGRADED:
                    await asyncio.sleep(self.backoff.step())
                    async with self.lock:
                        res = await self.check()
                        if res.ok:
                            self.tracker.feed(True, self.clock())
                            self.backoff.reset()
                            self._event("recovered", server=self._cur_name())
                        elif self.state.prefs.auto_failover:
                            await self._failover("retry from degraded")
                    continue
                await self.health_step()
            except Exception:  # never let the loop die; the kill switch holds meanwhile
                log.exception("health loop error")
            await asyncio.sleep(self.s.health_interval)

    # Switching ----------------------------------------------------------------------
    async def _try(self, srv: Server) -> bool:
        """Swap to `srv` and wait up to the warm-up for one fully passing cycle."""
        endpoint = srv.endpoint(self.s.wg_port)
        try:
            await self.wg.swap_peer(srv.pubkey, endpoint)
        except WgError as e:
            self._event("swap_error", server=srv.name, error=str(e))
            return False
        now = self.clock()
        self.tracker.start_grace(now)
        deadline = now + self.s.warmup
        while True:
            res = await self.check()
            if res.ok:
                self.state.current = Current(srv.name, srv.pubkey, endpoint,
                                             srv.country, srv.city, self.clock())
                self.tracker.feed(True, self.clock())
                self._save()
                return True
            if self.clock() >= deadline:
                return False
            await asyncio.sleep(1.0)

    def _blacklist(self, pubkey: str) -> None:
        self.state.blacklist_add(pubkey, self.clock() + self.s.blacklist_ttl)

    async def _failover(self, reason: str) -> bool:
        now = self.clock()
        self.state.prune(now)
        self.tracker.failing_over(now)
        old = self.current_server()
        if old:
            self._blacklist(old.pubkey)
        self._event("failover_start", server=old.name if old else None, reason=reason)
        cands = choose_candidates(self.catalog.servers, old, self.state.prefs,
                                  set(self.state.blacklist), self.rng, "failover")
        for srv in cands:
            if await self._try(srv):
                self.backoff.reset()
                self._event("failover_done", old=old.name if old else None, new=srv.name,
                            exit_ip=self.last.exit_ip if self.last else None,
                            seconds=round(self.clock() - now, 1))
                return True
            self._blacklist(srv.pubkey)
            self._event("candidate_failed", server=srv.name)
        self.tracker.degraded(self.clock())
        self._event("degraded", tried=len(cands))
        self._save()
        return False

    async def switch(self, candidates: list[Server], reason: str) -> Server:
        """Rotate/select: try candidates in order; on total failure, go back."""
        if not candidates:
            raise SwitchError("no eligible server matches")
        async with self.lock:
            old = self.current_server()
            t0 = self.clock()
            for srv in candidates[:MAX_SWITCH_TRIES]:
                if await self._try(srv):
                    self._event("switched", old=old.name if old else None, new=srv.name,
                                reason=reason, exit_ip=self.last.exit_ip if self.last else None,
                                seconds=round(self.clock() - t0, 1))
                    return srv
                self._blacklist(srv.pubkey)
                self._event("candidate_failed", server=srv.name)
            if old and await self._try(old):
                self._event("switch_reverted", server=old.name, reason=reason)
            self._save()
            raise SwitchError(f"none of {min(len(candidates), MAX_SWITCH_TRIES)} candidates worked")

    def candidates(self, scope: str) -> list[Server]:
        self.state.prune(self.clock())
        return choose_candidates(self.catalog.servers, self.current_server(), self.state.prefs,
                                 set(self.state.blacklist), self.rng, scope)

    def select_candidates(self, server: str | None, country: str | None,
                          city: str | None) -> list[Server]:
        from .failover import eligible
        if server:
            return self.catalog.by_name(server)  # explicit choice: no eligibility filter
        cur = self.current_server()
        pool = [s for s in self.catalog.filter(country, city)
                if eligible(s, self.state.prefs) and (not cur or s.pubkey != cur.pubkey)
                and not self.state.blacklisted(s.pubkey, self.clock())]
        self.rng.shuffle(pool)
        return pool

    # Views --------------------------------------------------------------------------
    def status(self) -> dict:
        c, r, now = self.state.current, self.last, self.clock()
        return {
            "server": c.name if c else None,
            "country": c.country if c else None,
            "city": c.city if c else None,
            "endpoint": c.endpoint if c else None,
            "since": c.since if c else None,
            "health": str(self.tracker.state),
            "health_since": self.tracker.since,
            "exit_ip": r.exit_ip if r else None,
            "exit_loc": r.loc if r else None,
            "handshake_age": round(r.handshake_age, 1) if r and r.handshake_age is not None else None,
            "last_ok_age": round(now - self.last_ok_at, 1) if self.last_ok_at else None,
            "errors": r.errors if r else [],
            "prefs": vars(self.state.prefs),
            "provider": self.s.provider,
            "key_options": self.conf.options,
            "catalog": {"servers": len(self.catalog), "source": self.catalog.source,
                        "fetched_at": self.catalog.fetched_at},
            "blacklisted": len([1 for v in self.state.blacklist.values() if v > now]),
            "events": self.state.events[-10:],
        }
