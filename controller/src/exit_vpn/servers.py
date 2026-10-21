"""VPN servers and their vocabulary (pure): what a server is, what it offers, where it is.

A server is identified by (public key, entry IP), never by its key alone: Secure Core
entries reuse their exit server's key with a different entry IP (PLAN Phase 3b.2). Its
name (`CH#821`) is logical: several physical servers can share one.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# What a server can offer that a pick keeps through failover (PLAN Phase 5). P2P is hard:
# traffic may be peer-to-peer, so a P2P server never fails over to one without it.
# Streaming and IPv6 are soft: preferred, then relaxed.
FEATURES = ("port_forward", "stream", "ipv6")
HARD_FEATURES = ("port_forward",)
# Flags a server listing can be filtered by
FLAGS = ("secure_core", "tor", "free", "stream", "port_forward", "ipv6")

_SC_NAME = re.compile(r"^([A-Z]{2})-[A-Z]{2}#")


@dataclass(frozen=True)
class Server:
    name: str
    country: str
    city: str
    hostname: str
    ip: str
    pubkey: str
    secure_core: bool = False
    tor: bool = False
    free: bool = False
    stream: bool = False
    port_forward: bool = False  # Proton: P2P
    ipv6: bool = False
    load: int | None = None     # percent, from a live catalog (None: unknown)
    enabled: bool = True        # False: in maintenance (live catalog)

    @property
    def id(self) -> tuple[str, str]:
        return (self.pubkey, self.ip)

    def endpoint(self, port: int) -> str:
        return f"{self.ip}:{port}"

    @property
    def entry(self) -> str:
        """Secure Core entry country code ("CH" for CH-US#3), else ""."""
        m = _SC_NAME.match(self.name) if self.secure_core else None
        return m.group(1) if m else ""

    def features(self) -> list[str]:
        return [f for f in FEATURES if getattr(self, f)]

    def has(self, flag: str) -> bool:
        return getattr(self, flag, False) is True

    def to_dict(self) -> dict:
        return {**asdict(self), "entry": self.entry}


@dataclass(frozen=True)
class Region:
    """A pinned region: a country, or one city in it."""
    country: str
    city: str | None = None

    def contains(self, s: Server, secure_core: bool = False) -> bool:
        """Secure Core servers are few per country: a city pin narrows only regular ones."""
        if s.country.lower() != self.country.lower():
            return False
        return not self.city or secure_core or s.city.lower() == self.city.lower()

    def to_dict(self) -> dict:
        return {"country": self.country, "city": self.city}

    @classmethod
    def from_dict(cls, d: dict) -> Region:
        return cls(d["country"], d.get("city") or None)


def common_features(servers: list[Server]) -> list[str]:
    """The profile of a pick that may land on any of `servers` (one logical name)."""
    return [f for f in FEATURES if servers and all(getattr(s, f) for s in servers)]


def by_load(servers: list[Server]) -> list[Server]:
    return sorted(servers, key=lambda s: 50 if s.load is None else s.load)
