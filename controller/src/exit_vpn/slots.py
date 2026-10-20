"""Uploading keys into slots (PLAN Phase 3b): validate, try, keep or roll back."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .controller import Controller
from .keys import SLOTS, KeyFile, KeyRejected, KeyStore, load_text
from .wg import public_key

log = logging.getLogger("exit_vpn")
TEST_URL = "https://www.cloudflare.com/cdn-cgi/trace"
TEST_SERVERS = 3
DAY = 86400.0


class KeyFailed(RuntimeError):
    """The key was well-formed but carried no traffic; the previous key is back."""


class SlotManager:
    def __init__(self, ctl: Controller, store: KeyStore, prober=None,
                 code_for: Callable[[str], str] | None = None,
                 forbid_ips: Callable[[], list[str]] | None = None,
                 pubkey: Callable[[str], Awaitable[str]] = public_key):
        self.ctl, self.store, self.prober = ctl, store, prober
        self.code_for = code_for or (lambda _c: "")
        self.forbid_ips = forbid_ips or (lambda: [])
        self.pubkey = pubkey
        self.lock = asyncio.Lock()
        ctl.conf_key = self._exit_private_key
        ctl.warnings = self.warnings

    def _exit_private_key(self) -> str:
        cur = self.store.current("a")
        return cur.private_key if cur else ""

    async def put(self, slot: str, text: str, meta: dict, who: str) -> dict:
        if slot not in SLOTS:
            raise KeyRejected(f"unknown slot {slot!r}")
        if slot == "a":
            like = self.ctl.conf
        else:
            cur = self.store.current("b")
            like = cur.conf if cur else None
        key = load_text(text, like)
        meta = {**meta, "wg_public_key": await self.pubkey(key.private_key),
                "uploaded_by": who, "uploaded_at": int(time.time())}
        async with self.lock:
            if slot == "a":
                ok = await self.ctl.replace_key(key, lambda: self.store.commit("a", key, meta),
                                                f"upload by {who}")
                if not ok:
                    raise KeyFailed("the new key carried no traffic; the previous key is back")
            else:
                await self._put_probe_key(key, meta, who)
        return await self.view(slot)

    async def _put_probe_key(self, key: KeyFile, meta: dict, who: str) -> None:
        if not self.prober:
            raise KeyRejected("no prober configured")
        prev, prev_meta = self.store.current("b"), self.store.meta("b")
        was_uploaded = self.store.source("b") == "uploaded"
        self.store.commit("b", key, meta)
        try:
            await self.prober.reload_key()
            if await self._probe_works():
                self.ctl._event("key_replaced", slot="b", reason=f"upload by {who}")
                return
        except Exception as e:  # prober down or reload refused: undo
            log.warning("prober key reload failed: %s", e)
        self.store.restore("b", prev, prev_meta, was_uploaded)
        try:
            await self.prober.reload_key()
        except Exception as e:
            log.warning("prober key restore failed: %s", e)
        self.ctl._event("key_rejected", slot="b", reason=f"upload by {who}")
        raise KeyFailed("the new prober key carried no traffic; the previous key is back")

    async def _probe_works(self) -> bool:
        for srv in self.ctl.candidates("country")[:TEST_SERVERS]:
            r = await self.prober.probe(srv, self.code_for(srv.country), TEST_URL, "http",
                                        False, self.forbid_ips())
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
        self.ctl._event("key_meta", slot=slot, by=who, expires_at=meta.get("expires_at"))
        return await self.view(slot)

    # Views ------------------------------------------------------------------------
    async def view(self, slot: str) -> dict:
        cur = self.store.current(slot)
        pub = await self.pubkey(cur.private_key) if cur else None
        meta = self.store.meta(slot)
        if meta.get("wg_public_key") != pub:
            meta = {}   # stale facts about some other key
        out = {"slot": slot, "role": "exit" if slot == "a" else "prober",
               "source": self.store.source(slot), "public_key": pub,
               "options": cur.conf.options if cur else {}, "meta": meta,
               "expires_in_days": None}
        if meta.get("expires_at"):
            out["expires_in_days"] = round((meta["expires_at"] - time.time()) / DAY, 1)
        return out

    async def views(self) -> list[dict]:
        return [await self.view(s) for s in SLOTS]

    def warnings(self) -> list[str]:
        out = []
        for s in SLOTS:
            exp = self.store.meta(s).get("expires_at")
            if exp and exp - time.time() < self.ctl.s.key_expiry_warn_days * DAY:
                days = (exp - time.time()) / DAY
                out.append(f"slot {s} key certificate "
                           + (f"expires in {days:.0f} days" if days > 0 else "has expired"))
        return out
