"""Fakes and builders for the prober's tests: a tunnel that records attach/detach, an exit
check, engines that return a prepared Observation (or raise), a recording command runner,
and an ASGI client for the HTTP layer. Test files import from here, never from each other."""

from __future__ import annotations

import contextlib

import httpx

from prober.api import create_app
from prober.exitcheck import ExitCheckError, ExitInfo
from prober.model import Engine, Observation, Target
from prober.probe import Prober
from prober.tunnel import TunnelError

BODY = {"server": {"name": "US-NY#1", "ip": "198.51.100.1", "pubkey": "pk=", "country_code": "US"},
        "url": "https://www.example.com/"}
TARGET = Target("US-NY#1", "198.51.100.1", "pk=", "US")
URL = "https://www.example.com/"


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
        self.name, self.raises, self.calls, self.personas = name, raises, 0, []
        self.kw = kw
        self.version = "155.0"

    async def fetch(self, url, persona):
        self.calls += 1
        self.personas.append(persona)
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


class FakeRun:
    """Stands in for the tunnel's command runner: records each command line and answers
    `wg show ... peers` and `ip addr show` with prepared text."""

    def __init__(self, peers="", addr=""):
        self.calls, self.stdin, self.peers, self.addr = [], [], peers, addr

    async def __call__(self, *args, stdin=None):
        self.calls.append(args)
        self.stdin.append(stdin)
        if args[:2] == ("wg", "show"):
            return self.peers
        if args[:2] == ("ip", "-4"):
            return self.addr
        return ""


async def request(p: Prober, method: str, path: str, body=None) -> httpx.Response:
    """One request to the prober's HTTP layer, in process."""
    app = create_app(p, p.browser)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(method, path, json=body)


def trace_fetch(*answers):
    """An ExitCheck fetch that gives `answers` in turn, then repeats the last one."""
    queue = list(answers)

    async def fetch(url, timeout):
        return queue.pop(0) if len(queue) > 1 else queue[0]
    return fetch
