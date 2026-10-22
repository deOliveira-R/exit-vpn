"""Key slots: uploading a key into slot a (the exit tunnel wg0), b (the prober) or c (the
standby tunnel wg1), keeping it only if it carries traffic.

The slots are tested in opposite orders, because of where the key runs:
- Slots a and c: the new key is tried on its tunnel first (`ExitService.replace_exit_key`) and
  saved only once traffic passes; otherwise the tunnel is back on the old key and nothing
  was saved. (Either tunnel may be the active one at the time; on the standby, forwarded
  traffic is never touched. A first slot c key brings the standby up.)
- Slot b: the prober reads its key from the slot's file, so the key is saved first, the
  prober reloads it and probes a few servers; if none answers, the old key is restored.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .events import EventBus
from .keys import SLOTS, KeyFile, KeyRejected, KeyStore, load_text
from .prober_client import ProberClient
from .service import ExitService
from .wg import public_key

log = logging.getLogger("exit_vpn")
TEST_URL = "https://www.cloudflare.com/cdn-cgi/trace"
TEST_SERVERS = 3
TUNNEL_SLOTS = ("a", "c")   # keys of the exit's own tunnels (wg0, wg1)
ROLES = {"a": "exit", "b": "prober", "c": "standby"}
DAY = 86400.0


class KeyFailed(RuntimeError):
    """The key was well-formed but carried no traffic; the previous key is back."""


class KeySlots:
    def __init__(self, service: ExitService, store: KeyStore, events: EventBus,
                 prober: ProberClient | None = None,
                 code_for: Callable[[str], str] = lambda _c: "",
                 forbid_ips: Callable[[], list[str]] = lambda: [],
                 pubkey: Callable[[str], Awaitable[str]] = public_key,
                 expiry_warn_days: float = 14.0):
        self.service, self.store, self.events, self.prober = service, store, events, prober
        self.code_for, self.forbid_ips, self.pubkey = code_for, forbid_ips, pubkey
        self.expiry_warn_days = expiry_warn_days
        self.lock = asyncio.Lock()

    async def put(self, slot: str, text: str, meta: dict, who: str) -> dict:
        if slot not in SLOTS:
            raise KeyRejected(f"unknown slot {slot!r}")
        if slot in TUNNEL_SLOTS:
            like = self.service.exit_conf   # both tunnels share the routing built on it
        else:
            cur = self.store.current("b")
            like = cur.conf if cur else None
        key = load_text(text, like)
        meta = {**meta, "wg_public_key": await self.pubkey(key.private_key),
                "uploaded_by": who, "uploaded_at": int(time.time())}
        async with self.lock:
            if slot in TUNNEL_SLOTS:
                await self._put_exit_key(slot, key, meta, who)
            else:
                await self._put_probe_key(key, meta, who)
        return await self.view(slot)

    async def _put_exit_key(self, slot: str, key: KeyFile, meta: dict, who: str) -> None:
        cur = self.store.current(slot)
        if not await self.service.replace_exit_key(key.private_key, cur.private_key if cur else "",
                                                   key.conf, f"upload by {who}", slot=slot):
            raise KeyFailed("the new key carried no traffic; the previous key is back")
        self.store.commit(slot, key, meta)

    async def _put_probe_key(self, key: KeyFile, meta: dict, who: str) -> None:
        if not self.prober:
            raise KeyRejected("no prober configured")
        prev, prev_meta = self.store.current("b"), self.store.meta("b")
        was_uploaded = self.store.source("b") == "uploaded"
        self.store.commit("b", key, meta)
        try:
            await self.prober.reload_key()
            if await self._probe_works():
                self.events.publish("key_replaced", slot="b", reason=f"upload by {who}")
                return
        except Exception as e:  # prober down or reload refused: undo
            log.warning("prober key reload failed: %s", e)
        self.store.restore("b", prev, prev_meta, was_uploaded)
        try:
            await self.prober.reload_key()
        except Exception as e:
            log.warning("prober key restore failed: %s", e)
        self.events.publish("key_rejected", slot="b", reason=f"upload by {who}")
        raise KeyFailed("the new prober key carried no traffic; the previous key is back")

    async def _probe_works(self) -> bool:
        for srv in self.service.test_servers(TEST_SERVERS):
            r = await self.prober.probe(srv, country_code=self.code_for(srv.country), url=TEST_URL,
                                        mode="http", forbid_ips=self.forbid_ips())
            if r.get("outcome") != "error":
                return True
        return False

    async def put_meta(self, slot: str, meta: dict, who: str) -> dict:
        if slot not in SLOTS:
            raise KeyRejected(f"unknown slot {slot!r}")
        cur = self.store.current(slot)
        if not cur or meta.get("wg_public_key") != await self.pubkey(cur.private_key):
            raise KeyRejected("wg_public_key does not match the slot's key")
        async with self.lock:
            self.store.write_meta(slot, {**meta, "updated_by": who, "updated_at": int(time.time())})
        self.events.publish("key_meta", slot=slot, by=who, expires_at=meta.get("expires_at"))
        return await self.view(slot)

    # Views ------------------------------------------------------------------------
    async def view(self, slot: str) -> dict:
        """One slot (contracts/controller/key_slot.json); never the private key."""
        cur = self.store.current(slot)
        pub = await self.pubkey(cur.private_key) if cur else None
        meta = self.store.meta(slot)
        if meta.get("wg_public_key") != pub:
            meta = {}   # stale facts about some other key
        out = {"slot": slot, "role": ROLES[slot],
               "source": self.store.source(slot), "public_key": pub,
               "options": cur.conf.options if cur else {}, "meta": meta,
               "expires_in_days": None}
        if meta.get("expires_at"):
            out["expires_in_days"] = round((meta["expires_at"] - time.time()) / DAY, 1)
        return out

    async def views(self) -> list[dict]:
        return [await self.view(s) for s in SLOTS]

    def warnings(self, now: float) -> list[str]:
        out = []
        for s in SLOTS:
            exp = self.store.meta(s).get("expires_at")
            if exp and exp - now < self.expiry_warn_days * DAY:
                days = (exp - now) / DAY
                out.append(f"slot {s} key certificate "
                           + (f"expires in {days:.0f} days" if days > 0 else "has expired"))
        return out
