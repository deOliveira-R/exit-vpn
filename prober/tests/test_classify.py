"""The classification rules (logic): what one page load's observation says about the site's view of the exit."""

import pytest

from prober.classify import classify, registrable
from prober.html import title_of, visible_text
from prober.model import Observation, Outcome

OK, SUSPICIOUS, INCONCLUSIVE, REDIRECTED, BLOCKED, CHALLENGE, ERROR = (
    Outcome.OK, Outcome.SUSPICIOUS, Outcome.INCONCLUSIVE, Outcome.REDIRECTED, Outcome.BLOCKED,
    Outcome.CHALLENGE, Outcome.ERROR)

U = "https://www.example.com/page"


def obs(**kw):
    base = dict(engine="browser", requested_url=U, final_url=U, status=200,
                title="Example", text="Welcome to the shop", html="<html>ok</html>", html_len=50_000)
    base.update(kw)
    return Observation(**base)


def test_an_ordinary_page_is_ok():
    assert classify(obs()).outcome == OK


def test_a_load_error_is_an_error_whatever_else_was_seen():
    assert classify(obs(error="net::ERR_CONNECTION_RESET", status=None, title="Just a moment...")).outcome == ERROR


@pytest.mark.parametrize("sign", [
    dict(headers={"cf-mitigated": "challenge"}),
    dict(title="Just a moment..."),
    dict(frame_urls=["https://challenges.cloudflare.com/cdn-cgi/x"]),
    dict(html='<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>'),
], ids=["header", "title", "frame", "markup"])
def test_a_cloudflare_challenge_is_recognised_by_any_of_its_signs(sign):
    assert classify(obs(**sign)).outcome == CHALLENGE


def test_a_waf_202_stub_is_a_challenge():
    assert classify(obs(engine="http", status=202, title="", text="", html="", html_len=2057)).outcome == CHALLENGE


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_refusing_statuses_are_blocks(status):
    assert classify(obs(status=status)).outcome == BLOCKED


def test_a_challenge_outranks_a_403_and_both_signals_are_kept():
    v = classify(obs(status=403, title="Just a moment..."))
    assert v.outcome == CHALLENGE and "status 403" in v.signals


@pytest.mark.parametrize("final, outcome", [
    ("https://blocked.other.net/", REDIRECTED),        # another site
    ("https://shop.example.com/x", OK),                # the same registrable domain
    ("https://accounts.google.com/signin", OK),        # single sign-on
])
def test_only_a_redirect_to_another_site_counts(final, outcome):
    assert classify(obs(final_url=final)).outcome == outcome


@pytest.mark.parametrize("text, outcome", [
    ("It looks like you are using a VPN or proxy. Please turn off", SUSPICIOUS),
    ("This title is not available in your country", SUSPICIOUS),
    ("Our VPN review and proxy guide", OK),            # the words alone are not enough
])
def test_vpn_and_geo_wording_is_suspicious_only_in_context(text, outcome):
    assert classify(obs(text=text)).outcome == outcome


def test_a_near_empty_page_is_inconclusive_over_http_but_not_in_the_browser():
    empty = dict(title="", text="", html="<div id=root></div>", html_len=300)
    assert classify(obs(engine="http", **empty)).outcome == INCONCLUSIVE
    assert classify(obs(engine="browser", **empty)).outcome == OK


@pytest.mark.parametrize("host, domain", [
    ("www.bbc.co.uk", "bbc.co.uk"), ("a.b.example.com", "example.com"), ("globo.com.br", "globo.com.br")])
def test_registrable_domain_respects_two_level_suffixes(host, domain):
    assert registrable(host) == domain


def test_the_title_is_unescaped_and_visible_text_leaves_out_scripts():
    h = "<html><head><title> Hi &amp; bye </title><script>var vpn='detected'</script></head><body><p>Hello</p></body></html>"
    assert title_of(h) == "Hi & bye"
    assert "detected" not in visible_text(h) and "Hello" in visible_text(h)
