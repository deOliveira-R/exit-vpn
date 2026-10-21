"""Proton's key scheme (like its dashboard and Linux app): an Ed25519 key pair is registered
with Proton, and WireGuard uses the X25519 key derived from the same seed. From a registered
public key we can also derive the WireGuard public key, to tell which certificate a slot uses.
Keys live only in memory here."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ...wgkeys import b64

_P = 2**255 - 19


@dataclass(frozen=True)
class Ed25519Pair:
    seed: bytes
    public: bytes

    @classmethod
    def generate(cls) -> Ed25519Pair:
        k = Ed25519PrivateKey.generate()
        return cls(k.private_bytes_raw(), k.public_key().public_bytes_raw())

    def x25519_private(self) -> str:
        """The WireGuard private key for this pair (clamped SHA-512 of the seed)."""
        h = bytearray(hashlib.sha512(self.seed).digest()[:32])
        h[0] &= 0xF8
        h[31] &= 0x7F
        h[31] |= 0x40
        return b64(bytes(h))

    def __repr__(self) -> str:
        return f"Ed25519Pair(public={b64(self.public)})"


def ed25519_to_x25519_public(ed_public: bytes) -> bytes:
    """Birational map Edwards → Montgomery (u = (1+y)/(1-y))."""
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
    """The WireGuard public key of a key registered as `text` ("" if it can't be read)."""
    try:
        return b64(ed25519_to_x25519_public(raw_ed25519_public(text)))
    except (ValueError, TypeError):
        return ""
