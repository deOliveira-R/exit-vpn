"""What the controller keeps between cycles and restarts (pure; saving is `persistence`):
the active and standby servers, the preferences and the blacklist."""

from __future__ import annotations

from dataclasses import dataclass, field

from .prefs import Prefs
from .servers import Server


@dataclass(frozen=True)
class TunnelServer:
    """The server a tunnel is talking to. `profile`: the features failover keeps from a manual
    pick (P2P stays P2P). A peer the catalog does not know keeps a descriptive name."""
    name: str
    pubkey: str
    endpoint: str          # ip:port as configured on the tunnel
    country: str = ""
    city: str = ""
    since: float = 0.0     # kept across restarts while the peer is the same
    profile: tuple[str, ...] = ()

    @property
    def ip(self) -> str:
        return self.endpoint.rsplit(":", 1)[0]

    @classmethod
    def of(cls, srv: Server, port: int, since: float, profile=()) -> TunnelServer:
        return cls(srv.name, srv.pubkey, srv.endpoint(port), srv.country, srv.city, since,
                   tuple(profile))

    def to_dict(self) -> dict:
        return {"name": self.name, "pubkey": self.pubkey, "endpoint": self.endpoint,
                "country": self.country, "city": self.city, "since": self.since,
                "profile": list(self.profile)}

    @classmethod
    def from_dict(cls, d: dict) -> TunnelServer:
        return cls(d["name"], d["pubkey"], d["endpoint"], d.get("country", ""), d.get("city", ""),
                   d.get("since", 0.0), tuple(d.get("profile", ())))


class Blacklist:
    """Servers that recently failed, avoided until their entry expires. Keyed by public
    key, not (key, IP): a server that failed is avoided under all its entry IPs, which for
    Secure Core also means the entries sharing an exit's key (they share its fate)."""

    def __init__(self, entries: dict[str, float] | None = None):
        self._until: dict[str, float] = dict(entries or {})

    def add(self, pubkey: str, until: float) -> None:
        self._until[pubkey] = until

    def contains(self, pubkey: str, now: float) -> bool:
        return self._until.get(pubkey, 0.0) > now

    def active(self, now: float) -> set[str]:
        return {k for k, v in self._until.items() if v > now}

    def prune(self, now: float) -> None:
        self._until = {k: v for k, v in self._until.items() if v > now}

    def to_dict(self) -> dict[str, float]:
        return dict(self._until)


@dataclass
class State:
    prefs: Prefs
    active: TunnelServer | None = None    # the active tunnel's server: the exit
    blacklist: Blacklist = field(default_factory=Blacklist)
    standby: TunnelServer | None = None   # the standby tunnel's server, ready for a handover
