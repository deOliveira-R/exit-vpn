"""Proving where probe traffic exits, before anything else is fetched: the session must carry
traffic within the warm-up, its exit IP must not be a forbidden (non-VPN) address, and its
country must be the server's. Defence in depth over the netns kill switch."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
# GET url → (status, body, error), through whatever routing is in place
Fetch = Callable[[str, float], Awaitable[tuple[int | None, str, str]]]


class ExitCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExitInfo:
    ip: str
    loc: str


def parse_trace(text: str) -> dict[str, str]:
    """Cloudflare's /cdn-cgi/trace: key=value lines."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class ExitCheck:
    def __init__(self, fetch: Fetch, warmup: float = 20.0, attempt_timeout: float = 3.0):
        self.fetch, self.warmup, self.attempt_timeout = fetch, warmup, attempt_timeout

    async def verify(self, expect_code: str, forbid_ips: set[str]) -> ExitInfo:
        deadline = time.monotonic() + self.warmup
        while True:
            status, body, error = await self.fetch(TRACE_URL, self.attempt_timeout)
            if not error and status == 200:
                trace = parse_trace(body)
                ip, loc = trace.get("ip", ""), trace.get("loc", "")
                if ip in forbid_ips:
                    raise ExitCheckError(f"exit IP {ip} is a forbidden (non-VPN) address")
                if expect_code and loc.upper() != expect_code.upper():
                    raise ExitCheckError(f"exit country {loc}, expected {expect_code}")
                return ExitInfo(ip, loc)
            if time.monotonic() > deadline:
                raise ExitCheckError(f"no traffic through the tunnel within {self.warmup:.0f} s")
            await asyncio.sleep(1.0)
