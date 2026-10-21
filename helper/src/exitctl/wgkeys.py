"""WireGuard basics, provider-neutral: public keys from private ones and wg-quick rendering.
Keys live only in memory here. (Proton's Ed25519 scheme is `providers/proton/crypto`.)"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def x25519_public(private_b64: str) -> str:
    k = X25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
    return b64(k.public_key().public_bytes_raw())


@dataclass(frozen=True)
class Peer:
    name: str
    pubkey: str
    endpoint: str      # host:port


def render_conf(private_key: str, address: str, dns: str, peer: Peer,
                title: str = "", option_lines: dict[str, str] | None = None) -> str:
    """A wg-quick config in the dashboard's layout (option comments in [Interface],
    the server's name as the first [Peer] comment), which the exit node parses."""
    head = [f"# Key for {title}"] if title else []
    head += [f"# {k} = {v}" for k, v in (option_lines or {}).items()]
    return "\n".join(["[Interface]", *head,
                      f"PrivateKey = {private_key}", f"Address = {address}", f"DNS = {dns}", "",
                      "[Peer]", f"# {peer.name}", f"PublicKey = {peer.pubkey}",
                      "AllowedIPs = 0.0.0.0/0, ::/0", f"Endpoint = {peer.endpoint}",
                      "PersistentKeepalive = 25", ""])
