"""Measuring one server's quality through wg-probe, for the controller's survey: the same
samples the exit takes on its own tunnels (quality.py there), so servers the exit never
used can be compared with the ones it does. Attach, prove the exit (as a probe does), let
the fresh session settle, then for `seconds`: a ping each second (a DNS query for a cached
name to the resolver the key's config names, at the far end of the tunnel, whatever the
provider) and an HTTPS check every `https_every` pings. The controller judges the samples; nothing is judged here.
Response: contracts/prober/measure_response.json."""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from collections.abc import Awaitable, Callable, Iterable

from .exitcheck import TRACE_URL, ExitCheckError, Fetch
from .keyfile import KeyFile, KeyFileError
from .model import Target
from .probe import Prober
from .tunnel import TunnelError

PING_NAME = "www.cloudflare.com"
SETTLE = 5                      # pings after the exit check that are not counted (warm-up)

Ping = Callable[[str], Awaitable[float | None]]   # resolver → ms, None if lost


def dns_query(name: str, qid: int) -> bytes:
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return header + qname + struct.pack("!HH", 1, 1)


def answer_ok(resp: bytes, qid: int) -> bool:
    if len(resp) < 12:
        return False
    rid, flags, _, ancount, _, _ = struct.unpack("!HHHHHH", resp[:12])
    return rid == qid and bool(flags & 0x8000) and flags & 0x000F == 0 and ancount > 0


async def dns_ping(server: str, name: str = PING_NAME, timeout: float = 1.0) -> float | None:
    """Round-trip milliseconds of one query, None if lost. The netns routes it via wg-probe."""
    loop = asyncio.get_running_loop()
    qid = int.from_bytes(os.urandom(2), "big")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    t0 = time.monotonic()
    try:
        await loop.sock_sendto(sock, dns_query(name, qid), (server, 53))
        resp = await asyncio.wait_for(loop.sock_recv(sock, 512), timeout)
    except (TimeoutError, OSError):
        return None
    finally:
        sock.close()
    return (time.monotonic() - t0) * 1000 if answer_ok(resp, qid) else None


class Measurer:
    def __init__(self, prober: Prober, fetch: Fetch, ping: Ping = dns_ping,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep,
                 https_every: int = 5, resolver: Callable[[], str] = lambda: KeyFile.current().dns):
        self.prober, self.fetch, self.ping, self.resolver = prober, fetch, ping, resolver
        self.clock, self.sleep, self.https_every = clock, sleep, https_every

    async def run(self, target: Target, seconds: int, forbid_ips: Iterable[str] = ()) -> dict:
        p = self.prober
        async with p.lock:   # one tunnel: never while a probe uses it
            t0 = self.clock()
            out: dict = {"server": target.name, "exit_ip": None, "exit_loc": None,
                         "interval": 1.0, "https_every": self.https_every,
                         "pings": [], "https": [], "error": ""}
            try:
                resolver = self.resolver()
            except (KeyFileError, OSError) as e:
                resolver, out["error"] = "", f"slot b key: {e}"
            if not resolver:
                out["error"] = out["error"] or "the slot b key's config names no IPv4 DNS server"
                out["elapsed_ms"] = 0
                return out
            try:
                async with p.tunnel.session(target):
                    info = await p.exit_check.verify(target.country_code, set(forbid_ips))
                    out["exit_ip"], out["exit_loc"] = info.ip, info.loc
                    for i in range(SETTLE + seconds):
                        started = self.clock()
                        ms = await self.ping(resolver)
                        if i >= SETTLE:
                            out["pings"].append(None if ms is None else round(ms, 1))
                            if (i - SETTLE) % self.https_every == 0:
                                out["https"].append(await self._https())
                        await self.sleep(max(0.0, 1.0 - (self.clock() - started)))
            except (TunnelError, ExitCheckError) as e:
                out["error"] = str(e)
            out["elapsed_ms"] = int((self.clock() - t0) * 1000)
            return out

    async def _https(self) -> float | None:
        started = self.clock()
        status, _, error = await self.fetch(TRACE_URL, 3.0)
        return round((self.clock() - started) * 1000, 1) if status == 200 and not error else None
