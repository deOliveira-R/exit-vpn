"""Health probes, all bound to the tunnel address so they can only travel through wg0."""

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


def parse_trace(text: str) -> dict[str, str]:
    """Cloudflare's /cdn-cgi/trace: key=value lines."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


async def https_probe(url: str, source: str, timeout: float) -> tuple[bool, dict[str, str], str]:
    transport = httpx.AsyncHTTPTransport(local_address=source, retries=0)
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


async def dns_probe(server: str, name: str, source: str, timeout: float) -> tuple[bool, str]:
    loop = asyncio.get_running_loop()
    qid = int.from_bytes(os.urandom(2), "big")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.bind((source, 0))
        await loop.sock_sendto(sock, dns_query(name, qid), (server, 53))
        resp = await asyncio.wait_for(loop.sock_recv(sock, 512), timeout)
        return (True, "") if dns_answer_ok(resp, qid) else (False, "dns: bad answer")
    except (TimeoutError, OSError) as e:
        return False, f"dns: {type(e).__name__}"
    finally:
        sock.close()


class Prober:
    """One health cycle: (a) >= 1 HTTPS probe OK, (b) DNS OK, (c) fresh handshake.
    A handshake alone proves nothing (Proton completes it even for unknown keys), so
    (a) is authoritative and (c) only catches a stalled tunnel."""

    def __init__(self, source: str, dns_server: str, urls: list[str], dns_name: str,
                 timeout: float, dns_timeout: float, handshake_max_age: float):
        self.source, self.dns_server, self.urls, self.dns_name = source, dns_server, urls, dns_name
        self.timeout, self.dns_timeout, self.handshake_max_age = timeout, dns_timeout, handshake_max_age

    async def cycle(self, latest_handshake: int, now: float | None = None) -> CycleResult:
        now = time.time() if now is None else now
        results = await asyncio.gather(
            *(https_probe(u, self.source, self.timeout) for u in self.urls),
            dns_probe(self.dns_server, self.dns_name, self.source, self.dns_timeout))
        https = results[:-1]
        dns_ok, dns_err = results[-1]
        https_ok = any(ok for ok, _, _ in https)
        trace = next((t for ok, t, _ in https if ok and t), {})
        age = (now - latest_handshake) if latest_handshake else None
        hs_ok = age is not None and age < self.handshake_max_age
        errors = [e for ok, _, e in https if not ok] + ([dns_err] if not dns_ok else [])
        if not hs_ok:
            errors.append(f"handshake age {age if age is None else round(age)}")
        return CycleResult(https_ok and dns_ok and hs_ok, https_ok, dns_ok, age,
                           trace.get("ip"), trace.get("loc"), errors)
