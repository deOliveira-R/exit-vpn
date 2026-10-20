"""Entry point (s6 longrun `controller`)."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import httpx
import uvicorn

from .api import create_app
from .auth import Whois
from .catalog import Catalog, CatalogError, parse_gluetun
from .config import Settings
from .controller import Controller
from .probes import Prober
from .state import StateStore
from .wg import WireGuard
from .wgconf import parse as parse_conf

log = logging.getLogger("exit_vpn")


async def load_catalog(s: Settings) -> Catalog:
    """Cached catalog if it came from the configured URL, else download (via the
    underlay; the catalog is public data) and cache it."""
    cached = None
    try:
        cached = Catalog.load(s.catalog_file)
        if cached.source == s.catalog_url:
            return cached
    except (FileNotFoundError, CatalogError, ValueError, KeyError) as e:
        log.info("no usable cached catalog: %s", e)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(s.catalog_url)
            r.raise_for_status()
        cat = Catalog(parse_gluetun(r.json(), s.hostname_suffix), s.catalog_url, time.time())
        cat.save(s.catalog_file)
        log.info("catalog: %d servers from %s", len(cat), s.catalog_url)
        return cat
    except (httpx.HTTPError, CatalogError, ValueError) as e:
        if cached:
            log.warning("catalog download failed (%s); keeping the cached one", e)
            return cached
        raise


async def tailnet_ip(s: Settings) -> str:
    for _ in range(120):
        proc = await asyncio.create_subprocess_exec(
            "tailscale", f"--socket={s.ts_socket}", "ip", "-4",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        if proc.returncode == 0 and out.strip():
            return out.decode().split()[0]
        await asyncio.sleep(1)
    raise RuntimeError("no tailnet IPv4 after 120 s")


async def amain() -> None:
    s = Settings.from_env()
    if not s.owners:
        raise SystemExit("EXIT_OWNERS is empty: nobody could use the API")
    conf = parse_conf(s.vpn_conf)
    catalog = await load_catalog(s)
    prober = Prober(conf.address, conf.dns, s.probe_urls, s.dns_probe_name,
                    s.probe_timeout, s.dns_timeout, s.handshake_max_age)
    ctl = Controller(s, conf, WireGuard(s.wg_if, s.wg_port), prober, catalog,
                     StateStore(s.state_file, s.peer_env_file, s.countries))
    await ctl.init()
    ip = await tailnet_ip(s)
    app = create_app(ctl, Whois(s.ts_socket), s.owners)
    server = uvicorn.Server(uvicorn.Config(app, host=ip, port=s.api_port, lifespan="off",
                                           access_log=False, log_level="warning"))
    log.info("API on http://%s:%d (owners: %s)", ip, s.api_port, ", ".join(s.owners))
    await asyncio.gather(ctl.run(), server.serve())


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="exit-vpn[controller]: %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
