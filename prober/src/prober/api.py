"""Prober API on the stack's internal network (exitnet:9000; nftables admits only the exit
container). Request and response: contracts/prober/. The work is `probe.Prober` (a URL
through a server) and `measure.Measurer` (a server's quality)."""

from __future__ import annotations

import contextlib
import logging
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .engines import BrowserEngine
from .keyfile import KeyFileError
from .measure import Measurer
from .model import Target
from .probe import Prober
from .tunnel import TunnelError

log = logging.getLogger("prober")


class TargetBody(BaseModel):
    name: str
    ip: str
    pubkey: str
    country_code: str = ""


class ProbeRequest(BaseModel):
    server: TargetBody
    url: str
    mode: Literal["auto", "http", "browser"] = "auto"
    js: bool = False            # the site reveals blocks only via JS: always use the browser
    forbid_ips: list[str] = []  # exit IPs that prove the tunnel is not in use (the host's own)


class MeasureRequest(BaseModel):
    server: TargetBody
    seconds: int = Field(60, ge=10, le=300)
    forbid_ips: list[str] = []


def create_app(prober: Prober, browser: BrowserEngine, measurer: Measurer | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        await browser.close()

    app = FastAPI(title="exit-vpn prober", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "busy": prober.busy, "browser": browser.version or None}

    @app.post("/key/reload")
    async def reload_key():
        try:
            await prober.reload_key()
        except (TunnelError, KeyFileError, OSError) as e:
            raise HTTPException(409, f"key reload failed: {e}") from None
        log.info("wg-probe key reloaded")
        return {"ok": True}

    @app.post("/probe")
    async def probe(req: ProbeRequest):
        s = req.server
        result = await prober.run(Target(s.name, s.ip, s.pubkey, s.country_code), req.url,
                                  req.mode, req.js, req.forbid_ips)
        return result.to_dict()

    @app.post("/measure")
    async def measure(req: MeasureRequest):
        if measurer is None:
            raise HTTPException(404, "no measurer")
        s = req.server
        return await measurer.run(Target(s.name, s.ip, s.pubkey, s.country_code), req.seconds,
                                  req.forbid_ips)

    return app
