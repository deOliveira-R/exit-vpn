"""VPN server catalog: gluetun-servers format (same schema for all providers, PLAN D12/D16)."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .state import atomic_write

MIN_SERVERS = 50  # a smaller list means a broken or truncated download


class CatalogError(ValueError):
    pass


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
    port_forward: bool = False

    def endpoint(self, port: int) -> str:
        return f"{self.ip}:{port}"

    def to_dict(self) -> dict:
        return asdict(self)


def parse_gluetun(data: dict, hostname_suffix: str = "") -> list[Server]:
    """WireGuard servers from a gluetun-servers provider file."""
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        raise CatalogError("not a gluetun-servers file (no 'servers' list)")
    servers: list[Server] = []
    for s in data["servers"]:
        if s.get("vpn") != "wireguard" or not s.get("wgpubkey"):
            continue
        ipv4 = [ip for ip in s.get("ips", []) if _is_ipv4(ip)]
        if not ipv4:
            continue
        hostname = s.get("hostname", "")
        if hostname_suffix and not hostname.endswith(hostname_suffix):
            raise CatalogError(f"unexpected hostname {hostname!r} (want *{hostname_suffix})")
        servers.append(
            Server(
                name=s.get("server_name") or hostname,
                country=s.get("country", ""),
                city=s.get("city", ""),
                hostname=hostname,
                ip=ipv4[0],
                pubkey=s["wgpubkey"],
                secure_core=bool(s.get("secure_core")),
                tor=bool(s.get("tor")),
                free=bool(s.get("free")),
                stream=bool(s.get("stream")),
                port_forward=bool(s.get("port_forward")),
            )
        )
    if len(servers) < MIN_SERVERS:
        raise CatalogError(f"only {len(servers)} WireGuard servers (want >= {MIN_SERVERS})")
    return servers


def _is_ipv4(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


class Catalog:
    def __init__(self, servers: list[Server], source: str = "", fetched_at: float = 0.0):
        self.servers = servers
        self.source = source
        self.fetched_at = fetched_at
        self._by_pubkey = {s.pubkey: s for s in servers}
        self._by_name: dict[str, Server] = {}
        for s in servers:  # several physical servers can share a logical name
            self._by_name.setdefault(s.name.lower(), s)

    def __len__(self) -> int:
        return len(self.servers)

    def by_pubkey(self, pubkey: str) -> Server | None:
        return self._by_pubkey.get(pubkey)

    def by_name(self, name: str) -> list[Server]:
        n = name.lower()
        return [s for s in self.servers if s.name.lower() == n or s.hostname.lower() == n]

    def countries(self) -> list[str]:
        return sorted({s.country for s in self.servers})

    def filter(self, country: str | None = None, city: str | None = None,
               feature: str | None = None) -> list[Server]:
        out = self.servers
        if country:
            out = [s for s in out if s.country.lower() == country.lower()]
        if city:
            out = [s for s in out if s.city.lower() == city.lower()]
        if feature:
            out = [s for s in out if getattr(s, feature, False) is True]
        return out

    # Persistence: the last good catalog survives restarts and upstream outages
    def save(self, path: Path) -> None:
        atomic_write(path, json.dumps({
            "source": self.source, "fetched_at": self.fetched_at,
            "servers": [s.to_dict() for s in self.servers],
        }))

    @classmethod
    def load(cls, path: Path) -> Catalog:
        d = json.loads(path.read_text())
        servers = [Server(**s) for s in d["servers"]]
        if len(servers) < MIN_SERVERS:
            raise CatalogError(f"cached catalog has only {len(servers)} servers")
        return cls(servers, d.get("source", ""), d.get("fetched_at", 0.0))
