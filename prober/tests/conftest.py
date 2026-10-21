"""Fakes for the prober's parts: a tunnel that records attach/detach, an exit check, and
engines that return a prepared Observation (or raise)."""

from __future__ import annotations

import contextlib

from prober.exitcheck import ExitCheckError, ExitInfo
from prober.model import Engine, Observation
from prober.probe import Prober
from prober.tunnel import TunnelError


class FakeTunnel:
    def __init__(self, attach_fails=None):
        self.attach_fails = attach_fails
        self.attached, self.detached, self.keys = [], 0, []

    @contextlib.asynccontextmanager
    async def session(self, target):
        try:
            if self.attach_fails:
                raise TunnelError(self.attach_fails)
            self.attached.append((target.pubkey, target.ip))
            yield
        finally:
            self.detached += 1

    async def reload_key(self, key):
        self.keys.append(key)


class FakeExitCheck:
    def __init__(self, fail=None):
        self.fail = fail

    async def verify(self, expect_code, forbid_ips):
        if self.fail:
            raise ExitCheckError(self.fail)
        return ExitInfo("203.0.113.7", expect_code or "US")


class FakeEngine:
    def __init__(self, name, raises=None, **kw):
        self.name, self.raises, self.calls = name, raises, 0
        self.kw = kw
        self.version = "155.0"

    async def fetch(self, url, persona):
        self.calls += 1
        if self.raises:
            raise self.raises
        base = dict(engine=self.name, requested_url=url, final_url=url, status=200, title="T",
                    text="x", html="<html/>", html_len=50_000 if self.name == Engine.HTTP else 90_000)
        base.update(self.kw)
        return Observation(**base)

    async def close(self):
        pass


def http(**kw):
    return FakeEngine(Engine.HTTP, **kw)


def browser(**kw):
    return FakeEngine(Engine.BROWSER, title="Real page", text="content", **kw)


def prober(tunnel=None, exit_check=None, http_engine=None, browser_engine=None) -> Prober:
    return Prober(tunnel or FakeTunnel(), exit_check or FakeExitCheck(), http_engine or http(),
                  browser_engine or browser())
