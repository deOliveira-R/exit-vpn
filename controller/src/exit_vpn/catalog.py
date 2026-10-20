"""VPN server catalog: gluetun-servers format (same schema for all providers, PLAN D12/D16),
or a list pushed from the provider's own API by exitctl (PLAN Phase 3b.2)."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .state import FEATURES, atomic_write

MIN_SERVERS = 50  # a smaller list means a broken or truncated download
MIN_PUSH_FRACTION = 0.3   # a push may not shrink the catalog below this share
PUSH_PREFIX = "push:"
_CODE = re.compile(r"^(?:node-)?([a-z]{2})-")
_SC_NAME = re.compile(r"^([A-Z]{2})-[A-Z]{2}#")


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
    port_forward: bool = False  # Proton: P2P
    ipv6: bool = False
    load: int | None = None     # percent, from a live catalog (None: unknown)
    enabled: bool = True        # False: in maintenance (live catalog)

    def endpoint(self, port: int) -> str:
        return f"{self.ip}:{port}"

    @property
    def entry(self) -> str:
        """Secure Core entry country code ("CH" for CH-US#3), else ""."""
        m = _SC_NAME.match(self.name) if self.secure_core else None
        return m.group(1) if m else ""

    def features(self) -> list[str]:
        return [f for f in FEATURES if getattr(self, f)]

    def to_dict(self) -> dict:
        return {**asdict(self), "entry": self.entry}


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


def parse_pushed(rows: list[dict], hostname_suffix: str,
                 country_for_code, current_size: int = 0) -> list[Server]:
    """A provider-neutral list from exitctl. It replaces the list wg0's peers come from,
    so it is checked at least as strictly as a download."""
    if len(rows) < max(MIN_SERVERS, int(current_size * MIN_PUSH_FRACTION)):
        raise CatalogError(f"only {len(rows)} servers (current catalog has {current_size})")
    servers = []
    for r in rows:
        host = r.get("hostname", "")
        if hostname_suffix and not host.endswith(hostname_suffix):
            raise CatalogError(f"unexpected hostname {host!r} (want *{hostname_suffix})")
        try:
            ip = ipaddress.ip_address(r.get("ip", ""))
        except ValueError:
            raise CatalogError(f"{host}: bad IP {r.get('ip')!r}") from None
        if not (isinstance(ip, ipaddress.IPv4Address) and ip.is_global):
            raise CatalogError(f"{host}: {ip} is not a public IPv4 address")
        try:
            if len(base64.b64decode(r.get("pubkey", ""), validate=True)) != 32:
                raise ValueError
        except (binascii.Error, ValueError):
            raise CatalogError(f"{host}: bad WireGuard public key") from None
        code = str(r.get("country_code", "")).upper()
        load = r.get("load")
        servers.append(Server(
            name=str(r.get("name") or host), country=country_for_code(code) or code,
            city=str(r.get("city") or ""), hostname=host, ip=str(ip), pubkey=r["pubkey"],
            secure_core=bool(r.get("secure_core")), tor=bool(r.get("tor")),
            free=bool(r.get("free")), stream=bool(r.get("stream")),
            port_forward=bool(r.get("port_forward")), ipv6=bool(r.get("ipv6")),
            load=int(load) if isinstance(load, (int, float)) else None,
            enabled=bool(r.get("enabled", True))))
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
        # A public key is NOT unique: Secure Core entries reuse their exit server's key
        # with a different (entry) IP. A server's identity is (pubkey, endpoint IP).
        self._by_peer = {(s.pubkey, s.ip): s for s in servers}
        self._by_name: dict[str, Server] = {}
        for s in servers:  # several physical servers can share a logical name
            self._by_name.setdefault(s.name.lower(), s)
        self._codes: dict[str, str] | None = None

    def __len__(self) -> int:
        return len(self.servers)

    @property
    def pushed(self) -> bool:
        return self.source.startswith(PUSH_PREFIX)

    def by_peer(self, pubkey: str, ip: str) -> Server | None:
        return self._by_peer.get((pubkey, ip))

    def by_name(self, name: str) -> list[Server]:
        n = name.lower()
        return [s for s in self.servers if s.name.lower() == n or s.hostname.lower() == n]

    def country_for_code(self, code: str) -> str | None:
        """Catalog country name for an ISO code (e.g. Cloudflare's loc=US), voted from
        hostnames like us-ca-01 / node-ch-03 (Proton's scheme; None if unknown)."""
        if self._codes is None:
            votes: dict[str, Counter] = {}
            for srv in self.servers:
                m = _CODE.match(srv.hostname)
                if m and not srv.secure_core:
                    votes.setdefault(m.group(1), Counter())[srv.country] += 1
            self._codes = {k: v.most_common(1)[0][0] for k, v in votes.items()}
        return self._codes.get(code.lower())

    def code_for_country(self, country: str) -> str:
        """Inverse of country_for_code ("" if unknown)."""
        self.country_for_code("")  # build the table
        for code, name in (self._codes or {}).items():
            if name.lower() == country.lower():
                return code.upper()
        return ""

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
        fields = Server.__dataclass_fields__
        servers = [Server(**{k: v for k, v in s.items() if k in fields}) for s in d["servers"]]
        if len(servers) < MIN_SERVERS:
            raise CatalogError(f"cached catalog has only {len(servers)} servers")
        return cls(servers, d.get("source", ""), d.get("fetched_at", 0.0))
