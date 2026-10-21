"""wg-probe peer control. The interface, routing and kill switch are set up once by
entrypoint.sh (interface `wg-probe`, routing table 200, peers on port 51820: keep these in
step with it); here a probe attaches the tunnel to one server and detaches it afterwards.
`session()` guarantees the detach, whatever happens inside."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

from .keyfile import KeyFile
from .model import Target

log = logging.getLogger("prober")


class TunnelError(RuntimeError):
    pass


async def _run(*args: str, stdin: str | None = None) -> str:
    p = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate(stdin.encode() if stdin is not None else None)
    if p.returncode != 0:
        raise TunnelError(f"{args[0]} {args[1]}: {err.decode().strip()}")
    return out.decode()


class ProbeTunnel:
    def __init__(self, ifname: str = "wg-probe", table: int = 200, port: int = 51820, run=_run):
        self.ifname, self.table, self.port, self._run = ifname, table, port, run

    async def _peers(self) -> list[str]:
        out = await self._run("wg", "show", self.ifname, "peers")
        return [p for p in out.split() if p]

    async def attach(self, target: Target) -> None:
        """Make `target` wg-probe's only peer."""
        args = ["wg", "set", self.ifname]
        for p in await self._peers():
            if p != target.pubkey:
                args += ["peer", p, "remove"]
        args += ["peer", target.pubkey, "endpoint", f"{target.ip}:{self.port}",
                 "allowed-ips", "0.0.0.0/0", "persistent-keepalive", "25"]
        await self._run(*args)
        # The link may have been taken down (T12d); bring it and its route back
        await self._run("ip", "link", "set", self.ifname, "up")
        await self._run("ip", "route", "replace", "default", "dev", self.ifname,
                        "table", str(self.table))

    async def detach(self) -> None:
        """No peer: nothing can leave until the next attach."""
        args = ["wg", "set", self.ifname]
        for p in await self._peers():
            args += ["peer", p, "remove"]
        if len(args) > 3:
            await self._run(*args)

    @contextlib.asynccontextmanager
    async def session(self, target: Target) -> AsyncGenerator[None]:
        try:
            await self.attach(target)
            yield
        finally:
            try:
                await self.detach()
            except TunnelError as e:   # the peer stays until the next probe replaces it
                log.warning("wg-probe detach failed: %s", e)

    async def reload_key(self, key: KeyFile) -> None:
        """Run wg-probe with another private key. The address must not change: the kill
        switch and NAT were set up for it."""
        have = await self._run("ip", "-4", "-o", "addr", "show", "dev", self.ifname)
        if f"inet {key.address} " not in have:
            raise TunnelError(f"address {key.address} differs from {self.ifname}'s")
        await self.detach()
        await self._run("wg", "set", self.ifname, "private-key", "/dev/stdin",
                        stdin=key.private_key + "\n")
