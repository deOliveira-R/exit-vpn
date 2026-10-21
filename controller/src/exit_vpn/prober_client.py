"""Client of the prober container (PLAN Phase 4): it probes a URL through its own tunnel to a
candidate server, never the exit's. Request and response: contracts/prober/."""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from .servers import Server

# A probe waits for the prober's lock (one tunnel) plus up to ~55 s of its own work
TIMEOUT = 120.0


class ProberClient:
    def __init__(self, base_url: str, timeout: float = TIMEOUT):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    async def probe(self, srv: Server, *, country_code: str, url: str, mode: str = "auto",
                    js: bool = False, forbid_ips: Sequence[str] = ()) -> dict:
        body = {"server": {"name": srv.name, "ip": srv.ip, "pubkey": srv.pubkey,
                           "country_code": country_code},
                "url": url, "mode": mode, "js": js, "forbid_ips": list(forbid_ips)}
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/probe", json=body)
            r.raise_for_status()
            return r.json()

    async def reload_key(self) -> dict:
        """The prober re-reads its key (slot b) and sets it on wg-probe."""
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/key/reload")
            r.raise_for_status()
            return r.json()
