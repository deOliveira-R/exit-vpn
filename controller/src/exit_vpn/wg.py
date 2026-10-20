"""Thin wrapper over `wg` for the active tunnel. Server changes are peer swaps (D8)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class WgError(RuntimeError):
    pass


@dataclass(frozen=True)
class Peer:
    pubkey: str
    endpoint: str
    latest_handshake: int  # unix time, 0 = never
    rx: int
    tx: int


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise WgError(f"{' '.join(args[:3])}...: {err.decode().strip()}")
    return out.decode()


def parse_dump(text: str) -> list[Peer]:
    """`wg show <if> dump`: an interface line, then one tab-separated line per peer."""
    peers = []
    for line in text.strip().splitlines()[1:]:
        f = line.split("\t")
        if len(f) >= 7:
            peers.append(Peer(f[0], f[2], int(f[4]), int(f[5]), int(f[6])))
    return peers


class WireGuard:
    def __init__(self, ifname: str, port: int):
        self.ifname, self.port = ifname, port

    async def peers(self) -> list[Peer]:
        return parse_dump(await _run("wg", "show", self.ifname, "dump"))

    async def swap_peer(self, pubkey: str, endpoint: str) -> None:
        """Replace all peers with one, in a single `wg set` (no interface teardown)."""
        args = ["wg", "set", self.ifname]
        for p in await self.peers():
            if p.pubkey != pubkey:
                args += ["peer", p.pubkey, "remove"]
        args += ["peer", pubkey, "endpoint", endpoint,
                 "allowed-ips", "0.0.0.0/0,::/0", "persistent-keepalive", "25"]
        await _run(*args)
