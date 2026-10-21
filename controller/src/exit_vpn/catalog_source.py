"""Where the catalog comes from (PLAN D12, 3b.2):
- at startup, the cached catalog if it was pushed or came from the configured URL, else a
  download of the pinned gluetun-servers file (through the underlay: public data);
- `exitctl … servers --push`: the provider's live list, validated (`accept_push`);
- `DELETE /v1/catalog`: back to the pinned file (`fetch_pinned`).
"""

from __future__ import annotations

import logging
import time

import httpx

from .catalog import PUSH_PREFIX, Catalog, CatalogError, parse_gluetun, parse_pushed
from .config import Settings
from .errors import PolicyError
from .persistence import CatalogCache

log = logging.getLogger("exit_vpn")


class CatalogSource:
    def __init__(self, settings: Settings, cache: CatalogCache):
        self.s, self.cache = settings, cache

    async def fetch_pinned(self) -> Catalog:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(self.s.catalog_url)
            r.raise_for_status()
        return Catalog(parse_gluetun(r.json(), self.s.hostname_suffix), self.s.catalog_url,
                       time.time())

    async def load_at_startup(self) -> Catalog:
        cached = None
        try:
            cached = self.cache.load()
            if cached.pushed or cached.source == self.s.catalog_url:
                return cached
        except (FileNotFoundError, CatalogError, ValueError, KeyError) as e:
            log.info("no usable cached catalog: %s", e)
        try:
            cat = await self.fetch_pinned()
            self.cache.save(cat)
            log.info("catalog: %d servers from %s", len(cat), self.s.catalog_url)
            return cat
        except (httpx.HTTPError, CatalogError, ValueError) as e:
            if cached:
                log.warning("catalog download failed (%s); keeping the cached one", e)
                return cached
            raise

    def accept_push(self, provider: str, rows: list[dict], current: Catalog, who: str,
                    now: float) -> Catalog:
        """A pushed list, checked as strictly as a download (it decides where exit traffic
        goes) and refused if it would shrink the catalog suspiciously."""
        if provider != self.s.provider:
            raise PolicyError(f"this node's provider is {self.s.provider!r}")
        try:
            servers = parse_pushed(rows, self.s.hostname_suffix, current.country_for_code,
                                   len(current))
        except CatalogError as e:
            raise PolicyError(str(e)) from None
        return Catalog(servers, f"{PUSH_PREFIX}{provider} api by {who}", now)
