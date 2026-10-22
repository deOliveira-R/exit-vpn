"""The exit service: which server the exit uses, and every change to it.

`ExitService` owns the two exit tunnels and their roles, the preferences, the blacklist and
the catalog in use. The *active* tunnel carries forwarded traffic; the *standby* tunnel (if
slot c has a key) is kept ready on another server of the exit's region, so that a handover
is a route change instead of a server change. Keys stay with interfaces (wg0: slot a, wg1:
slot c); the roles move. Changes to the active tunnel run under `lock`, changes to the
standby under `standby_lock`; a handover holds both (always in that order), so the health
loops, failover and API requests never change a tunnel concurrently. Changes to a tunnel go
through its `Tunnel`; candidate lists come from `selection`; events go to the `EventBus`;
state is saved through `StateStore`; the route switch is `routes.ActiveRoute`.

Active health cycle (`step`, every `health_interval` seconds):
- Healthy or suspect: a failed cycle is a strike; `strikes` in a row (outside the warm-up)
  call for a change, unless automatic failover is off.
- A change goes to the standby first (`_second_opinion`): it is checked at once; if it
  passes, a **handover** (one route change); if it fails too, the fault is taken to be the
  uplink or the test site: no change at all for up to `uplink_hold` (switching would end every
  open connection for nothing). No standby with a server: failover.
- Failover: blacklist the server, try candidates (same city, country, pinned regions) on the
  active tunnel itself until one carries traffic; none → degraded.
- Degraded: retry with backoff (30 s doubling to 5 min): recover if the server works again,
  else fail over again.
- IPv6: the active tunnel's IPv6 gate opens and closes with its own probe (`Tunnel`); an
  `ipv6` event reports each change as seen after a cycle or a switch. It never starts a change.

Standby cycle (`standby_step`, same interval): the standby is checked; it searches another
server when it has none, when its server no longer fits (outside the active server's
country, not eligible, blacklisted, or the active server itself), or when its own checks
fail (`strikes`) while the active tunnel works (if both fail, it is the uplink: it waits).
Servers that fail on the standby are avoided by the standby only (`standby_avoid`), never
put on the shared blacklist: a bad standby key must not steer the active tunnel's failover.
`standby_key_suspect` servers failing in a row mean the key itself is suspect: the standby
stops and says so.

Owner-requested switches (rotate, select, find) are made *before* breaking: the pick is
brought up on the standby tunnel while the active one keeps carrying traffic, then a
handover; the former active server stays as the standby if it still fits. Without a standby
the pick replaces the active tunnel's server, as before.

While anything is wrong, the kill switch (routing + nftables, set up before this process
starts) keeps exit traffic off the underlay; this code only decides which servers the
tunnels use and which tunnel is active.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from .routes import ActiveRoute, RouteError
from .servers import Region, Server, common_features
from .state import Blacklist, State, TunnelServer
from .tunnel import Tunnel
from .wg import WgError
from .wgconf import WgConf

log = logging.getLogger("exit_vpn")

MAX_SWITCH_TRIES = 5
# How long a handover waits for the standby's current health check to finish
STANDBY_WAIT = 10.0
# The controller API's version (contracts/controller/): raised when a change would break the
# app; additions that clients may ignore keep it.
API_VERSION = 1


class ExitService:
    def __init__(self, settings: Settings, tunnel: Tunnel, catalog: Catalog,
                 store: StateStore, events: EventBus, exit_conf: WgConf, *,
                 standby: Tunnel | None = None, route: ActiveRoute | None = None,
                 catalog_cache: CatalogCache | None = None,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.s, self.tunnel, self.catalog = settings, tunnel, catalog   # tunnel: the active one
        self.standby = standby   # the standby tunnel (None: this exit has no second interface)
        self.route = route
        self.store, self.events, self.cache = store, events, catalog_cache
        self.exit_conf = exit_conf   # slot a's config: Address/DNS and the key's options
        self.clock, self.rng = clock, rng or random.Random()
        self.state: State = store.load()[0]
        self.backoff = Backoff()
        self.lock = asyncio.Lock()
        self.gate_file = tunnel.gate_file   # the IPv6 gate goes with the active role
        # The standby's own bookkeeping
        self.standby_lock = asyncio.Lock()
        self.standby_searching = False
        self.standby_avoid = Blacklist()
        self.standby_backoff = Backoff()
        self.standby_next_search = 0.0
        self.standby_misses = 0
        self.standby_key_suspect = False
        self.uplink_since: float | None = None   # both tunnels failing since
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
        """The catalog entry for the active tunnel's peer; a peer the catalog does not know
        (a config's own peer, a server since removed) is described from what was saved."""
        return self._server_of(self.state.active)

    def _server_of(self, a: TunnelServer | None) -> Server | None:
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

    def _adopt(self, saved: TunnelServer | None, pubkey: str, endpoint: str,
               conf: WgConf | None, now: float) -> TunnelServer:
        """Who a tunnel's peer is, in order: the catalog entry with this (pubkey, IP); the
        config's own peer name; the saved state (a server since dropped from the catalog)."""
        same = saved is not None and (saved.pubkey, saved.endpoint) == (pubkey, endpoint)
        since = saved.since if same and saved else now
        profile = saved.profile if same and saved else ()
        srv = self.catalog.by_id(pubkey, endpoint.rsplit(":", 1)[0])
        if srv:
            return TunnelServer(srv.name, pubkey, endpoint, srv.country, srv.city, since, profile)
        if conf and (pubkey, endpoint) == (conf.peer_pubkey, conf.peer_endpoint):
            return TunnelServer(conf.peer_name or "config peer", pubkey, endpoint, since=since)
        if same and saved:
            return saved
        return TunnelServer("unknown peer", pubkey, endpoint, since=now)

    async def start(self) -> None:
        """Adopt whatever init-wg configured: wg0 active on the last good server (or its
        config's), wg1 standby on the last standby server. After a restart of the controller
        alone the roles may be swapped: the route file says which tunnel is active."""
        if self.standby and self.route and self.route.current() == self.standby.ifname:
            self.tunnel, self.standby = self.standby, self.tunnel
        now = self.clock()
        p = await self.tunnel.peer()
        self.state.active = self._adopt(self.state.active, p.pubkey, p.endpoint,
                                        self.exit_conf, now)
        self.tunnel.tracker.start_grace(now)
        self.tunnel.make_active(self.gate_file)
        if self.standby:
            saved, self.state.standby = self.state.standby, None
            self.standby.make_standby()
            if self.standby.keyed:
                with contextlib.suppress(WgError):
                    sp = await self.standby.peer()
                    if (sp.pubkey, sp.endpoint) != (p.pubkey, p.endpoint):
                        self.state.standby = self._adopt(saved, sp.pubkey, sp.endpoint, None, now)
                self.standby.tracker.start_grace(now)
        self.events.publish("started", server=self._name(),
                            standby=self.state.standby.name if self.state.standby else None)
        self._save()

    # Health -------------------------------------------------------------------------
    async def run(self) -> None:
        await asyncio.gather(self._loop(self._active_cycle), self._loop(self.standby_step))

    async def _loop(self, cycle) -> None:
        while True:
            started = self.clock()
            try:
                await cycle()
            except Exception:  # never let a loop die; the kill switch holds meanwhile
                log.exception("health loop error")
            # Fixed cadence: a failing cycle already spent its probe timeouts, so waiting a
            # full interval on top would stretch 3 strikes from ~15 s to ~24 s.
            await asyncio.sleep(max(0.5, self.s.health_interval - (self.clock() - started)))

    async def _active_cycle(self) -> None:
        if self.health == Health.DEGRADED:
            await asyncio.sleep(self.backoff.step())
        await self.step()

    async def step(self) -> None:
        """One health cycle of the active tunnel (tests drive it with a fake clock)."""
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
            self.uplink_since = None
        if before == Health.DEGRADED:
            if res.ok:
                tracker.feed(True, now)
                self.backoff.reset()
                self.events.publish("recovered", server=self._name())
            elif self.prefs.auto_failover:
                if not await self._second_opinion("retry from degraded"):
                    await self._failover("retry from degraded")
            return
        need = tracker.feed(res.ok, now)
        if tracker.state != before:
            self.events.publish("health", state=str(tracker.state), errors=res.errors)
        if need and self.prefs.auto_failover:
            reason = "health checks failed: " + "; ".join(res.errors)
            if not await self._second_opinion(reason):
                await self._failover(reason)
        elif need:
            self.events.publish("unhealthy_no_failover", errors=res.errors)

    def _report_ipv6(self) -> None:
        """An `ipv6` event when the gate differs from what the last one said."""
        now_open = self.tunnel.ipv6_open
        if now_open != self._ipv6_reported:
            self._ipv6_reported = now_open
            last = self.tunnel.last
            why = {} if now_open or not last or not last.ipv6_error else {"error": last.ipv6_error}
            self.events.publish("ipv6", open=now_open, server=self._name(),
                                exit_ip6=last.exit_ip6 if now_open and last else None, **why)

    def _learn_country(self, loc: str | None) -> None:
        """A server not in the catalog: at least its country, from where it exits."""
        a = self.state.active
        if a and not a.country and loc:
            self.state.active = TunnelServer(a.name, a.pubkey, a.endpoint,
                                             self.catalog.country_for_code(loc) or loc,
                                             a.city, a.since, a.profile)
            self._save()

    # The standby and the handover ---------------------------------------------------
    def standby_on(self) -> bool:
        return bool(self.standby and self.standby.keyed and not self.standby_key_suspect)

    def standby_has_server(self) -> bool:
        return self.standby_on() and self.state.standby is not None and not self.standby_searching

    def _fits(self, srv: Server | None) -> bool:
        """Whether a server may be the standby for the current active server: eligible under
        the preferences and the active server's profile, not blacklisted, not the active
        server, and in its country (a handover never moves the exit elsewhere)."""
        act = self.current_server()
        if srv is None or (act and srv.pubkey == act.pubkey):
            return False
        if not srv.hostname and not self.catalog.by_id(srv.pubkey, srv.ip):
            return False   # a peer the catalog does not know
        if not selection.eligible(srv, self.prefs, self._profile()):
            return False
        if srv.pubkey in self._blacklisted():
            return False
        return not (act and act.country) or srv.country == act.country

    async def _second_opinion(self, reason: str) -> bool:
        """The active tunnel fails: ask the standby (a fresh check). It works: hand over
        (True). It fails too: the uplink or the test site is at fault, so nothing changes
        (True) for up to `uplink_hold`. No standby to ask, or the hold is over: False (the
        caller fails over)."""
        if not self.standby_has_server():
            return False
        try:
            await asyncio.wait_for(self.standby_lock.acquire(), STANDBY_WAIT)
        except TimeoutError:
            return False
        try:
            if self.state.standby is None:
                return False
            res = await self.standby.check()
            now = self.clock()
            self.standby.tracker.feed(res.ok, now)
            if res.ok:
                return await self._handover(reason, failed=True)
            if self.uplink_since is None:
                self.uplink_since = now
                self.events.publish("uplink_suspect", server=self._name(),
                                    standby=self.state.standby.name,
                                    errors=self.tunnel.last.errors if self.tunnel.last else [])
            return now - self.uplink_since < self.s.uplink_hold
        finally:
            self.standby_lock.release()

    async def _handover(self, reason: str, failed: bool) -> bool:
        """Swap the roles (callers hold both locks): forwarded traffic moves to the standby
        tunnel in one route change. `failed`: the old server is blacklisted (the standby then
        moves away from it); otherwise it stays as the standby if it still fits."""
        t0 = self.clock()
        old_t, new_t = self.tunnel, self.standby
        old_srv, new_srv = self.state.active, self.state.standby
        old_t.make_standby()   # closes the IPv6 gate while it is still this tunnel's
        if self.route:
            try:
                await self.route.set(new_t.ifname)
            except (RouteError, OSError) as e:
                self.events.publish("handover_error", server=new_srv.name if new_srv else None,
                                    error=str(e))
                old_t.make_active(self.gate_file)
                return False
        self.tunnel, self.standby = new_t, old_t
        self.state.active, self.state.standby = new_srv, old_srv
        new_t.make_active(self.gate_file)
        new_t.tracker.feed(True, self.clock())
        old_t.tracker.start_grace(self.clock())
        if failed and old_srv:
            self._blacklist(old_srv.pubkey)
        self.backoff.reset()
        self._save()
        self.events.publish("handover", old=old_srv.name if old_srv else None,
                            new=new_srv.name if new_srv else None, reason=reason,
                            exit_ip=self._exit_ip(), seconds=round(self.clock() - t0, 2))
        return True

    async def standby_step(self) -> None:
        """One standby cycle: check it, or find it a (new) server."""
        if not self.standby_on():
            return
        async with self.standby_lock:
            if not self.standby_on():
                return   # changed while waiting for the lock (a key upload just failed)
            st, cur = self.standby, self.state.standby
            if cur and not self._fits(self._server_of(cur)):
                self.events.publish("standby", state="moving", server=cur.name,
                                    reason="no longer fits the exit's region or preferences")
                self.state.standby = None
            elif cur:
                res = await st.check()
                need = st.tracker.feed(res.ok, self.clock())
                if not need:
                    return
                if self.tunnel.last and not self.tunnel.last.ok:
                    return   # both fail: the uplink; the standby's server is not to blame
                self.standby_avoid.add(cur.pubkey, self.clock() + self.s.standby_blacklist_ttl)
                self.events.publish("standby", state="lost", server=cur.name,
                                    reason="health checks failed: " + "; ".join(res.errors))
                self.state.standby = None
            if self.clock() < self.standby_next_search:
                return
            await self._standby_search()

    async def _standby_search(self, first_warmup: float | None = None) -> None:
        """Bring the standby up on the best fitting server (same city first). Callers hold
        `standby_lock`. `first_warmup`: a longer wait on the first candidate (a new key)."""
        self.standby_searching = True
        try:
            now = self.clock()
            self.standby_avoid.prune(now)
            avoid = self._blacklisted() | self.standby_avoid.active(now)
            cands = [c for c in selection.choose_candidates(
                self.catalog.servers, self.current_server(), self.prefs, avoid, self.rng,
                "failover", profile=self._profile()) if self._fits(c)]
            for i, srv in enumerate(cands[:MAX_SWITCH_TRIES]):
                if self.tunnel.last and not self.tunnel.last.ok:
                    return   # the active tunnel fails too: probably the uplink, try later
                warmup = first_warmup if i == 0 and first_warmup else self.s.warmup
                try:
                    ok = await self.standby.try_peer(srv.pubkey, srv.endpoint(self.s.wg_port),
                                                     warmup)
                except WgError as e:
                    self.events.publish("swap_error", server=srv.name, error=str(e))
                    ok = False
                if ok:
                    self.state.standby = TunnelServer.of(srv, self.s.wg_port, self.clock(),
                                                         self._profile())
                    self.standby_misses = 0
                    self.standby_backoff.reset()
                    self.standby_next_search = 0.0
                    self._save()
                    self.events.publish("standby", state="ready", server=srv.name,
                                        exit_ip=self.standby.last.exit_ip if self.standby.last else None)
                    return
                last = self.standby.last
                self.events.publish("standby", state="candidate_failed", server=srv.name,
                                    errors=last.errors if last else [])
                self.standby_avoid.add(srv.pubkey, self.clock() + self.s.standby_blacklist_ttl)
                self.standby_misses += 1
                if self.standby_misses >= self.s.standby_key_suspect:
                    self.standby_key_suspect = True
                    self.events.publish("standby", state="key_suspect", server=srv.name,
                                        reason=f"{self.standby_misses} servers in a row carried "
                                               "no traffic on the standby key")
                    return
            self.standby_next_search = self.clock() + self.standby_backoff.step()
            self.events.publish("standby", state="none_found", server=None, tried=min(len(cands), MAX_SWITCH_TRIES))
        finally:
            self.standby_searching = False

    # Switching (callers hold the lock) ----------------------------------------------
    def _blacklist(self, pubkey: str) -> None:
        self.state.blacklist.add(pubkey, self.clock() + self.s.blacklist_ttl)

    def _blacklisted(self) -> set[str]:
        now = self.clock()
        self.state.blacklist.prune(now)
        return self.state.blacklist.active(now)

    async def _try(self, srv: Server, profile: list[str] | None = None) -> bool:
        """Swap the active tunnel to `srv` and wait up to the warm-up for traffic. `profile`:
        the features to keep from now on (None: the current server's)."""
        profile = self._profile() if profile is None else profile
        endpoint = srv.endpoint(self.s.wg_port)
        try:
            ok = await self.tunnel.try_peer(srv.pubkey, endpoint, self.s.warmup)
        except WgError as e:
            self.events.publish("swap_error", server=srv.name, error=str(e))
            return False
        if ok:
            self.state.active = TunnelServer.of(srv, self.s.wg_port, self.clock(), profile)
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
        """Try candidates in order: on the standby, then a handover (the active tunnel keeps
        carrying traffic meanwhile); without a free standby, on the active tunnel, and on total
        failure back to the old server."""
        if not candidates:
            raise SwitchError("no eligible server matches")
        if self.standby_on():
            try:
                await asyncio.wait_for(self.standby_lock.acquire(), STANDBY_WAIT)
            except TimeoutError:
                pass   # the standby is busy searching: switch the active tunnel itself
            else:
                try:
                    return await self._switch_via_standby(candidates, reason, profile)
                finally:
                    self.standby_lock.release()
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

    async def _switch_via_standby(self, candidates: list[Server], reason: str,
                                  profile: list[str] | None) -> Server:
        """Callers hold both locks. The active tunnel is never touched until the handover."""
        old = self.current_server()
        profile = self._profile() if profile is None else profile
        t0 = self.clock()
        for srv in candidates[:MAX_SWITCH_TRIES]:
            sb = self.state.standby
            if sb and (sb.pubkey, sb.ip) == (srv.pubkey, srv.ip) and self.standby.last \
                    and self.standby.last.ok:
                ok = True   # already up and proven on the standby
            else:
                self.state.standby = None
                try:
                    ok = await self.standby.try_peer(srv.pubkey, srv.endpoint(self.s.wg_port),
                                                     self.s.warmup)
                except WgError as e:
                    self.events.publish("swap_error", server=srv.name, error=str(e))
                    ok = False
            if ok:
                self.state.standby = TunnelServer.of(srv, self.s.wg_port, self.clock(), profile)
                if not await self._handover(reason, failed=False):
                    raise SwitchError("the route could not be switched")
                self.events.publish("switched", old=old.name if old else None, new=srv.name,
                                    reason=reason, exit_ip=self._exit_ip(),
                                    seconds=round(self.clock() - t0, 1), handover=True)
                return srv
            self._blacklist(srv.pubkey)
            self.events.publish("candidate_failed", server=srv.name)
        self._save()
        raise SwitchError(f"none of {min(len(candidates), MAX_SWITCH_TRIES)} candidates worked "
                          "(the exit is unchanged)")

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

    def tunnel_for(self, slot: str) -> Tunnel | None:
        return next((t for t in (self.tunnel, self.standby) if t and t.slot == slot), None)

    async def replace_exit_key(self, new_key: str, old_key: str, conf: WgConf, reason: str,
                               slot: str = "a") -> bool:
        """Run the tunnel of `slot` (a: wg0, c: wg1) with another private key; keep it only if
        traffic passes within the key warm-up (a new key's registration may take a while to
        reach the server), else the old key is back. On the standby tunnel this never touches
        forwarded traffic; a standby without a key or server first finds one with the new key.
        The caller saves the key when this is True."""
        t0 = self.clock()
        async with self.lock:
            t = self.tunnel_for(slot)
            if t is None:
                raise PolicyError(f"this exit has no tunnel for slot {slot}")
            if t is self.tunnel:
                kept, back = await self._replace_key_on(t, new_key, old_key)
                return self._key_result(kept, back, slot, conf, reason, t0)
        # The standby's key: under its own lock only, so the active tunnel's health cycles go
        # on meanwhile (a first key may search several servers). A handover needs both locks,
        # so the roles cannot change while this runs.
        async with self.standby_lock:
            if self.tunnel_for(slot) is not self.standby:
                raise SwitchError("the tunnels changed roles meanwhile; try again")
            kept, back = await self._replace_standby_key(new_key, old_key)
            return self._key_result(kept, back, slot, conf, reason, t0)

    def _key_result(self, kept: bool, back: bool, slot: str, conf: WgConf, reason: str,
                    t0: float) -> bool:
        if kept:
            if slot == "a":
                self.exit_conf = conf
            self.events.publish("key_replaced", slot=slot, reason=reason,
                                seconds=round(self.clock() - t0, 1))
        else:
            self.events.publish("key_rejected", slot=slot, reason=reason, restored_healthy=back)
        self._report_ipv6()
        return kept

    async def _replace_key_on(self, t: Tunnel, new_key: str, old_key: str) -> tuple[bool, bool]:
        return await t.try_private_key(new_key, old_key, self.s.key_warmup, self.s.warmup)

    async def _replace_standby_key(self, new_key: str, old_key: str) -> tuple[bool, bool]:
        st = self.standby
        if st.keyed and old_key and self.state.standby is not None:
            return await self._replace_key_on(st, new_key, old_key)
        # No key (or no server) yet: the new key must bring the standby up on some server
        await st.wg.set_private_key(new_key)
        was_keyed, st.keyed = st.keyed, True
        self.standby_key_suspect, self.standby_misses = False, 0
        self.standby_next_search = 0.0
        self.state.standby = None
        await self._standby_search(first_warmup=self.s.standby_key_warmup)
        if self.state.standby is not None:
            return True, True
        st.keyed = was_keyed
        if old_key:
            await st.wg.set_private_key(old_key)
        self.standby_key_suspect = False
        return False, False

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
        out += self._standby_warnings()
        cat = self.catalog
        if cat.pushed and now - cat.fetched_at > self.s.pushed_catalog_max_age:
            out.append(f"pushed catalog is {(now - cat.fetched_at) / 86400:.0f} days old "
                       "(exitctl <provider> servers --push)")
        return out

    def _standby_warnings(self) -> list[str]:
        if not self.standby:
            return []
        if not self.standby.keyed:
            return [f"no standby tunnel: slot c has no key (exitctl {self.s.provider} rotate --slot c)"]
        if self.standby_key_suspect:
            return ["the standby key (slot c) carried no traffic on several servers in a row: "
                    f"rotate it (exitctl {self.s.provider} rotate --slot c)"]
        return []

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

    def standby_view(self) -> dict | None:
        """The standby in status (contracts/controller/status.json); None: no second tunnel."""
        if not self.standby:
            return None
        sb, st = self.state.standby, self.standby
        if not st.keyed:
            state = "off"
        elif self.standby_key_suspect:
            state = "key_suspect"
        elif self.standby_searching or sb is None:
            state = "searching"
        elif st.tracker.state == Health.HEALTHY:
            state = "ready"
        else:
            state = str(st.tracker.state)
        r = st.last
        return {"state": state, "server": sb.name if sb else None,
                "country": sb.country if sb else None, "city": sb.city if sb else None,
                "since": sb.since if sb else None,
                "exit_ip": r.exit_ip if r and r.ok and sb else None,
                "interface": st.ifname}

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
            "standby": self.standby_view(),
            "prefs": self.prefs.view(),
            "provider": self.s.provider,
            "key_options": self.exit_conf.options,
            "catalog": self.catalog.view(),
            "blacklisted": len(self.state.blacklist.active(now)),
            "events": self.events.recent(10),
        }
