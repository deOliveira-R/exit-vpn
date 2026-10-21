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


def test_ok():
    assert classify(obs()).outcome == OK


def test_error_first():
    v = classify(obs(error="net::ERR_CONNECTION_RESET", status=None))
    assert v.outcome == ERROR


def test_cloudflare_challenge_by_header_title_frame_markup():
    assert classify(obs(headers={"cf-mitigated": "challenge"})).outcome == CHALLENGE
    assert classify(obs(title="Just a moment...")).outcome == CHALLENGE
    assert classify(obs(frame_urls=["https://challenges.cloudflare.com/cdn-cgi/x"])).outcome == CHALLENGE
    assert classify(obs(html='<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>')).outcome == CHALLENGE


def test_waf_202_stub_is_a_challenge():
    v = classify(obs(engine="http", status=202, title="", text="", html="", html_len=2057))
    assert v.outcome == CHALLENGE


def test_block_statuses():
    for st in (401, 403, 429, 451):
        assert classify(obs(status=st)).outcome == BLOCKED


def test_challenge_outranks_403_but_both_signals_kept():
    v = classify(obs(status=403, title="Just a moment..."))
    assert v.outcome == CHALLENGE and "status 403" in v.signals


def test_redirect_away_vs_same_site_vs_sso():
    assert classify(obs(final_url="https://blocked.other.net/")).outcome == REDIRECTED
    assert classify(obs(final_url="https://shop.example.com/x")).outcome == OK
    assert classify(obs(final_url="https://accounts.google.com/signin")).outcome == OK


def test_suspicious_text_needs_context():
    assert classify(obs(text="It looks like you are using a VPN or proxy. Please turn off")).outcome == SUSPICIOUS
    assert classify(obs(text="This title is not available in your country")).outcome == SUSPICIOUS
    assert classify(obs(text="Our VPN review and proxy guide")).outcome == OK


def test_http_tier_near_empty_is_inconclusive_browser_is_not():
    empty = dict(title="", text="", html="<div id=root></div>", html_len=300)
    assert classify(obs(engine="http", **empty)).outcome == INCONCLUSIVE
    assert classify(obs(engine="browser", **empty)).outcome == OK


def test_registrable_domains():
    assert registrable("www.bbc.co.uk") == "bbc.co.uk"
    assert registrable("a.b.example.com") == "example.com"
    assert registrable("globo.com.br") == "globo.com.br"


def test_text_helpers():
    h = "<html><head><title> Hi &amp; bye </title><script>var vpn='detected'</script></head><body><p>Hello</p></body></html>"
    assert title_of(h) == "Hi & bye"
    assert "detected" not in visible_text(h) and "Hello" in visible_text(h)
