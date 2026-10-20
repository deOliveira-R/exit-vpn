"""HTTP API, bound to the node's tailnet IPv4 only (PLAN Phase 3 'API')."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .auth import Whois, decide
from .controller import Controller, SwitchError


class RotateBody(BaseModel):
    scope: Literal["city", "country", "any"] = "country"


class SelectBody(BaseModel):
    server: str | None = None
    country: str | None = None
    city: str | None = None


class PrefsBody(BaseModel):
    countries: list[str] | None = None
    auto_failover: bool | None = None
    secure_core: bool | None = None
    allow_free: bool | None = None


def create_app(ctl: Controller, whois: Whois, owners: list[str]) -> FastAPI:
    app = FastAPI(title="exit-vpn controller", docs_url=None, redoc_url=None,
                  openapi_url=None)

    async def owner(request: Request) -> str:
        client = request.client
        if not client:
            raise HTTPException(403, "no client address")
        try:
            info = await whois.lookup(client.host, client.port)
        except Exception:
            raise HTTPException(503, "whois unavailable") from None
        ok, who = decide(info or {}, owners)
        if not ok:
            raise HTTPException(403, who)
        return who

    @app.get("/v1/status")
    async def status(_: str = Depends(owner)):
        return ctl.status()

    @app.get("/v1/servers")
    async def servers(country: str | None = None, city: str | None = None,
                      feature: str | None = None, _: str = Depends(owner)):
        if feature and feature not in ("secure_core", "tor", "free", "stream", "port_forward"):
            raise HTTPException(400, f"unknown feature {feature!r}")
        rows = ctl.catalog.filter(country, city, feature)
        return {"count": len(rows), "countries": ctl.catalog.countries() if not country else None,
                "servers": [s.to_dict() for s in rows]}

    @app.post("/v1/rotate")
    async def rotate(body: RotateBody, who: str = Depends(owner)):
        try:
            await ctl.switch(ctl.candidates(body.scope), f"rotate {body.scope} by {who}")
        except SwitchError as e:
            raise HTTPException(409, str(e)) from None
        return ctl.status()

    @app.post("/v1/select")
    async def select(body: SelectBody, who: str = Depends(owner)):
        if not (body.server or body.country or body.city):
            raise HTTPException(400, "give server, country or city")
        cands = ctl.select_candidates(body.server, body.country, body.city)
        try:
            await ctl.switch(cands, f"select {body.model_dump(exclude_none=True)} by {who}")
        except SwitchError as e:
            raise HTTPException(409, str(e)) from None
        return ctl.status()

    @app.get("/v1/prefs")
    async def get_prefs(_: str = Depends(owner)):
        return vars(ctl.state.prefs)

    @app.put("/v1/prefs")
    async def put_prefs(body: PrefsBody, who: str = Depends(owner)):
        p = ctl.state.prefs
        if body.countries is not None:
            known = {c.lower(): c for c in ctl.catalog.countries()}
            bad = [c for c in body.countries if c.lower() not in known]
            if bad or not body.countries:
                raise HTTPException(400, f"unknown countries: {bad}" if bad else "empty list")
            p.countries = [known[c.lower()] for c in body.countries]
        for k in ("auto_failover", "secure_core", "allow_free"):
            if getattr(body, k) is not None:
                setattr(p, k, getattr(body, k))
        ctl._event("prefs", by=who, prefs=vars(p))
        ctl._save()
        return vars(p)

    @app.get("/v1/events")
    async def events(request: Request, _: str = Depends(owner)):
        q = ctl.subscribe()

        async def stream():
            try:
                yield f"event: status\ndata: {json.dumps(ctl.status())}\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                        yield f"event: {ev['kind']}\ndata: {json.dumps(ev)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                ctl.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
