"""HTTP API, bound to the node's tailnet IPv4 only (PLAN Phase 3 'API')."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from urllib.parse import urlsplit

from .auth import Whois, decide
from .controller import Controller, SwitchError
from .failover import find_candidates
from .finder import Finder


class RotateBody(BaseModel):
    scope: Literal["city", "country", "any"] = "country"


class SelectBody(BaseModel):
    server: str | None = None
    country: str | None = None
    city: str | None = None


class FindBody(BaseModel):
    url: str
    countries: list[str] = []
    max_candidates: int = 10
    mode: Literal["auto", "http", "browser"] = "auto"
    js: bool = False   # the site reveals VPN blocks only via JS (e.g. streaming players)


class ApplyBody(BaseModel):
    server: str
    ip: str | None = None


class PrefsBody(BaseModel):
    countries: list[str] | None = None
    auto_failover: bool | None = None
    secure_core: bool | None = None
    allow_free: bool | None = None


def create_app(ctl: Controller, whois: Whois, owners: list[str],
               finder: Finder | None = None) -> FastAPI:
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

    @app.post("/v1/find", status_code=202)
    async def find(body: FindBody, who: str = Depends(owner)):
        if not finder:
            raise HTTPException(503, "no prober configured")
        u = urlsplit(body.url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise HTTPException(400, "url must be http(s)://host/...")
        if not 1 <= body.max_candidates <= 30:
            raise HTTPException(400, "max_candidates must be 1..30")
        known = {c.lower(): c for c in ctl.catalog.countries()}
        bad = [c for c in body.countries if c.lower() not in known]
        if bad:
            raise HTTPException(400, f"unknown countries: {bad}")
        ctl.state.prune(ctl.clock())
        cands = find_candidates(ctl.catalog.servers, ctl.current_server(), ctl.state.prefs,
                                set(ctl.state.blacklist), [known[c.lower()] for c in body.countries],
                                body.max_candidates, ctl.rng)
        if not cands:
            raise HTTPException(409, "no eligible candidates")
        job = finder.start(body.url, cands, body.mode, body.js)
        ctl._event("find_started", by=who, url=body.url, job=job.id, candidates=len(cands))
        return job.view()

    @app.get("/v1/find/{job_id}")
    async def find_status(job_id: str, _: str = Depends(owner)):
        job = finder.jobs.get(job_id) if finder else None
        if not job:
            raise HTTPException(404, "no such job")
        return job.view()

    @app.post("/v1/find/{job_id}/apply")
    async def find_apply(job_id: str, body: ApplyBody, who: str = Depends(owner)):
        job = finder.jobs.get(job_id) if finder else None
        if not job:
            raise HTTPException(404, "no such job")
        hit = next((r for r in job.results if r["server"] == body.server
                    and (body.ip is None or r.get("ip") == body.ip)), None)
        if not hit:
            raise HTTPException(400, "server was not probed in this job")
        srv = next((s for s in job.candidates if s.name == hit["server"] and s.ip == hit["ip"]), None)
        try:
            await ctl.switch([srv], f"find {job_id} apply by {who}")
        except SwitchError as e:
            raise HTTPException(409, str(e)) from None
        return ctl.status()

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
