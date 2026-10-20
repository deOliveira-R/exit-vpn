"""Prober API on the stack's internal network (exitnet:9000; nftables admits only the
exit container). One probe at a time: wg-probe has a single peer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from .classify import CHALLENGE, ERROR, INCONCLUSIVE, SUSPICIOUS, classify
from .engines import Browser, http_probe
from .tunnel import ProbeTunnel, TunnelError

log = logging.getLogger("prober")


class Target(BaseModel):
    name: str
    ip: str
    pubkey: str
    country_code: str = ""


class ProbeRequest(BaseModel):
    server: Target
    url: str
    mode: Literal["auto", "http", "browser"] = "auto"
    js: bool = False                     # the site reveals blocks only via JS: always use the browser
    forbid_ips: list[str] = []


def summarize(o, v) -> dict:
    return {"engine": o.engine, "outcome": v.outcome, "signals": v.signals,
            "http_status": o.status, "final_url": o.final_url, "title": o.title[:120],
            "text_len": len(o.text), "error": o.error}


def create_app(tunnel: ProbeTunnel | None = None, browser: Browser | None = None) -> FastAPI:
    tunnel = tunnel or ProbeTunnel(os.environ.get("PROBE_IF", "wg-probe"))
    browser = browser or Browser()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        await browser.close()

    app = FastAPI(title="exit-vpn prober", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    lock = asyncio.Lock()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "busy": lock.locked(), "browser": browser.version or None}

    @app.post("/probe")
    async def probe(req: ProbeRequest):
        async with lock:
            t0 = time.monotonic()
            res: dict = {"server": req.server.name, "url": req.url}
            try:
                await tunnel.use(req.server.pubkey, req.server.ip)
                res.update(await tunnel.verify(req.server.country_code, set(req.forbid_ips)))
            except TunnelError as e:
                await tunnel.clear()
                res.update(outcome=ERROR, signals=[f"tunnel: {e}"], tiers=[],
                           elapsed_ms=int((time.monotonic() - t0) * 1000))
                return res
            tiers = []
            if req.mode in ("auto", "http"):
                o = await http_probe(req.url)
                tiers.append(summarize(o, classify(o)))
            first = tiers[0]["outcome"] if tiers else None
            # Escalate unless the HTTP tier already saw an IP-level verdict a browser
            # would get too (403/451/429) or a hard connection error.
            if req.mode == "browser" or (req.mode == "auto" and (
                    req.js or first in (INCONCLUSIVE, CHALLENGE, SUSPICIOUS))):
                o = await browser.probe(req.url, req.server.country_code)
                tiers.append(summarize(o, classify(o)))
            await tunnel.clear()
            final = tiers[-1]
            res.update(outcome=final["outcome"], signals=final["signals"], tiers=tiers,
                       elapsed_ms=int((time.monotonic() - t0) * 1000))
            log.info("probe %s %s -> %s %s", req.server.name, req.url, final["outcome"],
                     final["signals"][:3])
            return res

    return app
