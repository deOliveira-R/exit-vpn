"""One probe, start to finish (PLAN Phase 4): attach wg-probe to the target server, prove
where traffic exits, load the URL with the engines the policy asks for, classify each load,
detach. One probe at a time (wg-probe has a single peer); a key reload waits for it too."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

from .classify import classify
from .engines import BrowserEngine, HttpEngine, Persona
from .exitcheck import ExitCheck, ExitCheckError
from .keyfile import KeyFile
from .model import Observation, Outcome, ProbeResult, Target, TierResult
from .tunnel import ProbeTunnel, TunnelError

log = logging.getLogger("prober")

MODES = ("auto", "http", "browser")


def needs_browser(mode: str, js: bool, first: Outcome | None) -> bool:
    """Whether the browser loads the page too. `auto` escalates when the site reveals
    blocks only through JS, or when the HTTP verdict is unsure (inconclusive, challenge,
    suspicious); an IP-level verdict (blocked, redirected) or an error would not change."""
    if mode == "browser":
        return True
    if mode == "http":
        return False
    return js or first in (Outcome.INCONCLUSIVE, Outcome.CHALLENGE, Outcome.SUSPICIOUS)


class Prober:
    def __init__(self, tunnel: ProbeTunnel, exit_check: ExitCheck, http: HttpEngine,
                 browser: BrowserEngine):
        self.tunnel, self.exit_check, self.http, self.browser = tunnel, exit_check, http, browser
        self.lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    async def run(self, target: Target, url: str, mode: str = "auto", js: bool = False,
                  forbid_ips: Iterable[str] = ()) -> ProbeResult:
        async with self.lock:
            t0 = time.monotonic()
            result = ProbeResult(server=target.name, url=url)
            try:
                async with self.tunnel.session(target):
                    exit_info = await self.exit_check.verify(target.country_code, set(forbid_ips))
                    result.exit_ip, result.exit_loc = exit_info.ip, exit_info.loc
                    persona = Persona.for_country(target.country_code)
                    if mode in ("auto", "http"):
                        result.tiers.append(await self._tier(self.http, url, persona))
                    first = result.tiers[0].verdict.outcome if result.tiers else None
                    if needs_browser(mode, js, first):
                        result.tiers.append(await self._tier(self.browser, url, persona))
            except (TunnelError, ExitCheckError) as e:
                result.tunnel_error = str(e)
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)
            v = result.verdict
            log.info("probe %s %s -> %s %s", target.name, url, v.outcome, v.signals[:3])
            return result

    @staticmethod
    async def _tier(engine, url: str, persona: Persona) -> TierResult:
        try:
            o = await engine.fetch(url, persona)
        except Exception as e:   # engines report failures themselves; this is a backstop
            o = Observation.failed(engine.name, url, f"{type(e).__name__}: {e}")
        return TierResult(o, classify(o))

    async def reload_key(self) -> None:
        async with self.lock:   # not while a probe uses the tunnel
            await self.tunnel.reload_key(KeyFile.current())
