"""What a probe works with (pure): the target server, what one engine saw, its verdict, and
the result sent back to the controller (contracts/prober/probe_response.json)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Outcome(enum.StrEnum):
    """The verdict on one page load, for "can I use this server for this site?". When several
    rules match, the first in `classify.RULES` order wins: error, challenge, blocked,
    redirected, suspicious, inconclusive; ok when none matches."""
    OK = "ok"
    SUSPICIOUS = "suspicious"      # the page talks about VPNs or unusual traffic
    INCONCLUSIVE = "inconclusive"  # the HTTP engine saw an almost empty page: ask the browser
    REDIRECTED = "redirected"      # sent to another site
    BLOCKED = "blocked"            # 401/403/429/451
    CHALLENGE = "challenge"        # CAPTCHA or bot check
    ERROR = "error"                # nothing loaded (or the tunnel failed)


class Engine(enum.StrEnum):
    HTTP = "http"        # curl_cffi, Chrome's TLS fingerprint, no JavaScript
    BROWSER = "browser"  # headless Chromium over CDP


@dataclass(frozen=True)
class Target:
    """The server to probe through (the controller's Server, reduced to what a probe needs).
    An empty `country_code` skips the exit-country check."""
    name: str
    ip: str
    pubkey: str
    country_code: str = ""


@dataclass
class Observation:
    """What one engine saw. `html` is truncated to engines.MAX_HTML; `html_len` is the
    whole body for HTTP and the (truncated) DOM for the browser; `text` is the visible text."""
    engine: Engine
    requested_url: str
    final_url: str = ""
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)   # lower-case names
    title: str = ""
    text: str = ""
    html: str = ""
    html_len: int = 0
    frame_urls: list[str] = field(default_factory=list)
    error: str = ""

    @classmethod
    def failed(cls, engine: Engine, url: str, error: str) -> Observation:
        return cls(engine=engine, requested_url=url, error=error[:200])


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    signals: list[str]


@dataclass(frozen=True)
class TierResult:
    """One engine's load and its verdict, as reported in `tiers`."""
    observation: Observation
    verdict: Verdict

    def to_dict(self) -> dict:
        o, v = self.observation, self.verdict
        return {"engine": str(o.engine), "outcome": str(v.outcome), "signals": v.signals,
                "http_status": o.status, "final_url": o.final_url, "title": o.title[:120],
                "text_len": len(o.text), "error": o.error}


@dataclass
class ProbeResult:
    server: str
    url: str
    tiers: list[TierResult] = field(default_factory=list)
    exit_ip: str | None = None     # None: the tunnel never carried traffic
    exit_loc: str | None = None
    tunnel_error: str = ""
    elapsed_ms: int = 0

    @property
    def verdict(self) -> Verdict:
        if self.tunnel_error:
            return Verdict(Outcome.ERROR, [f"tunnel: {self.tunnel_error}"])
        return self.tiers[-1].verdict if self.tiers else Verdict(Outcome.ERROR, ["no engine ran"])

    def to_dict(self) -> dict:
        v = self.verdict
        out: dict = {"server": self.server, "url": self.url}
        if not self.tunnel_error:
            out.update(exit_ip=self.exit_ip, exit_loc=self.exit_loc)
        out.update(outcome=str(v.outcome), signals=v.signals,
                   tiers=[t.to_dict() for t in self.tiers], elapsed_ms=self.elapsed_ms)
        return out
