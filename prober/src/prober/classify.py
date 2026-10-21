"""The verdict on one page load (pure; PLAN Phase 4 'Classifier'). `RULES` run in order;
the first that matches sets the outcome, and every match is reported as a signal, so the
controller and the app can show why."""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlsplit

from .model import Engine, Observation, Outcome, Verdict

CHALLENGE_TITLE = re.compile(r"^\s*(just a moment|attention required|checking your browser|"
                             r"verify you are human|please wait|security check)", re.I)
CHALLENGE_BODY = re.compile(r"cf-chl-|challenge-platform|_cf_chl_opt|awswaf|"
                            r"captcha-delivery|px-captcha|perimeterx|datadome", re.I)
CAPTCHA_FRAMES = re.compile(r"challenges\.cloudflare\.com|hcaptcha\.com|"
                            r"google\.com/recaptcha|recaptcha\.net|geo\.captcha-delivery\.com", re.I)
SUSPICIOUS_TEXT = re.compile(
    r"\b(vpn|proxy|anonymi[sz]er|unblocker)\b.{0,80}\b(detected|blocked|not allowed|disable|turn off)|"
    r"unusual traffic|automated (queries|requests|access)|access denied|"
    r"not available in your (region|country|location)|"
    r"you('re| are) (using|connected (to|through)) an? (vpn|proxy)", re.I | re.S)
BLOCK_STATUS = {401: "status 401", 403: "status 403", 429: "status 429", 451: "status 451"}
SCAN = 200_000          # how much markup and text the patterns look at
STUB_PAGE = 4096        # a 202 smaller than this is a WAF challenge page, not content
EMPTY_PAGE = 2048       # without JS, a page smaller than this (and no title) says nothing

# Registrable domain without a PSL download: last two labels, or three under these.
TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.br", "com.au", "net.au", "org.au", "co.jp",
    "ne.jp", "co.nz", "co.kr", "com.mx", "com.ar", "com.tr", "co.in", "co.za", "com.sg",
    "com.hk", "com.tw", "com.cn", "co.id", "com.my", "com.ph", "co.il", "com.pl"}
# Login/SSO hops that are not "redirected away" (the site sends you to its identity provider)
SSO_DOMAINS = {"accounts.google.com", "login.microsoftonline.com", "appleid.apple.com",
               "auth0.com", "okta.com", "login.live.com"}


def registrable(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _redirect(o: Observation) -> str | None:
    if not o.final_url:
        return None
    req, fin = urlsplit(o.requested_url).hostname or "", urlsplit(o.final_url).hostname or ""
    if fin and registrable(fin) != registrable(req) and not any(
            fin == d or fin.endswith("." + d) for d in SSO_DOMAINS):
        return f"redirected to {fin}"
    return None


def _suspicious(o: Observation) -> str | None:
    m = SUSPICIOUS_TEXT.search(o.text[:SCAN])
    return f"text: {m.group(0)[:80]!r}" if m else None


# (outcome, rule): a rule returns a signal when it matches. Order is precedence.
Rule = Callable[[Observation], "str | None"]
RULES: list[tuple[Outcome, Rule]] = [
    (Outcome.ERROR, lambda o: f"error: {o.error}" if o.error else None),
    (Outcome.CHALLENGE, lambda o: "cf-mitigated: challenge"
        if o.headers.get("cf-mitigated", "").lower() == "challenge" else None),
    (Outcome.CHALLENGE, lambda o: f"challenge title: {o.title[:60]!r}"
        if CHALLENGE_TITLE.search(o.title or "") else None),
    (Outcome.CHALLENGE, lambda o: "captcha iframe"
        if any(CAPTCHA_FRAMES.search(u) for u in o.frame_urls) else None),
    (Outcome.CHALLENGE, lambda o: "challenge markup" if CHALLENGE_BODY.search(o.html[:SCAN]) else None),
    (Outcome.CHALLENGE, lambda o: "status 202 with a stub page (WAF challenge)"
        if o.status == 202 and o.html_len < STUB_PAGE else None),
    (Outcome.BLOCKED, lambda o: BLOCK_STATUS.get(o.status or 0)),
    (Outcome.REDIRECTED, _redirect),
    (Outcome.SUSPICIOUS, _suspicious),
]


def classify(o: Observation) -> Verdict:
    signals: list[str] = []
    outcome: Outcome | None = None
    for kind, rule in RULES:
        signal = rule(o)
        if signal:
            signals.append(signal)
            outcome = outcome or kind
    # The HTTP engine runs no JS: an almost empty page means "ask the browser"
    if outcome is None and o.engine == Engine.HTTP and o.html_len < EMPTY_PAGE and not o.title:
        signals.append("near-empty page without JS")
        outcome = Outcome.INCONCLUSIVE
    return Verdict(outcome or Outcome.OK, signals)
