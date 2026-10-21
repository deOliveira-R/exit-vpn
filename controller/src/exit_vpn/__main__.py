"""Entry point (s6 longrun `controller`): read the settings, build the node (`node.build`),
start the health loop and the API on the node's tailnet IPv4."""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx
import uvicorn

from .api import create_app
from .auth import Whois
from .catalog_source import CatalogSource
from .config import Settings
from .keys import KeyStore
from .node import build
from .persistence import CatalogCache
from .probes import HealthProbe, parse_trace
from .prober_client import ProberClient
from .wg import WireGuard
from .wgconf import parse as parse_conf

log = logging.getLogger("exit_vpn")


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


async def learn_underlay_ip(found: list[str]) -> None:
    """The node's own (non-VPN) egress IP, via the main table: any probe that exits from it
    is aborted by the prober (defence in depth over its kill switch)."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://www.cloudflare.com/cdn-cgi/trace")
            ip = parse_trace(r.text).get("ip")
            if ip and ip not in found:
                found[:] = [ip]
                log.info("underlay egress IP learned")
            await asyncio.sleep(3600)
        except Exception as e:
            log.warning("underlay IP lookup failed: %s", e)
            await asyncio.sleep(60)


async def amain() -> None:
    s = Settings.from_env()
    if not s.owners:
        raise SystemExit("EXIT_OWNERS is empty: nobody could use the API")
    keys = KeyStore(s.key_dirs, {"a": s.vpn_conf, "b": s.probe_conf})
    conf = parse_conf(keys.path("a") or s.vpn_conf)   # init-wg used the same file
    catalog = await CatalogSource(s, CatalogCache(s.catalog_file)).load_at_startup()
    probe = HealthProbe(conf.address, conf.dns, s.probe_urls, s.dns_probe_name,
                        s.probe_timeout, s.dns_timeout, s.handshake_max_age)
    underlay: list[str] = []
    node = build(s, exit_conf=conf, wg=WireGuard(s.wg_if), probe=probe, catalog=catalog,
                 keys=keys, prober=ProberClient(s.prober_url) if s.prober_url else None,
                 forbid_ips=lambda: list(underlay))
    await node.service.start()
    ip = await tailnet_ip(s)
    app = create_app(node, Whois(s.ts_socket), s.owners)
    server = uvicorn.Server(uvicorn.Config(app, host=ip, port=s.api_port, lifespan="off",
                                           access_log=False, log_level="warning"))
    log.info("API on http://%s:%d (owners: %s)", ip, s.api_port, ", ".join(s.owners))
    await asyncio.gather(node.service.run(), server.serve(), learn_underlay_ip(underlay))


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="exit-vpn[controller]: %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
