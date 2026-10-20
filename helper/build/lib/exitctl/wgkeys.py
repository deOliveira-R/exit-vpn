"""WireGuard key material and wg-quick rendering. Keys live only in memory here."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@dataclass(frozen=True)
class Ed25519Pair:
    """Proton registers an Ed25519 public key; WireGuard uses the X25519 key derived
    from the same seed (like the dashboard and the Linux app)."""
    seed: bytes
    public: bytes

    @classmethod
    def generate(cls) -> Ed25519Pair:
        k = Ed25519PrivateKey.generate()
        return cls(k.private_bytes_raw(), k.public_key().public_bytes_raw())

    def x25519_private(self) -> str:
        h = bytearray(hashlib.sha512(self.seed).digest()[:32])
        h[0] &= 0xF8
        h[31] &= 0x7F
        h[31] |= 0x40
        return b64(bytes(h))

    def __repr__(self) -> str:
        return f"Ed25519Pair(public={b64(self.public)})"


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


_P = 2**255 - 19


def ed25519_to_x25519_public(ed_public: bytes) -> bytes:
    """Birational map Edwards → Montgomery (u = (1+y)/(1-y)): the WireGuard public key
    that belongs to a registered Ed25519 key. Lets us tell which certificate a slot uses."""
    y = int.from_bytes(ed_public, "little") & ((1 << 255) - 1)
    u = (1 + y) * pow(1 - y, _P - 2, _P) % _P
    return u.to_bytes(32, "little")


def raw_ed25519_public(text: str) -> bytes:
    """Proton returns ClientKey as base64 of the raw key or as a PEM public key."""
    if "-----BEGIN" in text:
        der = base64.b64decode("".join(l for l in text.splitlines() if l and not l.startswith("-----")))
        return der[-32:]
    return base64.b64decode(text)


def wg_public_for_client_key(text: str) -> str:
    try:
        return b64(ed25519_to_x25519_public(raw_ed25519_public(text)))
    except (ValueError, TypeError):
        return ""
