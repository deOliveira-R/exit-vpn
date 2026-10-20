"""The provider adapter interface (PLAN D20).

An adapter knows one VPN service: its levers (options bound to a key), how to log in,
and how to create, renew, list and delete keys. The CLI and, later, the menu-bar app
are generic over adapters; nothing outside `providers/` knows a provider's API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..wgkeys import Peer

# Capabilities an adapter may declare
LOGIN, CREATE, RENEW, LIST, DELETE, IMPORT = "login", "create", "renew", "list", "delete", "import"


class ProviderError(RuntimeError):
    pass


class NeedInput(ProviderError):
    """Login needs something only the user can give (a 2FA code, a CAPTCHA). `info`
    says what, so a non-interactive caller (the app) can ask and call again."""

    def __init__(self, what: str, message: str, **info):
        super().__init__(message)
        self.what, self.info = what, info


_TRUE, _FALSE = {"on", "true", "yes", "1"}, {"off", "false", "no", "0"}


@dataclass(frozen=True)
class Option:
    key: str                  # CLI flag / JSON key, e.g. "netshield"
    label: str                # human name
    conf_label: str           # the option comment in the .conf the exit node reports
    kind: str                 # "bool" | "choice"
    default: bool | int
    choices: dict[int, str] = field(default_factory=dict)   # for "choice": value -> meaning
    help: str = ""

    def parse(self, text: str | bool | int) -> bool | int:
        if self.kind == "bool":
            if isinstance(text, bool):
                return text
            t = str(text).strip().lower()
            if t in _TRUE or t in _FALSE:
                return t in _TRUE
            raise ProviderError(f"{self.key}: expected on/off, got {text!r}")
        try:
            v = int(text)
        except (TypeError, ValueError):
            raise ProviderError(f"{self.key}: expected one of {sorted(self.choices)}") from None
        if v not in self.choices:
            raise ProviderError(f"{self.key}: expected one of {sorted(self.choices)}")
        return v

    def format(self, value: bool | int) -> str:
        return ("on" if value else "off") if self.kind == "bool" else str(value)

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind,
                "default": self.default, "choices": {str(k): v for k, v in self.choices.items()},
                "help": self.help}


@dataclass(frozen=True)
class NewKey:
    conf: str          # the full wg-quick config, including the private key
    meta: dict         # public facts for the controller (cert id, expiry, options)

    def __repr__(self) -> str:
        return f"NewKey(meta={self.meta})"


class Prompt(Protocol):
    def ask(self, label: str, secret: bool = False) -> str: ...
    def info(self, message: str) -> None: ...


class Provider:
    name = ""
    title = ""
    capabilities: frozenset[str] = frozenset()
    tunnel_address = ""        # D16: the address/DNS the provider assigns inside the tunnel
    tunnel_dns = ""

    def options(self) -> list[Option]:
        return []

    def option(self, key: str) -> Option:
        for o in self.options():
            if o.key == key:
                return o
        raise ProviderError(f"{self.name} has no option {key!r}")

    def values_from_conf_options(self, conf_options: dict[str, str]) -> dict:
        """The exit node reports a key's options as its .conf comments; map them back."""
        out = {}
        for o in self.options():
            if o.conf_label in conf_options:
                try:
                    out[o.key] = o.parse(conf_options[o.conf_label])
                except ProviderError:
                    pass
        return out

    async def whoami(self) -> dict | None:
        return None

    async def login(self, prompt: Prompt | None, creds: dict) -> dict:
        raise ProviderError(f"{self.name} has no login")

    async def logout(self) -> None:
        return None

    async def create_key(self, device_name: str, values: dict, peer: Peer) -> NewKey:
        raise ProviderError(f"{self.name} cannot create keys")

    async def renew_key(self, meta: dict) -> dict:
        raise ProviderError(f"{self.name} cannot renew keys")

    async def list_keys(self) -> list[dict]:
        raise ProviderError(f"{self.name} cannot list keys")

    async def delete_key(self, key_id: str) -> None:
        raise ProviderError(f"{self.name} cannot delete keys")
