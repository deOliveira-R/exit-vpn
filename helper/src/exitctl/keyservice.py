"""What exitctl does with keys, provider-neutral (PLAN Phase 3b): rotate a slot's key, renew
its certificate, list the account's certificates against the slots, import a .conf, push the
live server list. Each works through a `Provider` and the `ExitNode`.

Rotation is make-then-swap: the new certificate is created, the controller tries the key on
the live tunnel and keeps it only if traffic passes; if it refuses, the new certificate is
deleted again. The old certificate is deleted only after the swap (unless `keep_old`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .exitnode import ExitNode
from .model import Certificate, ExitctlError, KeyMeta, NodeError, ProviderError, resolve_levers
from .providers.base import Provider
from .wgkeys import Peer


@dataclass(frozen=True)
class DeviceNaming:
    """Keys the exit node uses are named `<node>-<slot>-<yyyymmdd>` on the provider account,
    so its certificates can be told from the owner's other devices."""
    node: str

    def name(self, slot: str, now: datetime | None = None) -> str:
        return f"{self.node}-{slot}-{(now or datetime.now(UTC)):%Y%m%d}"

    def owns(self, device_name: str) -> bool:
        return device_name.startswith(f"{self.node}-")


def fmt_time(ts) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d") if ts else "-"


class KeyService:
    def __init__(self, provider: Provider, node: ExitNode, say: Callable[[str], None] = lambda _m: None):
        self.p, self.node, self.say = provider, node, say
        self.naming = DeviceNaming(node.node_name)

    async def slot_levers(self, slot_view: dict) -> dict:
        """The slot's lever values: recorded if this provider made the key, else what its
        .conf comments say (a dashboard-made key), else the defaults."""
        meta = slot_view.get("meta") or {}
        recorded = (meta.get("options") or {}) if meta.get("provider") == self.p.name else {}
        return resolve_levers(self.p.levers(), recorded, slot_view.get("options") or {})

    async def _certificate_for(self, wg_public: str) -> Certificate | None:
        if not wg_public:
            return None
        return next((c for c in await self.p.certificates() if c.wg_public_key == wg_public), None)

    async def rotate(self, slot: str, overrides: dict, keep_old: bool = False) -> dict:
        status, cur = await self.node.status(), await self.node.key(slot)
        if not (status.get("pubkey") and status.get("endpoint")):
            raise ExitctlError("the controller reports no current server")
        vals = await self.slot_levers(cur) | overrides
        old = await self._certificate_for(cur.get("public_key") or "")
        peer = Peer(status.get("server") or "server", status["pubkey"], status["endpoint"])
        name = self.naming.name(slot)
        self.say(f"creating {self.p.name} key {name} ({', '.join(f'{k}={v}' for k, v in vals.items())})")
        new = await self.p.create_key(name, vals, peer)
        self.say(f"uploading to slot {slot}; the controller tests it (up to a minute)")
        try:
            view = await self.node.put_key(slot, new.conf, new.meta.to_json())
        except NodeError:
            self.say("the controller kept the previous key; deleting the new certificate")
            try:
                await self.p.delete_certificate(new.meta.cert_id or "")
            except ProviderError as e:
                self.say(f"could not delete it: {e}")
            raise
        deleted = None
        if old and not keep_old and old.id != new.meta.cert_id:
            try:
                await self.p.delete_certificate(old.id)
                deleted = old.id
            except ProviderError as e:
                self.say(f"new key is live, but the old certificate could not be deleted: {e}")
        return {"slot": slot, "key": view, "deleted_old": deleted}

    async def renew(self, slot: str) -> dict:
        cur = await self.node.key(slot)
        recorded = cur.get("meta") or {}
        if recorded.get("provider") == self.p.name and recorded.get("client_public_key"):
            meta = KeyMeta.from_json(recorded)
        else:   # a key made elsewhere: find its certificate on the account
            cert = await self._certificate_for(cur.get("public_key") or "")
            if not cert:
                raise ExitctlError(f"slot {slot}'s key is not a {self.p.name} certificate on this account")
            meta = self.p.renewal_meta(cert)
        before = meta.expires_at
        meta = await self.p.renew_key(meta)
        meta.wg_public_key = cur.get("public_key")
        view = await self.node.put_meta(slot, meta.to_json())
        self.say(f"slot {slot}: expiry {fmt_time(before)} → {fmt_time(meta.expires_at)}")
        return {"slot": slot, "key": view}

    async def certificates(self) -> list[dict]:
        """The account's certificates, each with the slot that uses it and whether it is an
        orphan (named for this exit node, used by no slot: safe to delete)."""
        certs = await self.p.certificates()
        try:
            slots = {k["public_key"]: k["slot"] for k in await self.node.keys() if k.get("public_key")}
        except NodeError:
            slots = None   # node unreachable: list the account, call nothing an orphan
        return [c.to_json(slot=(slots or {}).get(c.wg_public_key),
                          orphan=slots is not None and self.naming.owns(c.device_name)
                          and c.wg_public_key not in slots)
                for c in certs]

    async def import_conf(self, slot: str, path: Path, provider_name: str) -> dict:
        meta = KeyMeta(provider=provider_name, device_name=path.stem, created_at=int(time.time()))
        return {"slot": slot, "key": await self.node.put_key(slot, path.read_text(), meta.to_json())}

    async def servers(self, push: bool) -> dict:
        rows = await self.p.servers()
        out = {"servers": len(rows), "enabled": sum(r["enabled"] for r in rows),
               "countries": len({r["country_code"] for r in rows})}
        if push:
            self.say(f"pushing {len(rows)} servers to {self.node.base_url}")
            out["catalog"] = await self.node.put_catalog(self.p.name, rows)
        return out
