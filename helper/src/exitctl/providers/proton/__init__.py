"""Proton VPN adapter (PLAN Phase 3b, D17/D18).

Keys are created the way Proton's web dashboard does it (WebClients,
`containers/vpn/WireGuardConfigurationSection`): an Ed25519 key pair is generated here, its
public key is registered as a *persistent* certificate carrying the levers (`Features`), and
WireGuard uses the X25519 key derived from the same seed (`crypto`).

The session (`session.ProtonSession`) is kept in the macOS Keychain and never leaves this Mac:
it grants the whole Proton account.
"""

from __future__ import annotations

import time

from ...model import Account, Certificate, KeyMeta, Lever, NewKey, ProviderError
from ...sessions import KeychainStore, SessionStore
from ...wgkeys import Peer, b64, render_conf, x25519_public
from ..base import CREATE, DELETE, LIST, LOGIN, RENEW, SERVERS, Credentials, Provider
from .catalog import catalog_rows
from .crypto import Ed25519Pair, wg_public_for_client_key
from .levers import LEVERS, features, option_lines, values_from_features
from .session import ProtonSession, default_session

__all__ = ["ProtonProvider", "LEVERS", "features", "catalog_rows"]


def certificate(c: dict) -> Certificate:
    return Certificate(
        id=c.get("ClientKeyFingerprint") or "", device_name=c.get("DeviceName") or "",
        expires_at=c.get("ExpirationTime"), options=values_from_features(c.get("Features") or {}),
        wg_public_key=wg_public_for_client_key(c.get("ClientKey") or ""),
        extra={"serial": c.get("SerialNumber"), "mode": c.get("Mode"),
               "client_public_key": c.get("ClientKey") or ""})


class ProtonProvider(Provider):
    name = "proton"
    title = "Proton VPN"
    capabilities = frozenset({LOGIN, CREATE, RENEW, LIST, DELETE, SERVERS})
    tunnel_address = "10.2.0.2/32"
    tunnel_dns = "10.2.0.1"

    def __init__(self, store: SessionStore | None = None, session_factory=default_session):
        self.session = ProtonSession(store or KeychainStore(), session_factory)

    def levers(self) -> list[Lever]:
        return LEVERS

    async def account(self) -> Account:
        return self.session.account()

    async def login(self, creds: Credentials) -> Account:
        return await self.session.login(creds)

    async def logout(self) -> None:
        await self.session.logout()

    async def create_key(self, device_name: str, values: dict, peer: Peer) -> NewKey:
        vals = {lv.key: lv.default for lv in LEVERS} | values
        pair = Ed25519Pair.generate()
        cert = await self.session.api("/vpn/v1/certificate", {
            "ClientPublicKey": b64(pair.public), "Mode": "persistent",
            "DeviceName": device_name, "Features": features(vals)})
        private = pair.x25519_private()
        del pair   # the Ed25519 private key is not needed again (renew uses the public key)
        conf = render_conf(private, self.tunnel_address, self.tunnel_dns, peer,
                           title=device_name, option_lines=option_lines(vals))
        meta = KeyMeta(provider=self.name, cert_id=cert.get("ClientKeyFingerprint"),
                       device_name=cert.get("DeviceName") or device_name,
                       created_at=int(time.time()), expires_at=cert.get("ExpirationTime"),
                       options=dict(vals), client_public_key=cert.get("ClientKey") or "",
                       wg_public_key=x25519_public(private))
        return NewKey(conf, meta)

    def renewal_meta(self, cert: Certificate) -> KeyMeta:
        return KeyMeta(provider=self.name, cert_id=cert.id, device_name=cert.device_name,
                       options=dict(cert.options),
                       client_public_key=cert.extra.get("client_public_key"))

    async def renew_key(self, meta: KeyMeta) -> KeyMeta:
        if not meta.client_public_key:
            raise ProviderError("this key has no recorded Proton public key; rotate it instead")
        vals = {lv.key: lv.default for lv in LEVERS} | meta.options
        cert = await self.session.api("/vpn/v1/certificate", {
            "ClientPublicKey": meta.client_public_key, "Mode": "persistent",
            "DeviceName": meta.device_name, "Features": features(vals), "Renew": True})
        meta.expires_at = cert.get("ExpirationTime")
        meta.cert_id = cert.get("ClientKeyFingerprint") or meta.cert_id
        return meta

    async def certificates(self) -> list[Certificate]:
        out, offset = [], 0
        while True:
            r = await self.session.api("/vpn/v1/certificate/all", method="get",
                                       params={"Mode": "persistent", "Offset": offset, "Limit": 50})
            page = r.get("Certificates") or []
            out += [certificate(c) for c in page]
            offset += len(page)
            if len(page) < 50 or offset >= int(r.get("Total", offset)):
                return out

    async def delete_certificate(self, cert_id: str) -> None:
        r = await self.session.api("/vpn/v1/certificate", {"ClientPublicKeyFingerprint": cert_id},
                                   method="delete")
        if not r.get("Count"):
            raise ProviderError(f"no certificate {cert_id} was deleted")

    async def servers(self) -> list[dict]:
        r = await self.session.api("/vpn/v1/logicals?SecureCoreFilter=all&WithState=true")
        return catalog_rows(r.get("LogicalServers") or [])
