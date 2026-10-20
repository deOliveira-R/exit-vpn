"""Verdict for one page load (pure). Ordered rules; the first match sets the outcome,
but every matching signal is reported (PLAN Phase 4 'Classifier')."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Outcomes, best to worst for "can I use this server for this site?"
OK, SUSPICIOUS, INCONCLUSIVE, REDIRECTED, BLOCKED, CHALLENGE, ERROR = (
    "ok", "suspicious", "inconclusive", "redirected", "blocked", "challenge", "error")

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

# Registrable domain without a PSL download: last two labels, or three under these.
TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.br", "com.au", "net.au", "org.au", "co.jp",
    "ne.jp", "co.nz", "co.kr", "com.mx", "com.ar", "com.tr", "co.in", "co.za", "com.sg",
    "com.hk", "com.tw", "com.cn", "co.id", "com.my", "com.ph", "co.il", "com.pl"}
# Login/SSO hops that are not "redirected away" (the site sends you to its identity provider)
SSO_DOMAINS = {"accounts.google.com", "login.microsoftonline.com", "appleid.apple.com",
               "auth0.com", "okta.com", "login.live.com"}


def visible_text(html: str) -> str:
    """Rough visible text of an HTML document (for the HTTP tier)."""
    import html as _html
    h = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def title_of(html: str) -> str:
    import html as _html
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip() if m else ""


def registrable(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


@dataclass
class Observation:
    engine: str                          # "http" (curl_cffi) or "browser" (Chromium)
    requested_url: str
    final_url: str = ""
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)   # lower-case names
    title: str = ""
    text: str = ""                       # visible text (scripts/styles removed)
    html: str = ""                       # markup, truncated (challenge markers live here)
    html_len: int = 0
    frame_urls: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class Verdict:
    outcome: str
    signals: list[str]


def classify(o: Observation) -> Verdict:
    signals: list[str] = []
    outcome: str | None = None

    def hit(kind: str, signal: str) -> None:
        nonlocal outcome
        signals.append(signal)
        if outcome is None:
            outcome = kind

    if o.error:
        hit(ERROR, f"error: {o.error}")
    if o.headers.get("cf-mitigated", "").lower() == "challenge":
        hit(CHALLENGE, "cf-mitigated: challenge")
    if CHALLENGE_TITLE.search(o.title or ""):
        hit(CHALLENGE, f"challenge title: {o.title[:60]!r}")
    if any(CAPTCHA_FRAMES.search(u) for u in o.frame_urls):
        hit(CHALLENGE, "captcha iframe")
    if CHALLENGE_BODY.search(o.html[:200_000]):
        hit(CHALLENGE, "challenge markup")
    if o.status == 202 and o.html_len < 4096:
        hit(CHALLENGE, "status 202 with a stub page (WAF challenge)")
    if o.status in BLOCK_STATUS:
        hit(BLOCKED, BLOCK_STATUS[o.status])
    if o.final_url:
        req, fin = urlsplit(o.requested_url).hostname or "", urlsplit(o.final_url).hostname or ""
        if fin and registrable(fin) != registrable(req) and not any(
                fin == d or fin.endswith("." + d) for d in SSO_DOMAINS):
            hit(REDIRECTED, f"redirected to {fin}")
    m = SUSPICIOUS_TEXT.search(o.text[:200_000])
    if m:
        hit(SUSPICIOUS, f"text: {m.group(0)[:80]!r}")
    # The HTTP tier sees no JS: an almost empty page means "ask the browser"
    if o.engine == "http" and outcome is None and o.html_len < 2048 and not o.title:
        hit(INCONCLUSIVE, "near-empty page without JS")
    return Verdict(outcome or OK, signals)
