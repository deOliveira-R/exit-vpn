"""The two probe tiers (PLAN D19). Both run inside the prober's netns, so all their
traffic can only leave through wg-probe."""

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

from .classify import Observation, title_of, visible_text

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


def locale_for(code: str) -> tuple[str, str]:
    return LOCALES.get((code or "").upper(), ("en-US", "UTC"))


async def http_probe(url: str, timeout: float = 15.0) -> Observation:
    """Tier 1: curl_cffi with Chrome's TLS/HTTP2 fingerprint, no JS."""
    from curl_cffi.requests import AsyncSession
    try:
        async with AsyncSession(impersonate="chrome", timeout=timeout) as s:
            r = await s.get(url, allow_redirects=True)
        html = r.text[:MAX_HTML]
        return Observation(
            engine="http", requested_url=url, final_url=str(r.url), status=r.status_code,
            headers={k.lower(): v for k, v in r.headers.items()}, title=title_of(html),
            text=visible_text(html), html=html, html_len=len(r.content))
    except Exception as e:  # curl errors, TLS failures, timeouts
        return Observation(engine="http", requested_url=url, error=f"{type(e).__name__}: {e}"[:200])


class Browser:
    """Tier 2: one persistent `chromium --headless=new` (unprivileged user, no sandbox:
    the container is the sandbox), a fresh context per probe, driven over CDP."""

    def __init__(self, binary: str = "chromium", user: str = "browser", port: int = 9222):
        self.binary, self.user, self.port = binary, user, port
        self.proc: subprocess.Popen | None = None
        self._pw = None
        self._browser = None
        self.version = ""

    def _spawn(self) -> None:
        pw = pwd.getpwnam(self.user)
        profile = "/tmp/chromium-profile"
        shutil.rmtree(profile, ignore_errors=True)
        os.makedirs(profile, mode=0o700)
        os.chown(profile, pw.pw_uid, pw.pw_gid)
        self.proc = subprocess.Popen(
            [self.binary, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--metrics-recording-only",
             f"--user-data-dir={profile}", "--remote-debugging-address=127.0.0.1",
             f"--remote-debugging-port={self.port}", "about:blank"],
            user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], env={"HOME": profile},
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

    async def probe(self, url: str, country: str, hard_cap: float = 20.0) -> Observation:
        try:
            browser = await self._ensure()
        except Exception as e:
            return Observation(engine="browser", requested_url=url, error=f"browser: {e}"[:200])
        locale, tz = locale_for(country)
        ctx = await browser.new_context(user_agent=self.user_agent(), locale=locale,
                                        timezone_id=tz, viewport={"width": 1366, "height": 768})
        try:
            return await asyncio.wait_for(self._load(ctx, url), hard_cap)
        except TimeoutError:
            return Observation(engine="browser", requested_url=url, error="hard cap reached")
        finally:
            with contextlib.suppress(Exception):
                await ctx.close()

    async def _load(self, ctx, url: str) -> Observation:
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            return Observation(engine="browser", requested_url=url,
                               error=f"{type(e).__name__}: {str(e).splitlines()[0]}"[:200])
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)
        html = (await page.content())[:MAX_HTML]
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        headers = await resp.all_headers() if resp else {}
        return Observation(
            engine="browser", requested_url=url, final_url=page.url,
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
            self.proc.wait(timeout=5)
        self._browser = self._pw = self.proc = None
