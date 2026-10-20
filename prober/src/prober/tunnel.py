"""wg-probe peer control. The interface, routing and kill switch are set up once by
entrypoint.sh; here we only swap the peer and prove where traffic exits."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from .engines import http_probe

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


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


def key_conf() -> Path:
    """The config wg-probe should run with: slot b if uploaded, else the bootstrap one
    (same choice as entrypoint.sh)."""
    up = Path(os.environ.get("KEY_DIR_B", "/keys/b")) / "wg.conf"
    return up if up.exists() else Path(os.environ.get("PROBE_VPN_CONF", "/secrets/proton_b.conf"))


def conf_fields(text: str) -> tuple[str, str]:
    """(PrivateKey, first IPv4 Address) of a wg-quick config."""
    key = re.search(r"^\s*PrivateKey\s*=\s*(\S+)", text, re.M | re.I)
    addr = re.search(r"^\s*Address\s*=\s*([^#\n]+)", text, re.M | re.I)
    v4 = next((a.strip() for a in (addr.group(1).split(",") if addr else [])
               if re.fullmatch(r"\d+(\.\d+){3}(/\d+)?", a.strip())), "")
    if not key or not v4:
        raise TunnelError("config has no PrivateKey or IPv4 Address")
    return key.group(1), v4 if "/" in v4 else v4 + "/32"


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

    async def reload_key(self) -> None:
        """Set wg-probe's private key from key_conf(). The address must not change: the
        kill switch and NAT were set up for it."""
        key, addr = conf_fields(key_conf().read_text())
        have = await _run("ip", "-4", "-o", "addr", "show", "dev", self.ifname)
        if f"inet {addr} " not in have:
            raise TunnelError(f"address {addr} differs from {self.ifname}'s")
        await self.clear()
        await _run("wg", "set", self.ifname, "private-key", "/dev/stdin", stdin=key + "\n")

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
