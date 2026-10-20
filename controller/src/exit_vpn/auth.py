"""Controller auth: Tailscale identity via LocalAPI whois (D13). No tokens."""

from __future__ import annotations

from pathlib import Path

import httpx


def decide(whois: dict, owners: list[str]) -> tuple[bool, str]:
    """Allow iff the caller is an untagged node owned by one of `owners`.
    Tagged nodes (servers, the exit node itself) are never owners' devices."""
    node = whois.get("Node") or {}
    login = (whois.get("UserProfile") or {}).get("LoginName", "")
    who = f"{login or '?'} ({node.get('ComputedName') or node.get('Name') or '?'})"
    if node.get("Tags"):
        return False, f"tagged node {who}"
    if not login or login not in owners:
        return False, f"not an owner: {who}"
    return True, who


class Whois:
    def __init__(self, socket: Path):
        self.socket = socket

    async def lookup(self, ip: str, port: int) -> dict | None:
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket))
        async with httpx.AsyncClient(transport=transport, timeout=3.0) as c:
            r = await c.get("http://local-tailscaled.sock/localapi/v0/whois",
                            params={"addr": f"{ip}:{port}"})
        return r.json() if r.status_code == 200 else None
