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
from .keys import KeyFile
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
        # The private key wg0 runs with (slot a); set by __main__ from the key store
        self.conf_key: Callable[[], str] = lambda: ""
        self.warnings: Callable[[], list[str]] = lambda: []

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
        ip = c.endpoint.rsplit(":", 1)[0]
        return self.catalog.by_peer(c.pubkey, ip) or Server(
            name=c.name, country=c.country, city=c.city, hostname="", ip=ip, pubkey=c.pubkey)

    def _cur_name(self) -> str | None:
        return self.state.current.name if self.state.current else None

    async def init(self) -> None:
        """Adopt whatever peer init-wg configured (last-good state, or the config's).
        Identity, in order: the catalog entry with this (pubkey, IP); the config's own
        peer name; the saved state (a server since dropped from the catalog)."""
        peers = await self.wg.peers()
        if not peers:
            raise WgError(f"{self.s.wg_if} has no peer")
        p = peers[0]
        now = self.clock()
        cur = self.state.current
        same = cur is not None and (cur.pubkey, cur.endpoint) == (p.pubkey, p.endpoint)
        srv = self.catalog.by_peer(p.pubkey, p.endpoint.rsplit(":", 1)[0])
        since = cur.since if same and cur else now
        profile = list(cur.profile) if same and cur else []
        if srv:
            self.state.current = Current(srv.name, p.pubkey, p.endpoint, srv.country, srv.city,
                                         since, profile)
        elif (p.pubkey, p.endpoint) == (self.conf.peer_pubkey, self.conf.peer_endpoint):
            self.state.current = Current(self.conf.peer_name or "config peer", p.pubkey,
                                         p.endpoint, since=since)
        elif not same:
            self.state.current = Current("unknown peer", p.pubkey, p.endpoint, since=now)
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
                # a server not in the catalog: at least its country, from the exit
                c.country = self.catalog.country_for_code(res.loc) or res.loc
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
            started = self.clock()
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
            # Fixed cadence: a failing cycle already spent its probe timeouts, so waiting a
            # full interval on top would stretch 3 strikes from ~15 s to ~24 s.
            await asyncio.sleep(max(0.5, self.s.health_interval - (self.clock() - started)))

    # Switching ----------------------------------------------------------------------
    async def _healthy_within(self, seconds: float) -> bool:
        """After a tunnel change: wait up to `seconds` for one fully passing cycle."""
        now = self.clock()
        self.tracker.start_grace(now)
        deadline = now + seconds
        while True:
            res = await self.check()
            if res.ok:
                self.tracker.feed(True, self.clock())
                return True
            if self.clock() >= deadline:
                return False
            await asyncio.sleep(1.0)

    def _profile(self) -> list[str]:
        return list(self.state.current.profile) if self.state.current else []

    async def _try(self, srv: Server, profile: list[str] | None = None) -> bool:
        """Swap to `srv` and wait up to the warm-up for one fully passing cycle.
        `profile`: features to keep from now on (None: the current server's)."""
        profile = self._profile() if profile is None else profile
        endpoint = srv.endpoint(self.s.wg_port)
        try:
            await self.wg.swap_peer(srv.pubkey, endpoint)
        except WgError as e:
            self._event("swap_error", server=srv.name, error=str(e))
            return False
        if not await self._healthy_within(self.s.warmup):
            return False
        self.state.current = Current(srv.name, srv.pubkey, endpoint,
                                     srv.country, srv.city, self.clock(), list(profile))
        self._save()
        return True

    async def replace_key(self, key: KeyFile, commit: Callable[[], None], reason: str) -> bool:
        """Swap wg0's private key (same peer); keep it only if traffic passes within the
        key warm-up, else put the old key back. `commit` persists the new key."""
        async with self.lock:
            old = self.conf_key()
            t0 = self.clock()
            await self.wg.set_private_key(key.private_key)
            if await self._healthy_within(self.s.key_warmup):
                commit()
                self.conf = key.conf
                self._event("key_replaced", slot="a", reason=reason,
                            seconds=round(self.clock() - t0, 1))
                return True
            await self.wg.set_private_key(old)
            back = await self._healthy_within(self.s.warmup)
            self._event("key_rejected", slot="a", reason=reason, restored_healthy=back)
            return False

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
                                  set(self.state.blacklist), self.rng, "failover",
                                  profile=self._profile())
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

    async def switch(self, candidates: list[Server], reason: str,
                     profile: list[str] | None = None) -> Server:
        """Rotate/select: try candidates in order; on total failure, go back.
        `profile`: what failover keeps afterwards (None: unchanged)."""
        if not candidates:
            raise SwitchError("no eligible server matches")
        async with self.lock:
            old = self.current_server()
            old_profile = self._profile()
            t0 = self.clock()
            for srv in candidates[:MAX_SWITCH_TRIES]:
                if await self._try(srv, profile):
                    self._event("switched", old=old.name if old else None, new=srv.name,
                                reason=reason, exit_ip=self.last.exit_ip if self.last else None,
                                seconds=round(self.clock() - t0, 1))
                    return srv
                self._blacklist(srv.pubkey)
                self._event("candidate_failed", server=srv.name)
            if old and await self._try(old, old_profile):
                self._event("switch_reverted", server=old.name, reason=reason)
            self._save()
            raise SwitchError(f"none of {min(len(candidates), MAX_SWITCH_TRIES)} candidates worked")

    async def replace_catalog(self, catalog: Catalog) -> None:
        async with self.lock:
            old = len(self.catalog)
            self.catalog = catalog
            catalog.save(self.s.catalog_file)
        self._event("catalog", source=catalog.source, servers=len(catalog), before=old)

    def candidates(self, scope: str, profile: list[str] | None = None) -> list[Server]:
        """Rotation keeps the profile of a manually picked server (P2P stays P2P)."""
        self.state.prune(self.clock())
        return choose_candidates(self.catalog.servers, self.current_server(), self.state.prefs,
                                 set(self.state.blacklist), self.rng, scope,
                                 profile=self._profile() if profile is None else profile)

    def exact_server(self, server: str, ip: str | None = None) -> list[Server]:
        """One physical server by name (or hostname) and, when given, entry IP. An
        explicit choice skips the load/blacklist rules, but not the Secure Core mode
        (failover could never keep it) and not maintenance."""
        found = [s for s in self.catalog.by_name(server) if ip is None or s.ip == ip]
        if not found:
            raise ValueError(f"no server {server!r}" + (f" at {ip}" if ip else ""))
        if any(s.secure_core != self.state.prefs.secure_core for s in found):
            raise ValueError("Secure Core is " + ("on: pick a Secure Core server"
                             if self.state.prefs.secure_core else "off: turn it on first"))
        usable = [s for s in found if s.enabled]
        if not usable:
            raise ValueError(f"{server} is in maintenance")
        return usable

    async def set_secure_core(self, on: bool, who: str) -> Server | None:
        """Secure Core is a mode, so turning it on or off moves to a server of the new
        mode now: the current country first, then the pinned regions. If none works, the
        mode stays as it was."""
        p = self.state.prefs
        if p.secure_core == on:
            return None
        p.secure_core = on
        # a new mode starts without a profile: Secure Core servers carry no P2P flag
        cands = self.candidates("country", []) + self.candidates("any", [])
        uniq = list({(s.pubkey, s.ip): s for s in cands}.values())
        if not uniq:
            p.secure_core = not on
            raise SwitchError("no Secure Core server in the current country or pinned regions"
                              if on else "no regular server in the current country or pinned regions")
        try:
            srv = await self.switch(uniq, f"secure core {'on' if on else 'off'} by {who}", [])
        except SwitchError:
            p.secure_core = not on
            self._save()
            raise
        self._event("prefs", by=who, prefs=p.view())
        self._save()
        return srv

    def select_candidates(self, server: str | None, country: str | None,
                          city: str | None) -> list[Server]:
        from .failover import eligible
        if server:
            return self.exact_server(server)
        cur = self.current_server()
        pool = [s for s in self.catalog.filter(country, city)
                if eligible(s, self.state.prefs) and (not cur or s.pubkey != cur.pubkey)
                and not self.state.blacklisted(s.pubkey, self.clock())]
        self.rng.shuffle(pool)
        return pool

    def _catalog_warnings(self, now: float) -> list[str]:
        cat = self.catalog
        if cat.pushed and now - cat.fetched_at > self.s.pushed_catalog_max_age:
            return [f"pushed catalog is {(now - cat.fetched_at) / 86400:.0f} days old "
                    "(exitctl <provider> servers --push)"]
        return []

    # Views --------------------------------------------------------------------------
    def status(self) -> dict:
        c, r, now = self.state.current, self.last, self.clock()
        cur = self.current_server()
        return {
            "server": c.name if c else None,
            "country": c.country if c else None,
            "city": c.city if c else None,
            "endpoint": c.endpoint if c else None,
            "pubkey": c.pubkey if c else None,
            "since": c.since if c else None,
            "profile": c.profile if c else [],
            "entry": cur.entry if cur else "",
            "features": cur.features() if cur else [],
            "load": cur.load if cur else None,
            "health": str(self.tracker.state),
            "health_since": self.tracker.since,
            "exit_ip": r.exit_ip if r else None,
            "exit_loc": r.loc if r else None,
            "handshake_age": round(r.handshake_age, 1) if r and r.handshake_age is not None else None,
            "last_ok_age": round(now - self.last_ok_at, 1) if self.last_ok_at else None,
            "errors": r.errors if r else [],
            "warnings": self.warnings() + self._catalog_warnings(now),
            "prefs": self.state.prefs.view(),
            "provider": self.s.provider,
            "key_options": self.conf.options,
            "catalog": {"servers": len(self.catalog), "source": self.catalog.source,
                        "pushed": self.catalog.pushed, "fetched_at": self.catalog.fetched_at,
                        "enabled": sum(1 for s in self.catalog.servers if s.enabled)},
            "blacklisted": len([1 for v in self.state.blacklist.values() if v > now]),
            "events": self.state.events[-10:],
        }
