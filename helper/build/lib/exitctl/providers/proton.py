"""Proton VPN adapter (PLAN Phase 3b, D17/D18).

Keys are created the way Proton's web dashboard does it (WebClients,
`containers/vpn/WireGuardConfigurationSection`): an Ed25519 key pair is generated here,
its public key is registered as a *persistent* certificate carrying the options
(`Features`), and WireGuard uses the X25519 key derived from the same seed. The
dashboard's features are the levers: they are bound to the key, not to the file.

The Proton session (SRP login + 2FA, via Proton's `python-proton-core`) is kept in the
macOS Keychain and never leaves this Mac: it grants the whole Proton account.
"""

from __future__ import annotations

import os
import time

from .. import keychain
from ..wgkeys import (Ed25519Pair, Peer, b64, render_conf, wg_public_for_client_key,
                      x25519_public)
from .base import (CREATE, DELETE, LIST, LOGIN, RENEW, SERVERS, NeedInput, NewKey, Option,
                   Prompt, Provider, ProviderError)

KEYCHAIN_NAME = "proton:session"
VERIFY_URL = "https://verify.proton.me/?methods={methods}&token={token}"
USER_AGENT = "exitctl/0.1 (macOS)"

# Option keys -> the dashboard's Features names. RandomNAT is the inverse of
# "Moderate NAT" (the dashboard's transform).
OPTIONS = [
    Option("netshield", "NetShield", "NetShield", "choice", 0,
           {0: "no filter", 1: "block malware", 2: "block malware, ads and trackers"},
           "DNS filtering by Proton's resolver (10.2.0.1)"),
    Option("accelerator", "VPN Accelerator", "VPN Accelerator", "bool", True,
           help="Proton's TCP split (Features.SplitTCP)"),
    Option("moderate_nat", "Moderate NAT", "Moderate NAT", "bool", False,
           help="stable external port mapping (Features.RandomNAT = off)"),
    Option("port_forward", "NAT-PMP (Port Forwarding)", "NAT-PMP (Port Forwarding)", "bool", False,
           help="NAT-PMP port forwarding on P2P servers"),
]


def features(values: dict) -> dict:
    return {"NetShieldLevel": int(values["netshield"]),
            "SplitTCP": bool(values["accelerator"]),
            "RandomNAT": not values["moderate_nat"],
            "PortForwarding": bool(values["port_forward"])}


def values_from_features(f: dict) -> dict:
    out = {}
    if "NetShieldLevel" in f:
        out["netshield"] = int(f["NetShieldLevel"])
    if "SplitTCP" in f:
        out["accelerator"] = bool(f["SplitTCP"])
    if "RandomNAT" in f:
        out["moderate_nat"] = not f["RandomNAT"]
    if "PortForwarding" in f:
        out["port_forward"] = bool(f["PortForwarding"])
    return out


def option_lines(values: dict) -> dict[str, str]:
    """The dashboard's `# <Option> = <value>` comments, in its order."""
    by_key = {o.key: o for o in OPTIONS}
    return {by_key[k].conf_label: by_key[k].format(values[k])
            for k in ("netshield", "moderate_nat", "port_forward", "accelerator") if k in values}


def cert_view(c: dict) -> dict:
    return {"id": c.get("ClientKeyFingerprint"), "serial": c.get("SerialNumber"),
            "device_name": c.get("DeviceName") or "", "mode": c.get("Mode"),
            "expires_at": c.get("ExpirationTime"),
            "client_public_key": c.get("ClientKey") or "",
            "wg_public_key": wg_public_for_client_key(c.get("ClientKey") or ""),
            "options": values_from_features(c.get("Features") or {})}


# vpn/v1/logicals `Features` bits (python-proton-vpn-api-core ServerFeatureEnum)
F_SECURE_CORE, F_TOR, F_P2P, F_STREAMING, F_IPV6 = 1, 2, 4, 8, 16


def catalog_rows(logicals: list[dict]) -> list[dict]:
    """One row per physical server (the WireGuard peer), deduplicated by (key, IP)."""
    rows, seen = [], set()
    for lg in logicals:
        feat = int(lg.get("Features") or 0)
        for ps in lg.get("Servers") or []:
            key, ip = ps.get("X25519PublicKey"), ps.get("EntryIP")
            if not key or not ip or (key, ip) in seen:
                continue
            seen.add((key, ip))
            rows.append({
                "name": lg.get("Name", ""), "country_code": lg.get("ExitCountry", ""),
                "city": lg.get("City") or "", "hostname": ps.get("Domain", ""), "ip": ip,
                "pubkey": key, "secure_core": bool(feat & F_SECURE_CORE),
                "tor": bool(feat & F_TOR), "port_forward": bool(feat & F_P2P),
                "stream": bool(feat & F_STREAMING), "ipv6": bool(feat & F_IPV6),
                "free": int(lg.get("Tier", 2)) == 0,
                "load": lg.get("Load"),
                "enabled": lg.get("Status") == 1 and ps.get("Status") == 1})
    return rows


class _KeychainObserver:
    """proton-core calls this around every change of session state (login, refresh,
    logout); we mirror the state into the Keychain."""

    def _acquire_session_lock(self, account_name, session_data):
        pass

    def _release_session_lock(self, account_name, new_session_data):
        if new_session_data:
            keychain.save(KEYCHAIN_NAME, new_session_data)
        else:
            keychain.delete(KEYCHAIN_NAME)


class ProtonProvider(Provider):
    name = "proton"
    title = "Proton VPN"
    capabilities = frozenset({LOGIN, CREATE, RENEW, LIST, DELETE, SERVERS})
    tunnel_address = "10.2.0.2/32"
    tunnel_dns = "10.2.0.1"

    def __init__(self, session_factory=None):
        self._factory = session_factory or self._default_session
        self._session = None

    # Session ------------------------------------------------------------------------
    @staticmethod
    def _default_session():
        from proton.session import Session
        return Session(appversion=os.environ.get("EXITCTL_PROTON_APPVERSION", "Other"),
                       user_agent=USER_AGENT)

    def session(self, fresh: bool = False):
        if self._session is None or fresh:
            s = self._factory()
            state = None if fresh else keychain.load(KEYCHAIN_NAME)
            if state:
                s.__setstate__(state)
            s.register_persistence_observer(_KeychainObserver())
            self._session = s
        return self._session

    def options(self) -> list[Option]:
        return OPTIONS

    async def whoami(self) -> dict | None:
        s = self.session()
        if not s.UID:
            return None
        return {"account": s.AccountName, "needs_2fa": bool(s.needs_twofa)}

    async def login(self, prompt: Prompt | None, creds: dict) -> dict:
        """creds: username, password, totp (optional), hv_token + hv_type (after a
        CAPTCHA). Missing input → NeedInput, unless `prompt` can ask for it."""
        from proton.session.exceptions import ProtonAPIAuthenticationNeeded, ProtonAPIError

        s = self.session()
        # Step 2 of a login that stopped at 2FA (the half-open session is in the Keychain)
        if s.UID and s.needs_twofa and not creds.get("password"):
            return await self._second_factor(s, prompt, creds)

        user = creds.get("username") or (prompt.ask("Proton username") if prompt else "")
        password = creds.get("password") or (prompt.ask("Proton password", secret=True) if prompt else "")
        if not user or not password:
            raise NeedInput("password", "username and password are needed")
        headers = None
        if creds.get("hv_token"):
            headers = {"x-pm-human-verification-token": creds["hv_token"],
                       "x-pm-human-verification-token-type": creds.get("hv_type", "captcha")}
        s = self.session(fresh=True)
        try:
            ok = await s.async_authenticate(user, password, additional_headers=headers)
        except ProtonAPIError as e:
            # During login proton-core raises CAPTCHA requests (422/9001) as a plain
            # ProtonAPIError, not ProtonAPIHumanVerificationNeeded, so match the code.
            if isinstance(e, ProtonAPIAuthenticationNeeded):
                raise ProviderError("login failed (wrong username or password?)") from None
            if e.body_code not in (9001, 12087):
                raise ProviderError(f"Proton login: {e}") from None
            if creds.get("hv_token"):
                raise ProviderError("Proton still asks for verification: the CAPTCHA was not "
                                    "completed (or expired); run login again") from None
            details = (e.json_data or {}).get("Details") or {}
            token, methods = details.get("HumanVerificationToken"), details.get("Methods") or ["captcha"]
            if not token:
                raise ProviderError(f"Proton login: {e}") from None
            info = {"token": token, "methods": methods,
                    "url": VERIFY_URL.format(methods=",".join(methods), token=token)}
            if not prompt:
                raise NeedInput("human_verification", "Proton asks for human verification", **info) from None
            prompt.info("Proton asks for a CAPTCHA. Open this URL in your browser and solve it:\n"
                        f"  {info['url']}")
            prompt.ask("Then press Enter here")
            return await self.login(prompt, {**creds, "username": user, "password": password,
                                             "hv_token": token, "hv_type": "captcha"})
        del password
        if not ok:
            raise ProviderError("login failed (wrong username or password?)")
        if s.needs_twofa:
            return await self._second_factor(s, prompt, creds)
        return {"account": s.AccountName, "logged_in": True}

    async def _second_factor(self, s, prompt: Prompt | None, creds: dict) -> dict:
        code = creds.get("totp") or (prompt.ask("2FA code") if prompt else "")
        if not code:
            raise NeedInput("totp", "a 2FA code is needed (run login again with it)")
        if not await s.async_validate_2fa_code(code.strip()):
            raise ProviderError("2FA code rejected")
        return {"account": s.AccountName, "logged_in": True}

    async def logout(self) -> None:
        s = self.session()
        if s.UID:
            try:
                await s.async_logout()
            finally:
                keychain.delete(KEYCHAIN_NAME)
        keychain.delete(KEYCHAIN_NAME)

    async def _api(self, endpoint: str, jsondata=None, method=None, params=None) -> dict:
        from proton.session.exceptions import (ProtonAPI2FANeeded,
                                               ProtonAPIAuthenticationNeeded, ProtonAPIError)
        s = self.session()
        if not s.UID:
            raise ProviderError("not logged in: run `exitctl proton login`")
        try:
            return await s.async_api_request(endpoint, jsondata=jsondata, method=method, params=params)
        except ProtonAPI2FANeeded:
            raise ProviderError("login incomplete: run `exitctl proton login` and give the 2FA code") from None
        except ProtonAPIAuthenticationNeeded:
            raise ProviderError("Proton session expired: run `exitctl proton login`") from None
        except ProtonAPIError as e:
            raise ProviderError(f"Proton API {endpoint}: {e}") from None

    # Keys ---------------------------------------------------------------------------
    async def create_key(self, device_name: str, values: dict, peer: Peer) -> NewKey:
        vals = {o.key: o.default for o in OPTIONS} | values
        pair = Ed25519Pair.generate()
        cert = await self._api("/vpn/v1/certificate", {
            "ClientPublicKey": b64(pair.public), "Mode": "persistent",
            "DeviceName": device_name, "Features": features(vals)})
        private = pair.x25519_private()
        del pair   # the Ed25519 private key is not needed again (renew uses the public key)
        conf = render_conf(private, self.tunnel_address, self.tunnel_dns, peer,
                           title=device_name, option_lines=option_lines(vals))
        meta = {"provider": self.name, "cert_id": cert.get("ClientKeyFingerprint"),
                "device_name": cert.get("DeviceName") or device_name,
                "created_at": int(time.time()), "expires_at": cert.get("ExpirationTime"),
                "options": {k: vals[k] for k in vals},
                "client_public_key": cert.get("ClientKey") or "",
                "wg_public_key": x25519_public(private)}
        return NewKey(conf, meta)

    async def renew_key(self, meta: dict) -> dict:
        pub = meta.get("client_public_key")
        if not pub:
            raise ProviderError("this key has no recorded Proton public key; rotate it instead")
        vals = {o.key: o.default for o in OPTIONS} | (meta.get("options") or {})
        cert = await self._api("/vpn/v1/certificate", {
            "ClientPublicKey": pub, "Mode": "persistent", "DeviceName": meta.get("device_name", ""),
            "Features": features(vals), "Renew": True})
        return {**meta, "expires_at": cert.get("ExpirationTime"),
                "cert_id": cert.get("ClientKeyFingerprint") or meta.get("cert_id")}

    async def list_keys(self) -> list[dict]:
        out, offset = [], 0
        while True:
            r = await self._api("/vpn/v1/certificate/all", method="get",
                                params={"Mode": "persistent", "Offset": offset, "Limit": 50})
            page = r.get("Certificates") or []
            out += [cert_view(c) for c in page]
            offset += len(page)
            if len(page) < 50 or offset >= int(r.get("Total", offset)):
                return out

    async def delete_key(self, key_id: str) -> None:
        r = await self._api("/vpn/v1/certificate", {"ClientPublicKeyFingerprint": key_id},
                            method="delete")
        if not r.get("Count"):
            raise ProviderError(f"no certificate {key_id} was deleted")

    async def servers(self) -> list[dict]:
        r = await self._api("/vpn/v1/logicals?SecureCoreFilter=all&WithState=true")
        return catalog_rows(r.get("LogicalServers") or [])
