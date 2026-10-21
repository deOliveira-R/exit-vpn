"""The server catalog (pure): every server the exit may use, with lookups by identity, name
and place. It comes from gluetun-servers (same schema for all providers) or
from a list exitctl pushes from the provider's own API; both are validated as
strictly, because the catalog decides where exit traffic goes. Caching on disk is
`persistence.CatalogCache`; where a catalog comes from is `catalog_source`."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from collections import Counter

from .countries import ISO_NAMES
from .errors import PolicyError
from .servers import Region, Server

MIN_SERVERS = 50  # a smaller list means a broken or truncated download
MIN_PUSH_FRACTION = 0.3   # a push may not shrink the catalog below this share
PUSH_PREFIX = "push:"
_CODE = re.compile(r"^(?:node-)?([a-z]{2})-")


class CatalogError(ValueError):
    """A downloaded or pushed list that must not replace the current one."""


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
        self._by_id = {s.id: s for s in servers}
        self._codes: dict[str, str] | None = None

    def __len__(self) -> int:
        return len(self.servers)

    @property
    def pushed(self) -> bool:
        return self.source.startswith(PUSH_PREFIX)

    def by_id(self, pubkey: str, ip: str) -> Server | None:
        return self._by_id.get((pubkey, ip))

    def by_name(self, name: str) -> list[Server]:
        """Every physical server with this logical name (or hostname)."""
        n = name.lower()
        return [s for s in self.servers if s.name.lower() == n or s.hostname.lower() == n]

    def canonical_name(self, name: str) -> str:
        found = self.by_name(name)
        if not found:
            raise PolicyError(f"unknown server: {name!r}")
        return found[0].name

    def canonical_region(self, country: str, city: str | None = None) -> Region:
        """The catalog's spelling of a region; unknown places are refused."""
        countries = {c.lower(): c for c in self.countries()}
        name = countries.get(country.lower())
        if not name:
            raise PolicyError(f"unknown country: {country!r}")
        if not city:
            return Region(name)
        cities = {s.city.lower(): s.city for s in self.filter(name)}
        if city.lower() not in cities:
            raise PolicyError(f"no servers in {city!r}, {name}")
        return Region(name, cities[city.lower()])

    def country_for_code(self, code: str) -> str | None:
        """Catalog country name for an ISO code (e.g. Cloudflare's loc=US), voted from
        hostnames like us-ca-01 / node-ch-03 (Proton's scheme), else the ISO name; None if
        unknown. A "name" that is just the code (an older push) does not count."""
        if self._codes is None:
            votes: dict[str, Counter] = {}
            for srv in self.servers:
                m = _CODE.match(srv.hostname)
                if m and not srv.secure_core and srv.country.lower() != m.group(1):
                    votes.setdefault(m.group(1), Counter())[srv.country] += 1
            self._codes = {k: v.most_common(1)[0][0] for k, v in votes.items()}
        return self._codes.get(code.lower()) or ISO_NAMES.get(code.upper())

    def code_for_country(self, country: str) -> str:
        """Inverse of country_for_code ("" if unknown)."""
        self.country_for_code("")  # build the table
        for code, name in (self._codes or {}).items():
            if name.lower() == country.lower():
                return code.upper()
        return next((c for c, n in ISO_NAMES.items() if n.lower() == country.lower()), "")

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
            out = [s for s in out if s.has(feature)]
        return out

    def view(self) -> dict:
        return {"servers": len(self), "source": self.source, "pushed": self.pushed,
                "fetched_at": self.fetched_at, "enabled": sum(1 for s in self.servers if s.enabled)}
