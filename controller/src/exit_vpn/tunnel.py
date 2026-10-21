"""The exit tunnel (wg0) and its health: the only object that changes wg0. It knows how to
check the tunnel, try a server and try a key, each judged by real traffic; it decides
nothing about which server or key (that is `service.ExitService`).

A WireGuard handshake proves nothing (Proton completes it even for unregistered keys), so
"works" always means one fully passing health cycle, waited for through a warm-up: fresh
sessions drop TCP for 4-14 s (PLAN Phase 0).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .health import HealthTracker
from .probes import CycleResult, HealthProbe
from .wg import Peer, WgError, WireGuard


class Tunnel:
    def __init__(self, wg: WireGuard, probe: HealthProbe, tracker: HealthTracker,
                 clock: Callable[[], float]):
        self.wg, self.probe, self.tracker, self.clock = wg, probe, tracker, clock
        self.last: CycleResult | None = None   # the latest health cycle
        self.last_ok_at = 0.0

    async def peer(self) -> Peer:
        peers = await self.wg.peers()
        if not peers:
            raise WgError(f"{self.wg.ifname} has no peer")
        return peers[0]

    async def check(self) -> CycleResult:
        """One health cycle (does not feed the tracker: callers decide what it means)."""
        try:
            peers = await self.wg.peers()
        except WgError as e:
            return CycleResult(False, False, False, None, errors=[str(e)])
        hs = peers[0].latest_handshake if peers else 0
        res = await self.probe.cycle(hs, self.clock())
        self.last = res
        if res.ok:
            self.last_ok_at = self.clock()
        return res

    async def healthy_within(self, seconds: float) -> bool:
        """After a change: wait up to `seconds` for one fully passing cycle."""
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

    async def try_peer(self, pubkey: str, endpoint: str, warmup: float) -> bool:
        """Point wg0 at another server; True once traffic passes. Raises WgError if the
        swap itself fails (the tunnel is unchanged then)."""
        await self.wg.swap_peer(pubkey, endpoint)
        return await self.healthy_within(warmup)

    async def try_private_key(self, new: str, old: str, warmup: float,
                              fallback_warmup: float) -> tuple[bool, bool]:
        """Run wg0 with another private key (same peer); keep it only if traffic passes,
        else put `old` back. Returns (kept, healthy after putting the old one back)."""
        await self.wg.set_private_key(new)
        if await self.healthy_within(warmup):
            return True, True
        await self.wg.set_private_key(old)
        return False, await self.healthy_within(fallback_warmup)
