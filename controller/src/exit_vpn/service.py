"""The exit service: which server the exit uses, and every change to it.

`ExitService` owns the active server, the preferences, the blacklist and the catalog in
use, and runs every change under one lock, so the health loop, failover and API requests
never swap peers concurrently. Changes to wg0 go through `Tunnel`; candidate lists come from
`selection`; events go to the `EventBus`; state is saved through `StateStore`.

Health cycle (`step`, every `health_interval` seconds):
- Healthy or suspect: a failed cycle is a strike; `strikes` in a row (outside the warm-up)
  start failover, unless automatic failover is off.
- Failover: blacklist the server, try candidates (same city, country, pinned regions)
  until one carries traffic; none → degraded.
- Degraded: retry with backoff (30 s doubling to 5 min): recover if the server works again,
  else fail over again.
- IPv6: the tunnel's IPv6 gate opens and closes with its own probe (`Tunnel`); an `ipv6`
  event reports each change as seen after a cycle or a switch. It never starts a failover.
While anything is wrong, the kill switch (routing + nftables, set up before this process
starts) keeps exit traffic off the underlay; this code only decides which server wg0 uses.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

from . import selection
from .catalog import Catalog
from .config import Settings
from .errors import PolicyError, SwitchError
from .events import EventBus
from .health import Backoff, Health
from .persistence import CatalogCache, StateStore
from .prefs import Prefs
from .servers import Region, Server, common_features
from .state import ActiveServer, State
from .tunnel import Tunnel
from .wg import WgError
from .wgconf import WgConf

log = logging.getLogger("exit_vpn")

MAX_SWITCH_TRIES = 5
# The controller API's version (contracts/controller/): raised when a change would break the
# app; additions that clients may ignore keep it.
API_VERSION = 1


class ExitService:
    def __init__(self, settings: Settings, tunnel: Tunnel, catalog: Catalog,
                 store: StateStore, events: EventBus, exit_conf: WgConf, *,
                 catalog_cache: CatalogCache | None = None,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.s, self.tunnel, self.catalog = settings, tunnel, catalog
        self.store, self.events, self.cache = store, events, catalog_cache
        self.exit_conf = exit_conf   # slot a's config: Address/DNS and the key's options
        self.clock, self.rng = clock, rng or random.Random()
        self.state: State = store.load()[0]
        self.backoff = Backoff()
        self.lock = asyncio.Lock()
        # status warnings from other parts (key expiry): callables of `now`
        self.warning_sources: list[Callable[[float], list[str]]] = []
        self._ipv6_reported = False   # the gate as the last `ipv6` event told it

    @property
    def health(self) -> Health:
        return self.tunnel.tracker.state

    @property
    def prefs(self) -> Prefs:
        return self.state.prefs

    # The active server --------------------------------------------------------------
    def current_server(self) -> Server | None:
        """The catalog entry for wg0's peer; a peer the catalog does not know (a config's
        own peer, a server since removed) is described from what was saved."""
        a = self.state.active
        if not a:
            return None
        return self.catalog.by_id(a.pubkey, a.ip) or Server(
            name=a.name, country=a.country, city=a.city, hostname="", ip=a.ip, pubkey=a.pubkey)

    def _name(self) -> str | None:
        return self.state.active.name if self.state.active else None

    def _profile(self) -> list[str]:
        return list(self.state.active.profile) if self.state.active else []

    def _save(self) -> None:
        self.store.save(self.state)

    async def start(self) -> None:
        """Adopt whatever peer init-wg configured (last-good state, or the config's).
        Identity, in order: the catalog entry with this (pubkey, IP); the config's own
        peer name; the saved state (a server since dropped from the catalog)."""
        p = await self.tunnel.peer()
        now = self.clock()
        cur = self.state.active
        same = cur is not None and (cur.pubkey, cur.endpoint) == (p.pubkey, p.endpoint)
        since = cur.since if same and cur else now
        profile = cur.profile if same and cur else ()
        srv = self.catalog.by_id(p.pubkey, p.endpoint.rsplit(":", 1)[0])
        if srv:
            self.state.active = ActiveServer(srv.name, p.pubkey, p.endpoint, srv.country,
                                             srv.city, since, profile)
        elif (p.pubkey, p.endpoint) == (self.exit_conf.peer_pubkey, self.exit_conf.peer_endpoint):
            self.state.active = ActiveServer(self.exit_conf.peer_name or "config peer",
                                             p.pubkey, p.endpoint, since=since)
        elif not same:
            self.state.active = ActiveServer("unknown peer", p.pubkey, p.endpoint, since=now)
        self.tunnel.tracker.start_grace(now)
        self.tunnel.close_ipv6()
        self.events.publish("started", server=self._name())
        self._save()

    # Health -------------------------------------------------------------------------
    async def run(self) -> None:
        while True:
            started = self.clock()
            try:
                if self.health == Health.DEGRADED:
                    await asyncio.sleep(self.backoff.step())
                    await self.step()
                    continue   # the backoff was the wait
                await self.step()
            except Exception:  # never let the loop die; the kill switch holds meanwhile
                log.exception("health loop error")
            # Fixed cadence: a failing cycle already spent its probe timeouts, so waiting a
            # full interval on top would stretch 3 strikes from ~15 s to ~24 s.
            await asyncio.sleep(max(0.5, self.s.health_interval - (self.clock() - started)))

    async def step(self) -> None:
        """One health cycle (the loop's body; tests drive it with a fake clock)."""
        async with self.lock:
            try:
                await self._step()
            finally:
                self._report_ipv6()

    async def _step(self) -> None:
        tracker = self.tunnel.tracker
        before = tracker.state
        res = await self.tunnel.check()
        now = self.clock()
        if res.ok:
            self._learn_country(res.loc)
        if before == Health.DEGRADED:
            if res.ok:
                tracker.feed(True, now)
                self.backoff.reset()
                self.events.publish("recovered", server=self._name())
            elif self.prefs.auto_failover:
                await self._failover("retry from degraded")
            return
        need = tracker.feed(res.ok, now)
        if tracker.state != before:
            self.events.publish("health", state=str(tracker.state), errors=res.errors)
        if need and self.prefs.auto_failover:
            await self._failover("health checks failed: " + "; ".join(res.errors))
        elif need:
            self.events.publish("unhealthy_no_failover", errors=res.errors)

    def _report_ipv6(self) -> None:
        """An `ipv6` event when the gate differs from what the last one said."""
        now_open = self.tunnel.ipv6_open
        if now_open != self._ipv6_reported:
            self._ipv6_reported = now_open
            last = self.tunnel.last
            self.events.publish("ipv6", open=now_open, server=self._name(),
                                exit_ip6=last.exit_ip6 if now_open and last else None)

    def _learn_country(self, loc: str | None) -> None:
        """A server not in the catalog: at least its country, from where it exits."""
        a = self.state.active
        if a and not a.country and loc:
            self.state.active = ActiveServer(a.name, a.pubkey, a.endpoint,
                                             self.catalog.country_for_code(loc) or loc,
                                             a.city, a.since, a.profile)
            self._save()

    # Switching (callers hold the lock) ----------------------------------------------
    def _blacklist(self, pubkey: str) -> None:
        self.state.blacklist.add(pubkey, self.clock() + self.s.blacklist_ttl)

    def _blacklisted(self) -> set[str]:
        now = self.clock()
        self.state.blacklist.prune(now)
        return self.state.blacklist.active(now)

    async def _try(self, srv: Server, profile: list[str] | None = None) -> bool:
        """Swap to `srv` and wait up to the warm-up for traffic. `profile`: the features to
        keep from now on (None: the current server's)."""
        profile = self._profile() if profile is None else profile
        endpoint = srv.endpoint(self.s.wg_port)
        try:
            ok = await self.tunnel.try_peer(srv.pubkey, endpoint, self.s.warmup)
        except WgError as e:
            self.events.publish("swap_error", server=srv.name, error=str(e))
            return False
        if ok:
            self.state.active = ActiveServer.of(srv, self.s.wg_port, self.clock(), profile)
            self._save()
        return ok

    def _exit_ip(self) -> str | None:
        return self.tunnel.last.exit_ip if self.tunnel.last else None

    async def _failover(self, reason: str) -> bool:
        now = self.clock()
        self.tunnel.tracker.failing_over(now)
        old = self.current_server()
        if old:
            self._blacklist(old.pubkey)
        self.events.publish("failover_start", server=old.name if old else None, reason=reason)
        cands = selection.choose_candidates(self.catalog.servers, old, self.prefs,
                                            self._blacklisted(), self.rng, "failover",
                                            profile=self._profile())
        for srv in cands:
            if await self._try(srv):
                self.backoff.reset()
                self.events.publish("failover_done", old=old.name if old else None, new=srv.name,
                                    exit_ip=self._exit_ip(), seconds=round(self.clock() - now, 1))
                return True
            self._blacklist(srv.pubkey)
            self.events.publish("candidate_failed", server=srv.name)
        self.tunnel.tracker.degraded(self.clock())
        self.events.publish("degraded", tried=len(cands))
        self._save()
        return False

    async def _switch(self, candidates: list[Server], reason: str,
                      profile: list[str] | None = None) -> Server:
        try:
            return await self._switch_to_first(candidates, reason, profile)
        finally:
            self._report_ipv6()

    async def _switch_to_first(self, candidates: list[Server], reason: str,
                               profile: list[str] | None) -> Server:
        """Try candidates in order; on total failure, go back to the old server."""
        if not candidates:
            raise SwitchError("no eligible server matches")
        old = self.current_server()
        old_profile = self._profile()
        t0 = self.clock()
        for srv in candidates[:MAX_SWITCH_TRIES]:
            if await self._try(srv, profile):
                self.events.publish("switched", old=old.name if old else None, new=srv.name,
                                    reason=reason, exit_ip=self._exit_ip(),
                                    seconds=round(self.clock() - t0, 1))
                return srv
            self._blacklist(srv.pubkey)
            self.events.publish("candidate_failed", server=srv.name)
        if old and await self._try(old, old_profile):
            self.events.publish("switch_reverted", server=old.name, reason=reason)
        self._save()
        raise SwitchError(f"none of {min(len(candidates), MAX_SWITCH_TRIES)} candidates worked")

    # What the owner can ask for ---------------------------------------------------------
    async def rotate(self, scope: str, who: str) -> Server:
        """Another server in the same city / country / pinned regions; a manual pick's
        profile is kept (P2P stays P2P)."""
        async with self.lock:
            cands = selection.choose_candidates(self.catalog.servers, self.current_server(),
                                                self.prefs, self._blacklisted(), self.rng, scope,
                                                profile=self._profile())
            return await self._switch(cands, f"rotate {scope} by {who}")

    async def select_region(self, country: str | None, city: str | None, who: str,
                            reason: str) -> Server:
        """A server in a country or city; starts a new (empty) profile."""
        async with self.lock:
            region = self._region(country, city)
            cands = selection.region_candidates(self.catalog.servers, self.current_server(),
                                                self.prefs, self._blacklisted(), self.rng, region)
            return await self._switch(cands, reason, [])

    def _region(self, country: str | None, city: str | None) -> Region:
        if country:
            return self.catalog.canonical_region(country, city)
        cur = self.current_server()   # a city alone: in the current country
        if not cur:
            raise PolicyError("give a country")
        return self.catalog.canonical_region(cur.country, city)

    async def select_server(self, name: str, ip: str | None, reason: str) -> Server:
        """One server by name (and, with `ip`, one physical server). Failover then keeps
        the features every physical server of the pick has."""
        async with self.lock:
            cands = selection.exact(self.catalog.by_name(name), self.prefs, name, ip)
            return await self._switch(cands, reason, common_features(cands))

    async def switch_to(self, srv: Server, reason: str) -> Server:
        """A server already vetted elsewhere (a find result); failover keeps its features."""
        async with self.lock:
            return await self._switch([srv], reason, srv.features())

    async def update_prefs(self, who: str, **changes) -> Prefs:
        """All or nothing: validate every change, switch servers if Secure Core changes
        (it is a mode), then keep the new preferences."""
        async with self.lock:
            new = self.prefs.updated(self.catalog, **changes)
            if new.secure_core != self.prefs.secure_core:
                cands = selection.mode_switch_candidates(self.catalog.servers, self.current_server(),
                                                         new, self._blacklisted(), self.rng)
                if not cands:
                    kind = "Secure Core" if new.secure_core else "regular"
                    raise SwitchError(f"no {kind} server in the current country or pinned regions")
                await self._switch(cands, f"secure core {'on' if new.secure_core else 'off'} by {who}", [])
            self.state.prefs = new
            self._save()
            self.events.publish("prefs", by=who, prefs=new.view())
            return new

    async def replace_catalog(self, catalog: Catalog) -> None:
        async with self.lock:
            old = len(self.catalog)
            self.catalog = catalog
            if self.cache:
                self.cache.save(catalog)
        self.events.publish("catalog", source=catalog.source, servers=len(catalog), before=old)

    async def replace_exit_key(self, new_key: str, old_key: str, conf: WgConf, reason: str) -> bool:
        """Run wg0 with another private key (slot a, same peer); keep it only if traffic
        passes within the key warm-up (a new key's registration may take a while to reach
        the server), else the old key is back. The caller saves the key when this is True."""
        async with self.lock:
            t0 = self.clock()
            kept, back = await self.tunnel.try_private_key(new_key, old_key, self.s.key_warmup,
                                                           self.s.warmup)
            if kept:
                self.exit_conf = conf
                self.events.publish("key_replaced", slot="a", reason=reason,
                                    seconds=round(self.clock() - t0, 1))
            else:
                self.events.publish("key_rejected", slot="a", reason=reason, restored_healthy=back)
            self._report_ipv6()
            return kept

    # Read-only views for others -----------------------------------------------------
    def find_candidates(self, countries: list[str], limit: int) -> list[Server]:
        known = {c.lower(): c for c in self.catalog.countries()}
        bad = [c for c in countries if c.lower() not in known]
        if bad:
            raise PolicyError(f"unknown countries: {bad}")
        return selection.find_candidates(self.catalog.servers, self.current_server(), self.prefs,
                                         self._blacklisted(), [known[c.lower()] for c in countries],
                                         limit, self.rng)

    def test_servers(self, n: int) -> list[Server]:
        """Servers in the current country, to test a prober key against."""
        return selection.choose_candidates(self.catalog.servers, self.current_server(), self.prefs,
                                           self._blacklisted(), self.rng, "country")[:n]

    def warnings(self, now: float) -> list[str]:
        out = [w for src in self.warning_sources for w in src(now)]
        out += self._ipv6_warnings()
        cat = self.catalog
        if cat.pushed and now - cat.fetched_at > self.s.pushed_catalog_max_age:
            out.append(f"pushed catalog is {(now - cat.fetched_at) / 86400:.0f} days old "
                       "(exitctl <provider> servers --push)")
        return out

    def _ipv6_warnings(self) -> list[str]:
        if not self.s.ipv6:
            return []
        if not self.tunnel.probe.source6:
            return ["IPv6 is on but the tunnel has no IPv6 address: "
                    + ("restart the exit to use the exit key's" if self.exit_conf.address6
                       else "rotate the exit key (slot a), its config has none")]
        cur, gate = self.current_server(), self.tunnel.ipv6
        if cur and cur.ipv6 and not gate.open and gate.failures >= gate.failures_needed:
            return ["IPv6 fails on this server, which the catalog lists as IPv6-capable"]
        return []

    def snapshot(self) -> dict:
        """`/v1/status` (contracts/controller/status.json)."""
        a, r, now = self.state.active, self.tunnel.last, self.clock()
        cur = self.current_server()
        tracker = self.tunnel.tracker
        return {
            "version": self.s.version,   # the node's release; `api` says which app speaks to it
            "api": API_VERSION,
            "server": a.name if a else None,
            "country": a.country if a else None,
            "city": a.city if a else None,
            "endpoint": a.endpoint if a else None,
            "pubkey": a.pubkey if a else None,
            "since": a.since if a else None,
            "profile": list(a.profile) if a else [],
            "entry": cur.entry if cur else "",
            "features": cur.features() if cur else [],
            "load": cur.load if cur else None,
            "health": str(tracker.state),
            "health_since": tracker.since,
            "exit_ip": r.exit_ip if r else None,
            "exit_loc": r.loc if r else None,
            "ipv6": self.tunnel.ipv6_open,   # forwarded IPv6 goes through the tunnel
            "exit_ip6": r.exit_ip6 if r and self.tunnel.ipv6_open else None,
            "handshake_age": round(r.handshake_age, 1) if r and r.handshake_age is not None else None,
            "last_ok_age": round(now - self.tunnel.last_ok_at, 1) if self.tunnel.last_ok_at else None,
            "errors": r.errors if r else [],
            "warnings": self.warnings(now),
            "prefs": self.prefs.view(),
            "provider": self.s.provider,
            "key_options": self.exit_conf.options,
            "catalog": self.catalog.view(),
            "blacklisted": len(self.state.blacklist.active(now)),
            "events": self.events.recent(10),
        }
