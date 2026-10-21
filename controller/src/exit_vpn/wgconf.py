"""The non-secret parts of a wg-quick config. The PrivateKey is never read here."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

OPTION_COMMENT = re.compile(r"^#\s*(NetShield|Moderate NAT|NAT-PMP \(Port Forwarding\)|"
                            r"VPN Accelerator|Bouncing)\s*=\s*(.+)$")


@dataclass(frozen=True)
class WgConf:
    address: str        # IPv4 address without prefix
    dns: str            # first IPv4 DNS server
    peer_pubkey: str
    peer_endpoint: str
    peer_name: str = ""          # the `# <name>` comment in [Peer] (Proton writes it)
    options: dict[str, str] = field(default_factory=dict)  # provider option comments
    address6: str = ""  # IPv6 address without prefix; "" when the config has none


def _first(values: str, family: type) -> str:
    for v in values.split(","):
        v = v.strip().split("/")[0]
        try:
            if isinstance(ipaddress.ip_address(v), family):
                return v
        except ValueError:
            continue
    return ""


def _first_ipv4(values: str) -> str:
    return _first(values, ipaddress.IPv4Address)


def parse(path: Path) -> WgConf:
    return parse_text(path.read_text(), str(path))


def parse_text(text: str, where: str = "config") -> WgConf:
    section, vals, options, peer_name = "", {}, {}, ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line.strip("[]").lower()
            continue
        if line.startswith("#"):
            m = OPTION_COMMENT.match(line)
            if m and section == "interface":
                options[m.group(1)] = m.group(2).strip()
            elif section == "peer" and not peer_name and "=" not in line:
                peer_name = line.lstrip("#").strip()
            continue
        if "=" in line:
            k, v = (x.strip() for x in line.split("=", 1))
            if k.lower() != "privatekey":
                vals.setdefault(f"{section}.{k.lower()}", v.split("#")[0].strip())
    conf = WgConf(
        address=_first_ipv4(vals.get("interface.address", "")),
        dns=_first_ipv4(vals.get("interface.dns", "")),
        peer_pubkey=vals.get("peer.publickey", ""),
        peer_endpoint=vals.get("peer.endpoint", ""),
        peer_name=peer_name, options=options,
        address6=_first(vals.get("interface.address", ""), ipaddress.IPv6Address))
    if not (conf.address and conf.dns and conf.peer_pubkey and conf.peer_endpoint):
        raise ValueError(f"{where}: missing IPv4 Address/DNS or [Peer] PublicKey/Endpoint")
    return conf
