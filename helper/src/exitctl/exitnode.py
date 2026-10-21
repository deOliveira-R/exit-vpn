"""Client of the exit node's controller API. Authentication is this Mac's tailnet identity
(the controller asks tailscaled who is calling), so there are no tokens."""

from __future__ import annotations

import os

import httpx

from .model import NodeError

DEFAULT_URL = "http://exit-proton:8080"


class ExitNode:
    def __init__(self, base_url: str | None = None, timeout: float = 15.0):
        self.base_url = (base_url or os.environ.get("EXITCTL_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout

    async def _req(self, method: str, path: str, timeout: float | None = None, **kw) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as c:
                r = await c.request(method, f"{self.base_url}{path}", **kw)
        except httpx.HTTPError as e:
            raise NodeError(0, f"{self.base_url} unreachable: {e!r}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise NodeError(r.status_code, str(detail))
        return r.json()

    async def status(self) -> dict:
        return await self._req("GET", "/v1/status")

    async def keys(self) -> list[dict]:
        return (await self._req("GET", "/v1/keys"))["slots"]

    async def key(self, slot: str) -> dict:
        for k in await self.keys():
            if k["slot"] == slot:
                return k
        raise NodeError(404, f"the exit node has no slot {slot!r}")

    async def put_key(self, slot: str, conf: str, meta: dict) -> dict:
        # The controller tries the key (up to ~45 s) and may roll back (~20 s more)
        return await self._req("PUT", f"/v1/keys/{slot}", timeout=120.0,
                               json={"conf": conf, "meta": meta})

    async def put_meta(self, slot: str, meta: dict) -> dict:
        """Update a slot's recorded facts (e.g. a renewed expiry); the key is unchanged.
        `meta.wg_public_key` must match the slot's key."""
        return await self._req("PUT", f"/v1/keys/{slot}/meta", json=meta)

    async def put_catalog(self, provider: str, servers: list[dict]) -> dict:
        return await self._req("PUT", "/v1/catalog", timeout=60.0,
                               json={"provider": provider, "servers": servers})

    async def delete_catalog(self) -> dict:
        return await self._req("DELETE", "/v1/catalog", timeout=60.0)

    @property
    def node_name(self) -> str:
        """The exit node's tailnet name (`exit-proton`), which prefixes its keys' names."""
        from urllib.parse import urlsplit
        host = urlsplit(self.base_url).hostname or "exit"
        return os.environ.get("EXITCTL_NODE") or host.split(".")[0]
