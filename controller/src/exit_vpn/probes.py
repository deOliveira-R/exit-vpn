"""The health probe of one exit tunnel: HTTPS, DNS and handshake checks, all bound to the
tunnel's interface (both tunnels carry the same address, so only the interface tells them
apart) so they can only travel through it. (Not the prober container, which tests other
servers for /v1/find: that is `prober_client`.)"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class CycleResult:
    ok: bool
    https_ok: bool
    dns_ok: bool
    handshake_age: float | None
    exit_ip: str | None = None
    loc: str | None = None
    errors: list[str] = field(default_factory=list)
    # IPv6 through the tunnel: None when not probed (no IPv6 address, or IPv6 off). Never
    # part of `ok`: IPv6 opens or closes the IPv6 gate, it never causes a failover.
    ipv6_ok: bool | None = None
    exit_ip6: str | None = None
    ipv6_error: str = ""


def parse_trace(text: str) -> dict[str, str]:
    """Cloudflare's /cdn-cgi/trace: key=value lines."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


# Linux's SO_BINDTODEVICE (absent from macOS's socket module, where the unit tests run)
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)


def _device_opts(device: str) -> list[tuple[int, int, bytes]]:
    return [(socket.SOL_SOCKET, SO_BINDTODEVICE, device.encode())] if device else []


async def https_probe(url: str, source: str, timeout: float,
                      device: str = "") -> tuple[bool, dict[str, str], str]:
    transport = httpx.AsyncHTTPTransport(local_address=source, retries=0,
                                         socket_options=_device_opts(device))
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout,
                                     follow_redirects=False) as client:
            r = await client.get(url)
        if r.status_code >= 400:
            return False, {}, f"{url}: HTTP {r.status_code}"
        return True, parse_trace(r.text) if "trace" in url else {}, ""
    except (httpx.HTTPError, OSError) as e:
        return False, {}, f"{url}: {type(e).__name__}"


def dns_query(name: str, qid: int) -> bytes:
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # RD, one question
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return header + qname + struct.pack("!HH", 1, 1)  # A, IN


def dns_answer_ok(resp: bytes, qid: int) -> bool:
    if len(resp) < 12:
        return False
    rid, flags, _, ancount, _, _ = struct.unpack("!HHHHHH", resp[:12])
    return rid == qid and flags & 0x8000 and (flags & 0x000F) == 0 and ancount > 0


async def dns_probe(server: str, name: str, source: str, timeout: float,
                    device: str = "") -> tuple[bool, str]:
    loop = asyncio.get_running_loop()
    qid = int.from_bytes(os.urandom(2), "big")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        for level, opt, value in _device_opts(device):
            sock.setsockopt(level, opt, value)
        sock.bind((source, 0))
        await loop.sock_sendto(sock, dns_query(name, qid), (server, 53))
        resp = await asyncio.wait_for(loop.sock_recv(sock, 512), timeout)
        return (True, "") if dns_answer_ok(resp, qid) else (False, "dns: bad answer")
    except (TimeoutError, OSError) as e:
        return False, f"dns: {type(e).__name__}"
    finally:
        sock.close()


class HealthProbe:
    """One health cycle: (a) >= 1 HTTPS probe OK, (b) DNS OK, (c) fresh handshake.
    A handshake alone proves nothing (Proton completes it even for unknown keys), so
    (a) is authoritative and (c) only catches a stalled tunnel."""

    def __init__(self, source: str, dns_server: str, urls: list[str], dns_name: str,
                 timeout: float, dns_timeout: float, handshake_max_age: float,
                 source6: str = "", url6: str = "", timeout6: float = 10.0, device: str = ""):
        self.source, self.dns_server, self.urls, self.dns_name = source, dns_server, urls, dns_name
        self.device = device   # the tunnel's interface: every probe is bound to it
        self.timeout, self.dns_timeout, self.handshake_max_age = timeout, dns_timeout, handshake_max_age
        self.source6, self.url6 = source6, url6   # IPv6 probe: bound to the tunnel's v6 address
        self.timeout6 = timeout6
        self.ipv6_on = True   # IPv6 is probed on the active tunnel only (it carries the gate)
        self._v6: asyncio.Task | None = None

    async def _ipv6(self) -> tuple[bool | None, dict[str, str], str]:
        """The IPv6 probe runs across cycles: Proton's first IPv6 connection to a destination
        can stall 4-8 s, so it gets `timeout6`, while a cycle waits for it no longer than for
        its IPv4 probes. Until it finishes, cycles report no IPv6 verdict (None)."""
        if not (self.source6 and self.url6 and self.ipv6_on):
            return None, {}, ""
        if self._v6 is None:
            self._v6 = asyncio.create_task(https_probe(self.url6, self.source6, self.timeout6,
                                                       device=self.device))
        try:
            ok, trace, err = await asyncio.wait_for(asyncio.shield(self._v6), self.timeout)
        except TimeoutError:
            return None, {}, ""
        self._v6 = None
        return ok, trace, f"ipv6: {err}" if err else ""

    def reset_ipv6(self) -> None:
        """A new server or key: a probe still running speaks for the old one, drop it."""
        if self._v6 is not None:
            self._v6.cancel()
            self._v6 = None

    async def cycle(self, latest_handshake: int, now: float | None = None) -> CycleResult:
        now = time.time() if now is None else now
        results = await asyncio.gather(
            *(https_probe(u, self.source, self.timeout, device=self.device) for u in self.urls),
            dns_probe(self.dns_server, self.dns_name, self.source, self.dns_timeout,
                      device=self.device),
            self._ipv6())
        https = results[:-2]
        dns_ok, dns_err = results[-2]
        v6_ok, v6_trace, v6_err = results[-1]
        https_ok = any(ok for ok, _, _ in https)
        trace = next((t for ok, t, _ in https if ok and t), {})
        age = (now - latest_handshake) if latest_handshake else None
        hs_ok = age is not None and age < self.handshake_max_age
        errors = [e for ok, _, e in https if not ok] + ([dns_err] if not dns_ok else [])
        if not hs_ok:
            errors.append(f"handshake age {age if age is None else round(age)}")
        return CycleResult(https_ok and dns_ok and hs_ok, https_ok, dns_ok, age,
                           trace.get("ip"), trace.get("loc"), errors,
                           v6_ok, v6_trace.get("ip") if v6_ok else None, v6_err)
