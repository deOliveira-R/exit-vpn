"""What exitctl works with, provider-neutral (PLAN D20): levers, certificates, key facts, the
account, and the errors. The JSON each object produces is the contract with the app
(contracts/exitctl/); change it only together with the app."""

from __future__ import annotations

from dataclasses import dataclass, field

SLOTS = ("a", "b")   # a: the exit tunnel, b: the prober's tunnel


# Errors, by what the caller can do about them (cli maps them to exit codes) ------------
class ExitctlError(RuntimeError):
    """Something went wrong; the message says what (exit code 1)."""


class ProviderError(ExitctlError):
    """The VPN provider refused or failed (not logged in, a rejected code, an API error)."""


class NeedInput(ProviderError):
    """Login needs something only the user can give; `kind` says what (exit code 3):
    "password", "totp" (a 2FA code) or "human_verification" (`info`: url, token, methods).
    The caller asks, then runs login again with it."""

    def __init__(self, kind: str, message: str, **info):
        super().__init__(message)
        self.kind, self.info = kind, info

    def to_json(self) -> dict:
        return {"error": str(self), "need": self.kind, **self.info}


class NodeError(ExitctlError):
    """The exit node's controller answered with an error, or not at all (`status` 0)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"controller: HTTP {status}: {detail}")
        self.status, self.detail = status, detail

    @property
    def refused(self) -> bool:
        """409: the node tried the key or change and kept what it had (exit code 2)."""
        return self.status == 409


# Levers ------------------------------------------------------------------------------
_TRUE, _FALSE = {"on", "true", "yes", "1"}, {"off", "false", "no", "0"}


@dataclass(frozen=True)
class Lever:
    """An option bound to a key (D17), e.g. Proton's NetShield: changing it means a new key."""
    key: str                  # CLI flag / JSON key, e.g. "netshield"
    label: str                # human name
    conf_label: str           # how the .conf comments name it (what the exit node reports)
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
            raise ExitctlError(f"{self.key}: expected on/off, got {text!r}")
        try:
            v = int(text)
        except (TypeError, ValueError):
            raise ExitctlError(f"{self.key}: expected one of {sorted(self.choices)}") from None
        if v not in self.choices:
            raise ExitctlError(f"{self.key}: expected one of {sorted(self.choices)}")
        return v

    def format(self, value: bool | int) -> str:
        return ("on" if value else "off") if self.kind == "bool" else str(value)

    def to_json(self) -> dict:
        return {"key": self.key, "label": self.label, "conf_label": self.conf_label, "kind": self.kind,
                "default": self.default, "choices": {str(k): v for k, v in self.choices.items()},
                "help": self.help}


def resolve_levers(levers: list[Lever], recorded: dict | None = None,
                   conf_options: dict[str, str] | None = None, overrides: dict | None = None) -> dict:
    """A key's lever values: the defaults, then what its .conf comments say (a key made
    elsewhere, e.g. the provider's dashboard), then what was recorded when exitctl made it,
    then explicit overrides."""
    vals = {lv.key: lv.default for lv in levers}
    for lv in levers:
        if conf_options and lv.conf_label in conf_options:
            try:
                vals[lv.key] = lv.parse(conf_options[lv.conf_label])
            except ExitctlError:
                pass
    return vals | (recorded or {}) | (overrides or {})


# Keys and certificates ----------------------------------------------------------------
@dataclass
class KeyMeta:
    """Public facts about a key, recorded on the exit node (the controller's `KeyMeta`).
    `client_public_key`: what the provider needs to renew it (Proton: the Ed25519 key)."""
    provider: str
    device_name: str
    cert_id: str | None = None
    created_at: int | None = None
    expires_at: int | None = None
    options: dict = field(default_factory=dict)
    client_public_key: str | None = None
    wg_public_key: str | None = None

    def to_json(self) -> dict:
        return {k: v for k, v in {
            "provider": self.provider, "cert_id": self.cert_id, "device_name": self.device_name,
            "created_at": self.created_at, "expires_at": self.expires_at, "options": self.options,
            "client_public_key": self.client_public_key, "wg_public_key": self.wg_public_key,
        }.items() if v is not None}

    @classmethod
    def from_json(cls, d: dict) -> KeyMeta:
        return cls(provider=d.get("provider", ""), device_name=d.get("device_name", ""),
                   cert_id=d.get("cert_id"), created_at=d.get("created_at"),
                   expires_at=d.get("expires_at"), options=dict(d.get("options") or {}),
                   client_public_key=d.get("client_public_key"), wg_public_key=d.get("wg_public_key"))


@dataclass(frozen=True)
class NewKey:
    conf: str          # the whole wg-quick config, private key included: never printed
    meta: KeyMeta

    def __repr__(self) -> str:
        return f"NewKey(meta={self.meta})"


@dataclass
class Certificate:
    """A key registered on the provider account. `wg_public_key` ties it to a slot;
    `extra`: provider fields shown as they are (Proton: serial, mode, client_public_key)."""
    id: str
    device_name: str
    expires_at: int | None
    options: dict
    wg_public_key: str
    extra: dict = field(default_factory=dict)

    def to_json(self, slot: str | None = None, orphan: bool = False) -> dict:
        """`slot`: the exit node's slot using it; `orphan`: an exit-node key no slot uses."""
        return {"id": self.id, "device_name": self.device_name, "expires_at": self.expires_at,
                **self.extra, "wg_public_key": self.wg_public_key, "options": self.options,
                "slot": slot, "orphan": orphan}


# The account ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Account:
    """The provider login. `needs_2fa`: the password step passed, the 2FA code is pending
    (the half-open session waits in the Keychain for the next `login --totp`)."""
    name: str | None
    needs_2fa: bool = False

    @property
    def logged_in(self) -> bool:
        return bool(self.name) and not self.needs_2fa

    def whoami_json(self) -> dict:
        if not self.name:
            return {"logged_in": False}
        return {"account": self.name, "needs_2fa": self.needs_2fa}

    def login_json(self) -> dict:
        return {"account": self.name, "logged_in": True}


LOGGED_OUT = Account(None)
