"""One probe (component): tunnel session, exit check, then the HTTP tier and, when needed, the browser."""

import pytest

from prober.engines import Persona
from prober.model import Outcome
from prober.probe import needs_browser

from harness import TARGET, URL, FakeExitCheck, FakeTunnel, browser, http, prober


@pytest.mark.parametrize("mode, js, first, expected", [
    ("browser", False, None, True),
    ("http", True, None, False),                 # http mode never escalates, even for js sites
    ("auto", True, Outcome.OK, True),            # js sites always get the browser
    *[("auto", False, o, True) for o in (Outcome.INCONCLUSIVE, Outcome.CHALLENGE, Outcome.SUSPICIOUS)],
    *[("auto", False, o, False) for o in (Outcome.OK, Outcome.BLOCKED, Outcome.REDIRECTED, Outcome.ERROR)],
])
def test_the_browser_is_needed_only_when_the_http_tier_cannot_decide(mode, js, first, expected):
    assert needs_browser(mode, js, first) is expected


@pytest.mark.parametrize("http_kw, escalates, final", [
    (dict(), False, Outcome.OK),                                              # HTTP ok: done
    (dict(status=403), False, Outcome.BLOCKED),                               # IP-level block: done
    (dict(title="Just a moment..."), True, Outcome.OK),                       # challenge: ask the browser
    (dict(title="", text="", html="<div id=root>", html_len=200), True, Outcome.OK),  # inconclusive
])
async def test_auto_mode_escalates_to_the_browser_only_when_the_http_tier_is_unsure(http_kw, escalates, final):
    t, b = FakeTunnel(), browser()
    res = await prober(t, http_engine=http(**http_kw), browser_engine=b).run(TARGET, URL)
    assert (b.calls == 1) is escalates
    assert res.verdict.outcome == final and res.exit_loc == "US"


async def test_the_tunnel_is_attached_to_the_target_for_the_probe_and_detached_after():
    t = FakeTunnel()
    await prober(t).run(TARGET, URL)
    assert t.attached == [("pk=", "198.51.100.1")] and t.detached == 1


async def test_js_sites_always_use_the_browser():
    b = browser()
    res = await prober(browser_engine=b).run(TARGET, URL, js=True)
    assert b.calls == 1 and [tier.observation.engine for tier in res.tiers] == ["http", "browser"]


async def test_browser_mode_skips_the_http_tier():
    h, b = http(), browser()
    res = await prober(http_engine=h, browser_engine=b).run(TARGET, URL, mode="browser")
    assert h.calls == 0 and b.calls == 1 and len(res.tiers) == 1


async def test_the_engines_present_a_persona_matching_the_exit_country():
    b = browser()
    await prober(http_engine=http(title="Just a moment..."), browser_engine=b).run(TARGET, URL)
    assert b.personas == [Persona("en-US", "America/New_York")]
    assert Persona.for_country("zz") == Persona("en-US", "UTC"), "unknown country: neutral persona"


async def test_a_failed_exit_check_is_an_error_and_the_page_is_never_loaded():
    h, b, t = http(), browser(), FakeTunnel()
    res = await prober(t, FakeExitCheck(fail="exit IP 1.2.3.4 is a forbidden (non-VPN) address"), h, b).run(TARGET, URL)
    assert res.verdict.outcome == Outcome.ERROR and "forbidden" in res.verdict.signals[0]
    assert res.exit_ip is None and h.calls == b.calls == 0 and t.detached == 1


async def test_a_failed_attach_is_an_error():
    res = await prober(FakeTunnel(attach_fails="wg set: no such device")).run(TARGET, URL)
    assert res.verdict.outcome == Outcome.ERROR and res.verdict.signals == ["tunnel: wg set: no such device"]


async def test_an_engine_crash_is_an_error_result_and_the_tunnel_is_still_detached():
    """A dead Chromium must not turn the probe into a 500 or leave the peer on wg-probe."""
    t = FakeTunnel()
    p = prober(t, http_engine=http(title="Just a moment..."),
               browser_engine=browser(raises=RuntimeError("Target closed")))
    res = await p.run(TARGET, URL)
    assert res.verdict.outcome == Outcome.ERROR and "Target closed" in res.verdict.signals[0]
    assert t.detached == 1 and len(res.tiers) == 2
