"""The prober's side of its contracts (contract): what it emits matches contracts/prober/, and it accepts the controller's request sample."""

import sys
from pathlib import Path

from prober.api import ProbeRequest

from harness import BODY, FakeExitCheck, FakeTunnel, http, prober, request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "contracts"))
import shape  # noqa: E402


async def test_a_two_tier_probe_response_matches_the_sample():
    r = await request(prober(http_engine=http(title="Just a moment...")), "POST", "/probe", BODY)
    assert r.status_code == 200 and len(r.json()["tiers"]) == 2     # HTTP, then the browser
    shape.check("prober/probe_response", r.json())


async def test_a_failed_probe_response_matches_the_error_sample():
    r = await request(prober(FakeTunnel(), FakeExitCheck(fail="no handshake")), "POST", "/probe", BODY)
    assert r.status_code == 200 and r.json()["outcome"] == "error"
    shape.check("prober/probe_response_error", r.json())


async def test_healthz_matches_the_sample():
    r = await request(prober(), "GET", "/healthz")
    assert r.status_code == 200
    shape.check("prober/healthz", r.json())


def test_the_controllers_request_sample_is_accepted():
    req = ProbeRequest.model_validate(shape.load("prober/probe_request"))
    assert req.server.pubkey and req.url.startswith("https://")
