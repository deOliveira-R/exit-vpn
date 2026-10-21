"""The two ways to load a page (PLAN D19), behind one interface: `await engine.fetch(url,
persona) -> Observation`. Both run inside the prober's netns, so their traffic can only
leave through wg-probe; neither raises: a failure is an Observation with `error` set.

- `HttpEngine`: curl_cffi with Chrome's TLS/HTTP2 fingerprint, no JavaScript. Fast; sees
  IP-level blocks (403/451/429) and WAF challenges, not what scripts render.
- `BrowserEngine`: one persistent `chromium --headless=new` (unprivileged user, no sandbox:
  the container is the sandbox), a fresh context per load, driven over CDP.
- `Persona`: what the browser claims to be, matched to the exit country (locale, timezone).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pwd
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from .html import title_of, visible_text
from .model import Engine, Observation

log = logging.getLogger("prober")

MAX_HTML = 300_000
# Locale and timezone per exit country (ISO code), so the browser matches its IP
LOCALES = {
    "US": ("en-US", "America/New_York"), "CA": ("en-CA", "America/Toronto"),
    "GB": ("en-GB", "Europe/London"), "CH": ("de-CH", "Europe/Zurich"),
    "DE": ("de-DE", "Europe/Berlin"), "FR": ("fr-FR", "Europe/Paris"),
    "NL": ("nl-NL", "Europe/Amsterdam"), "SE": ("sv-SE", "Europe/Stockholm"),
    "IS": ("is-IS", "Atlantic/Reykjavik"), "BR": ("pt-BR", "America/Sao_Paulo"),
    "JP": ("ja-JP", "Asia/Tokyo"), "AU": ("en-AU", "Australia/Sydney"),
}


@dataclass(frozen=True)
class Persona:
    locale: str = "en-US"
    timezone: str = "UTC"

    @classmethod
    def for_country(cls, code: str) -> Persona:
        locale, tz = LOCALES.get((code or "").upper(), ("en-US", "UTC"))
        return cls(locale, tz)


class PageEngine(Protocol):
    name: Engine

    async def fetch(self, url: str, persona: Persona) -> Observation: ...


class HttpEngine:
    name = Engine.HTTP

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    async def fetch(self, url: str, persona: Persona = Persona()) -> Observation:
        from curl_cffi.requests import AsyncSession
        try:
            async with AsyncSession(impersonate="chrome", timeout=self.timeout) as s:
                r = await s.get(url, allow_redirects=True)
            html = r.text[:MAX_HTML]
            return Observation(
                engine=Engine.HTTP, requested_url=url, final_url=str(r.url), status=r.status_code,
                headers={k.lower(): v for k, v in r.headers.items()}, title=title_of(html),
                text=visible_text(html), html=html, html_len=len(r.content))
        except Exception as e:  # curl errors, TLS failures, timeouts
            return Observation.failed(Engine.HTTP, url, f"{type(e).__name__}: {e}")

    async def get_text(self, url: str, timeout: float) -> tuple[int | None, str, str]:
        """(status, body, error) of a plain GET through the current routing (the exit check)."""
        from curl_cffi.requests import AsyncSession
        try:
            async with AsyncSession(impersonate="chrome", timeout=timeout) as s:
                r = await s.get(url, allow_redirects=False)
            return r.status_code, r.text, ""
        except Exception as e:
            return None, "", f"{type(e).__name__}: {e}"[:200]


class BrowserEngine:
    name = Engine.BROWSER
    PROFILE = "/tmp/chromium-profile"

    def __init__(self, binary: str = "chromium", user: str = "browser", port: int = 9222,
                 hard_cap: float = 20.0):
        self.binary, self.user, self.port, self.hard_cap = binary, user, port, hard_cap
        self.proc: subprocess.Popen | None = None
        self._pw = None
        self._browser = None
        self.version = ""

    def _spawn(self) -> None:
        pw = pwd.getpwnam(self.user)
        shutil.rmtree(self.PROFILE, ignore_errors=True)
        os.makedirs(self.PROFILE, mode=0o700)
        os.chown(self.PROFILE, pw.pw_uid, pw.pw_gid)
        self.proc = subprocess.Popen(
            [self.binary, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--metrics-recording-only",
             f"--user-data-dir={self.PROFILE}", "--remote-debugging-address=127.0.0.1",
             f"--remote-debugging-port={self.port}", "about:blank"],
            user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], env={"HOME": self.PROFILE},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    async def _ensure(self):
        if self._browser and self._browser.is_connected() and self.proc and self.proc.poll() is None:
            return self._browser
        await self.close()
        from playwright.async_api import async_playwright
        self._spawn()
        self._pw = await async_playwright().start()
        deadline = time.monotonic() + 20
        while True:
            try:
                self._browser = await self._pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self.port}", timeout=2000)
                break
            except Exception:
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    raise RuntimeError("chromium did not start") from None
                await asyncio.sleep(0.5)
        self.version = self._browser.version
        log.info("chromium %s ready", self.version)
        return self._browser

    def user_agent(self) -> str:
        major = (self.version or "155").split(".")[0]
        # Match the real platform (navigator.platform says Linux aarch64), minus "Headless"
        return (f"Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 Safari/537.36")

    async def fetch(self, url: str, persona: Persona = Persona()) -> Observation:
        ctx = None
        try:
            browser = await self._ensure()
            ctx = await browser.new_context(user_agent=self.user_agent(), locale=persona.locale,
                                            timezone_id=persona.timezone,
                                            viewport={"width": 1366, "height": 768})
            return await asyncio.wait_for(self._load(ctx, url), self.hard_cap)
        except TimeoutError:
            return Observation.failed(Engine.BROWSER, url, "hard cap reached")
        except Exception as e:   # Chromium died, CDP refused, ...
            return Observation.failed(Engine.BROWSER, url, f"browser: {e}")
        finally:
            if ctx is not None:
                with contextlib.suppress(Exception):
                    await ctx.close()

    async def _load(self, ctx, url: str) -> Observation:
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            return Observation.failed(Engine.BROWSER, url,
                                      f"{type(e).__name__}: {str(e).splitlines()[0]}")
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)
        html = (await page.content())[:MAX_HTML]
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        headers = await resp.all_headers() if resp else {}
        return Observation(
            engine=Engine.BROWSER, requested_url=url, final_url=page.url,
            status=resp.status if resp else None, headers={k.lower(): v for k, v in headers.items()},
            title=await page.title(), text=" ".join(text.split())[:MAX_HTML], html=html,
            html_len=len(html), frame_urls=[f.url for f in page.frames])

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._browser:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw:
                await self._pw.stop()
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            proc = self.proc
            await asyncio.to_thread(proc.wait, 5)   # not on the event loop
        self._browser = self._pw = self.proc = None
