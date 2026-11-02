"""HTTP API, bound to the node's tailnet IPv4 only; only the owner's
untagged devices may call it (`auth`). This layer checks the shape of requests and turns
domain errors into HTTP: PolicyError → 400, SwitchError → 409. Rules live in the services.
Response shapes: contracts/controller/."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import Whois, decide
from .errors import PolicyError, SwitchError
from .keys import MAX_CONF_BYTES, KeyRejected
from .node import Node
from .servers import FEATURES, FLAGS, Region
from .slots import KeyFailed
from .survey import SurveySchedule


class RotateBody(BaseModel):
    scope: Literal["city", "country", "any"] = "country"


class SelectBody(BaseModel):
    server: str | None = None   # exact pick (with `ip`: one physical server)
    ip: str | None = None
    country: str | None = None
    city: str | None = None


class RegionBody(BaseModel):
    country: str = Field(max_length=100)
    city: str | None = Field(None, max_length=100)


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
    regions: list[RegionBody] | None = Field(None, max_length=50)
    countries: list[str] | None = None   # older clients: countries only
    auto_failover: bool | None = None
    secure_core: bool | None = None      # a mode: changing it switches servers now
    allow_free: bool | None = None
    require: list[Literal[FEATURES]] | None = None
    favorites: list[str] | None = Field(None, max_length=100)


class KeyMeta(BaseModel):
    """Public facts the helper knows about a key (never the key itself)."""
    model_config = ConfigDict(extra="ignore")
    provider: str | None = Field(None, max_length=40)
    cert_id: str | None = Field(None, max_length=200)      # provider's certificate id/fingerprint
    device_name: str | None = Field(None, max_length=100)
    created_at: int | None = None
    expires_at: int | None = None
    options: dict[str, str | int | bool] = {}
    client_public_key: str | None = Field(None, max_length=200)  # e.g. Proton's Ed25519 key, for renew
    wg_public_key: str | None = Field(None, max_length=60)       # which key these facts describe


class PushedServer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(max_length=100)
    country_code: str = Field(max_length=8)
    city: str = Field("", max_length=100)
    hostname: str = Field(max_length=200)
    ip: str = Field(max_length=45)
    pubkey: str = Field(max_length=60)
    secure_core: bool = False
    tor: bool = False
    free: bool = False
    stream: bool = False
    port_forward: bool = False
    ipv6: bool = False
    load: int | None = Field(None, ge=0, le=100)
    enabled: bool = True


class CatalogBody(BaseModel):
    provider: str = Field(max_length=40)
    servers: list[PushedServer] = Field(max_length=20000)


class SurveyBody(BaseModel):
    countries: list[str] = Field([], max_length=50)   # none (and no cities): the exit's country
    cities: list[str] = Field([], max_length=50)
    servers: int = 10
    seconds: int = 60


class SurveyTargetBody(BaseModel):
    countries: list[str] = Field([], max_length=50)
    cities: list[str] = Field([], max_length=50)
    servers: int = 10


class SurveyScheduleBody(BaseModel):
    enabled: bool = False
    hours: list[int] = Field([], max_length=24)       # UTC
    minute: int = 10
    seconds: int = 60
    targets: list[SurveyTargetBody] = Field([], max_length=20)


class KeyBody(BaseModel):
    conf: str = Field(max_length=MAX_CONF_BYTES)
    meta: KeyMeta = KeyMeta()


def create_app(node: Node, whois: Whois, owners: list[str]) -> FastAPI:
    svc, slots, finder, events = node.service, node.slots, node.finder, node.events
    app = FastAPI(title="exit-vpn controller", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, exc: RequestValidationError):
        # FastAPI's default echoes the offending input, which for /v1/keys is a private key
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]})

    @app.exception_handler(PolicyError)
    async def refused(_request: Request, exc: PolicyError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SwitchError)
    async def nothing_worked(_request: Request, exc: SwitchError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

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

    # The exit: status, servers, switching ------------------------------------------------
    @app.get("/v1/status")
    async def status(_: str = Depends(owner)):
        return svc.snapshot()

    @app.get("/v1/servers")
    async def servers(country: str | None = None, city: str | None = None,
                      feature: str | None = None, _: str = Depends(owner)):
        if feature and feature not in FLAGS:
            raise HTTPException(400, f"unknown feature {feature!r}")
        rows = svc.catalog.filter(country, city, feature)
        return {"count": len(rows), "countries": svc.catalog.countries() if not country else None,
                "servers": [s.to_dict() for s in rows]}

    @app.post("/v1/rotate")
    async def rotate(body: RotateBody, who: str = Depends(owner)):
        await svc.rotate(body.scope, who)
        return svc.snapshot()

    @app.post("/v1/select")
    async def select(body: SelectBody, who: str = Depends(owner)):
        reason = f"select {body.model_dump(exclude_none=True)} by {who}"
        if body.server:
            await svc.select_server(body.server, body.ip, reason)
        elif body.country or body.city:
            await svc.select_region(body.country, body.city, who, reason)
        else:
            raise HTTPException(400, "give server, country or city")
        return svc.snapshot()

    # Preferences ------------------------------------------------------------------------
    @app.get("/v1/prefs")
    async def get_prefs(_: str = Depends(owner)):
        return svc.prefs.view()

    @app.put("/v1/prefs")
    async def put_prefs(body: PrefsBody, who: str = Depends(owner)):
        if body.regions is not None and body.countries is not None:
            raise HTTPException(400, "give regions or countries, not both")
        regions = ([Region(r.country, r.city) for r in body.regions] if body.regions is not None
                   else [Region(c) for c in body.countries] if body.countries is not None else None)
        try:
            p = await svc.update_prefs(who, regions=regions, auto_failover=body.auto_failover,
                                       secure_core=body.secure_core, allow_free=body.allow_free,
                                       require=body.require, favorites=body.favorites)
        except SwitchError as e:
            raise HTTPException(409, f"Secure Core unchanged: {e}") from None
        return p.view()

    # Quality survey ---------------------------------------------------------------------
    def survey_service():
        if not node.survey:
            raise HTTPException(503, "no prober configured")
        return node.survey

    @app.post("/v1/survey", status_code=202)
    async def survey(body: SurveyBody, who: str = Depends(owner)):
        return survey_service().start(body.countries, body.servers, body.seconds, who,
                                      body.cities).view()

    @app.get("/v1/survey/schedule")
    async def survey_schedule(_: str = Depends(owner)):
        return survey_service().schedule_view()

    @app.put("/v1/survey/schedule")
    async def survey_schedule_set(body: SurveyScheduleBody, _: str = Depends(owner)):
        sv = survey_service()
        sv.set_schedule(SurveySchedule.from_dict(body.model_dump()))
        return sv.schedule_view()

    def log_page(qlog, since: float, limit: int) -> dict:
        if qlog is None:
            raise HTTPException(404, "no such log")
        records, more = qlog.read(since, max(1, min(limit, 10000)))
        return {"records": records, "more": more}

    @app.get("/v1/survey/log")
    async def survey_log(since: float = 0.0, limit: int = 1000, _: str = Depends(owner)):
        """Survey results after `since` (unix seconds), oldest first; page with the last `ts`."""
        return log_page(survey_service().log, since, limit)

    @app.get("/v1/quality/log")
    async def quality_log(since: float = 0.0, limit: int = 1000, _: str = Depends(owner)):
        """The exit tunnels' quality summaries (one per tunnel each minute), likewise."""
        return log_page(svc.quality_log, since, limit)

    @app.get("/v1/survey/{job_id}")
    async def survey_status(job_id: str, _: str = Depends(owner)):
        job = survey_service().jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such survey")
        return job.view()

    @app.delete("/v1/survey/{job_id}")
    async def survey_cancel(job_id: str, _: str = Depends(owner)):
        if job_id not in survey_service().jobs:
            raise HTTPException(404, "no such survey")
        return survey_service().cancel(job_id).view()

    # Find a server for a URL ------------------------------------------------------------
    def find_service():
        if not finder:
            raise HTTPException(503, "no prober configured")
        return finder

    @app.post("/v1/find", status_code=202)
    async def find(body: FindBody, who: str = Depends(owner)):
        return find_service().start(body.url, body.countries, body.max_candidates, body.mode,
                                    body.js, who).view()

    @app.get("/v1/find/{job_id}")
    async def find_status(job_id: str, _: str = Depends(owner)):
        job = find_service().jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return job.view()

    @app.post("/v1/find/{job_id}/apply")
    async def find_apply(job_id: str, body: ApplyBody, who: str = Depends(owner)):
        try:
            await find_service().apply(job_id, body.server, body.ip, who)
        except KeyError:
            raise HTTPException(404, "no such job") from None
        return svc.snapshot()

    # Catalog ----------------------------------------------------------------------------
    @app.put("/v1/catalog")
    async def put_catalog(body: CatalogBody, who: str = Depends(owner)):
        cat = node.catalogs.accept_push(body.provider, [s.model_dump() for s in body.servers],
                                        svc.catalog, who, time.time())
        await svc.replace_catalog(cat)
        return svc.catalog.view()

    @app.delete("/v1/catalog")
    async def delete_catalog(who: str = Depends(owner)):
        try:
            cat = await node.catalogs.fetch_pinned()
        except Exception as e:
            raise HTTPException(502, f"pinned catalog download failed: {e}") from None
        await svc.replace_catalog(cat)
        events.publish("catalog_reverted", by=who)
        return svc.catalog.view()

    # Key slots --------------------------------------------------------------------------
    @app.get("/v1/keys")
    async def get_keys(_: str = Depends(owner)):
        return {"slots": await slots.views()}

    @app.put("/v1/keys/{slot}")
    async def put_key(slot: str, body: KeyBody, who: str = Depends(owner)):
        try:
            return await slots.put(slot, body.conf, body.meta.model_dump(exclude_none=True), who)
        except KeyRejected as e:
            raise HTTPException(400, str(e)) from None
        except KeyFailed as e:
            raise HTTPException(409, str(e)) from None

    @app.put("/v1/keys/{slot}/meta")
    async def put_key_meta(slot: str, body: KeyMeta, who: str = Depends(owner)):
        try:
            return await slots.put_meta(slot, body.model_dump(exclude_none=True), who)
        except KeyRejected as e:
            raise HTTPException(400, str(e)) from None

    # Live updates (server-sent events) --------------------------------------------------
    @app.get("/v1/events")
    async def stream_events(request: Request, _: str = Depends(owner)):
        q = events.subscribe()

        async def stream():
            try:
                yield f"event: status\ndata: {json.dumps(svc.snapshot())}\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                        yield f"event: {ev['kind']}\ndata: {json.dumps(ev)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                events.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
