"""The provider adapter interface.

An adapter knows one VPN service: its levers (options bound to a key), how to log in, and how
to create, renew, list and delete keys, and optionally its live server list. Everything else
(`keyservice`, `cli`, the app) is generic over adapters; nothing outside `providers/` knows a
provider's API. `capabilities` says which of the methods below an adapter implements; the
CLI offers only those commands.

Adding a provider: a module (or package) here with a `Provider` subclass, registered in
`providers/__init__.py`. Its keys must be standard wg-quick configs whose Address/DNS match
the exit node's (`tunnel_address`, `tunnel_dns`); `create_key` returns the `KeyMeta` the
controller records; `servers()` returns rows in the controller's push schema (`ServerRow`).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import Account, Certificate, ExitctlError, KeyMeta, Lever, NewKey, ProviderError
from ..wgkeys import Peer

# Capabilities an adapter may declare
LOGIN, CREATE, RENEW, LIST, DELETE, IMPORT = "login", "create", "renew", "list", "delete", "import"
SERVERS = "servers"   # a live server catalog

# A pushed server row (the controller's PUT /v1/catalog): name, country_code, city, hostname,
# ip, pubkey, secure_core, tor, free, stream, port_forward, ipv6, load (0-100 or None), enabled
ServerRow = dict


@dataclass(frozen=True)
class Credentials:
    """What a login step may carry. A step that lacks something raises NeedInput."""
    username: str = ""
    password: str = ""
    totp: str = ""
    hv_token: str = ""          # after human verification (a CAPTCHA solved in the browser)
    hv_type: str = "captcha"

    def __repr__(self) -> str:   # never print the password
        return f"Credentials(username={self.username!r})"

    @classmethod
    def from_json(cls, d: dict) -> Credentials:
        return cls(**{k: str(d[k]) for k in ("username", "password", "totp", "hv_token", "hv_type")
                      if d.get(k)})


class Provider:
    name = ""
    title = ""
    capabilities: frozenset[str] = frozenset()
    tunnel_address = ""
    tunnel_dns = ""

    def levers(self) -> list[Lever]:
        return []

    def lever(self, key: str) -> Lever:
        for lv in self.levers():
            if lv.key == key:
                return lv
        raise ExitctlError(f"{self.name} has no option {key!r}")

    def _unsupported(self, what: str):
        return ProviderError(f"{self.name} cannot {what}")

    async def account(self) -> Account:
        raise self._unsupported("log in")

    async def login(self, creds: Credentials) -> Account:
        """One login step; raises NeedInput when the user has to give more."""
        raise self._unsupported("log in")

    async def logout(self) -> None:
        raise self._unsupported("log in")

    async def create_key(self, device_name: str, values: dict, peer: Peer) -> NewKey:
        raise self._unsupported("create keys")

    async def renew_key(self, meta: KeyMeta) -> KeyMeta:
        raise self._unsupported("renew keys")

    def renewal_meta(self, cert: Certificate) -> KeyMeta:
        """The facts needed to renew a key this provider made elsewhere (its dashboard)."""
        raise self._unsupported("renew keys made elsewhere")

    async def certificates(self) -> list[Certificate]:
        raise self._unsupported("list keys")

    async def delete_certificate(self, cert_id: str) -> None:
        raise self._unsupported("delete keys")

    async def servers(self) -> list[ServerRow]:
        raise self._unsupported("list its servers")
