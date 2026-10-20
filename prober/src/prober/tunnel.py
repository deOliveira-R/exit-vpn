"""wg-probe peer control. The interface, routing and kill switch are set up once by
entrypoint.sh; here we only swap the peer and prove where traffic exits."""

from __future__ import annotations

import asyncio
import time

from .engines import http_probe

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


class TunnelError(RuntimeError):
    pass


async def _run(*args: str) -> str:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    if p.returncode != 0:
        raise TunnelError(f"{args[0]} {args[1]}: {err.decode().strip()}")
    return out.decode()


class ProbeTunnel:
    def __init__(self, ifname: str = "wg-probe", table: int = 200, port: int = 51820):
        self.ifname, self.table, self.port = ifname, table, port

    async def _peers(self) -> list[str]:
        out = await _run("wg", "show", self.ifname, "peers")
        return [p for p in out.split() if p]

    async def use(self, pubkey: str, ip: str) -> None:
        args = ["wg", "set", self.ifname]
        for p in await self._peers():
            if p != pubkey:
                args += ["peer", p, "remove"]
        args += ["peer", pubkey, "endpoint", f"{ip}:{self.port}",
                 "allowed-ips", "0.0.0.0/0", "persistent-keepalive", "25"]
        await _run(*args)
        # The link may have been taken down (T12b); bring it and its route back
        await _run("ip", "link", "set", self.ifname, "up")
        await _run("ip", "route", "replace", "default", "dev", self.ifname, "table", str(self.table))

    async def clear(self) -> None:
        args = ["wg", "set", self.ifname]
        for p in await self._peers():
            args += ["peer", p, "remove"]
        if len(args) > 3:
            await _run(*args)

    async def verify(self, expect_code: str, forbid_ips: set[str], warmup: float = 20.0) -> dict:
        """Wait for the session to pass traffic, then check where it exits."""
        deadline = time.monotonic() + warmup
        while True:
            o = await http_probe(TRACE_URL, timeout=3.0)
            if not o.error and o.status == 200:
                trace = dict(line.split("=", 1) for line in o.html.splitlines() if "=" in line)
                ip, loc = trace.get("ip", ""), trace.get("loc", "")
                if ip in forbid_ips:
                    raise TunnelError(f"exit IP {ip} is a forbidden (non-VPN) address")
                if expect_code and loc.upper() != expect_code.upper():
                    raise TunnelError(f"exit country {loc}, expected {expect_code}")
                return {"exit_ip": ip, "exit_loc": loc}
            if time.monotonic() > deadline:
                raise TunnelError(f"no traffic through the tunnel within {warmup:.0f} s")
            await asyncio.sleep(1.0)
