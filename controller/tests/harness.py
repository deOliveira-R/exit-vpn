"""Test harness: a controller node built by `node.build`, as in production, with fake
tunnels (wg0; with `standby=True` also wg1), fake health probes, a fake route and a fake
clock. Traffic works unless the peer's public key or the interface's private key is marked
dead, or the uplink is down (`World`, shared by both tunnels); IPv6 works too unless the peer
is marked as without IPv6. Also the builders and fakes the tests share:
WireGuard configs and keys, pushed catalog rows, a scripted prober, an owner-only whois."""

from __future__ import annotations

import asyncio
import base64
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from exit_vpn.api import create_app
from exit_vpn.config import Settings
from exit_vpn.keys import KeyStore
from exit_vpn.node import Node, build
from exit_vpn.probes import CycleResult
from exit_vpn.servers import Server
from exit_vpn.wg import Peer
from exit_vpn.wgconf import WgConf


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class World:
    """What both fake tunnels see: dead servers and keys, servers without IPv6, the uplink."""

    def __init__(self):
        self.dead_peers, self.dead_keys = set(), set()
        self.no_ipv6 = set()   # peers whose IPv6 drops (servers without IPv6)
        self.uplink_down = False


class FakeWg:
    def __init__(self, clock, key, pubkey, endpoint, ifname="wg0", world=None):
        self.clock, self.key, self.ifname = clock, key, ifname
        self.peer = Peer(pubkey, endpoint, int(clock())) if pubkey else None
        self.swaps, self.keys = [], [key]
        self.world = world or World()
        self.dead_peers, self.dead_keys = self.world.dead_peers, self.world.dead_keys
        self.no_ipv6 = self.world.no_ipv6

    async def peers(self):
        return [self.peer] if self.peer else []

    def works(self) -> bool:
        return (self.peer is not None and self.key is not None and not self.world.uplink_down
                and self.peer.pubkey not in self.dead_peers and self.key not in self.dead_keys)

    async def swap_peer(self, pubkey, endpoint):
        self.swaps.append(pubkey)
        self.peer = Peer(pubkey, endpoint, int(self.clock()))

    async def set_private_key(self, key):
        self.key = key
        self.keys.append(key)


class FakeHealth:
    """Each cycle takes a second (so warm-up deadlines are reached). `source6` empty: no
    IPv6 probe, as for a config without an IPv6 address."""

    def __init__(self, wg, clock, source6="2a07:b944::2:2"):
        self.wg, self.clock, self.source6 = wg, clock, source6
        self.ipv6_on, self.device = True, wg.ifname

    async def cycle(self, latest_handshake, now=None):
        self.clock.t += 1.0
        ok = self.wg.works()
        v6 = (ok and self.wg.peer.pubkey not in self.wg.no_ipv6) if self.source6 and self.ipv6_on else None
        return CycleResult(ok, ok, ok, 1.0, "9.9.9.9" if ok else None, "US" if ok else None,
                           [] if ok else ["https: timeout"], v6, "2001:db8::9" if v6 else None,
                           "" if v6 or v6 is None else "ipv6: ConnectTimeout")

    def reset_ipv6(self):
        pass


class FakeRoute:
    """The handover's route switch: records which tunnel table 100 points at."""

    def __init__(self):
        self.active, self.sets, self.ended = None, [], []

    def current(self):
        return self.active

    async def set(self, ifname):
        self.active = ifname
        self.sets.append(ifname)

    async def end_flows(self, old_ifname):
        self.ended.append(old_ifname)
        return 2


class FakeProberClient:
    def __init__(self, keys: KeyStore, answer: dict | None = None):
        self.keys, self.answer, self.reloads, self.dead, self.key = keys, answer, 0, set(), None
        self.probes = []

    async def reload_key(self):
        self.reloads += 1
        self.key = self.keys.current("b").private_key
        return {"ok": True}

    async def probe(self, srv, *, country_code, url, mode="auto", js=False, forbid_ips=()):
        self.probes.append((srv.name, country_code, tuple(forbid_ips)))
        if self.answer is not None:
            return {**self.answer, "server": srv.name}
        return {"server": srv.name, "outcome": "error" if self.key in self.dead else "ok",
                "signals": []}


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def conf(key: str, addr="10.2.0.2/32", dns="10.2.0.1", netshield="0") -> str:
    return (f"[Interface]\n# Key for exit-proton-a\n# NetShield = {netshield}\n"
            f"PrivateKey = {key}\nAddress = {addr}\nDNS = {dns}\n\n"
            "[Peer]\n# US-NY#1\nPublicKey = cGVlcnB1YmtleXBlZXJwdWJrZXlwZWVycHViazE=\n"
            "AllowedIPs = 0.0.0.0/0\nEndpoint = 192.0.2.10:51820\n")


async def fake_pubkey(private: str) -> str:
    return "pub:" + private[:6]


async def no_sleep(_seconds):
    return None


@dataclass
class Rig:
    node: Node
    wg: FakeWg
    clock: Clock
    start: Server
    keys: KeyStore
    prober: FakeProberClient
    boot_a: str
    settings: Settings
    wg1: FakeWg | None = None
    route: FakeRoute | None = None
    catalog: object = None

    @property
    def world(self) -> World:
        return self.wg.world

    @property
    def svc(self):
        return self.node.service

    def kinds(self, ipv6: bool = False) -> list[str]:
        """The event kinds so far; `ipv6` events, which ride along most changes, only if asked."""
        return [e["kind"] for e in self.node.events.recent() if ipv6 or e["kind"] != "ipv6"]

    def rebuild(self, catalog) -> Rig:
        """The same state directory and tunnels, as after a controller restart."""
        node = build(self.settings, exit_conf=self.svc.exit_conf, wg=self.wg,
                     probe=FakeHealth(self.wg, self.clock), catalog=catalog, keys=self.keys,
                     prober=self.prober, clock=self.clock, rng=random.Random(0),
                     pubkey=fake_pubkey, **_standby_parts(self.wg1, self.clock, self.route))
        return Rig(node, self.wg, self.clock, self.start, self.keys, self.prober, self.boot_a,
                   self.settings, self.wg1, self.route, catalog)


def _standby_parts(wg1, clock, route) -> dict:
    if wg1 is None:
        return {}
    return {"standby_wg": wg1, "standby_probe": FakeHealth(wg1, clock), "route": route}


def make(catalog, tmp_path: Path, monkeypatch, prober_answer: dict | None = None,
         standby: bool = False, standby_key: bool = True) -> Rig:
    """A node on New York's first server. `standby`: with the second tunnel wg1 (keyed with
    slot c's bootstrap key unless `standby_key` is False), which init-wg left on the same
    server as wg0 (its config's peer), so the standby starts by searching."""
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    clock = Clock()
    start = catalog.filter("United States", "New York")[0]
    boot_a, boot_b, boot_c = new_key(), new_key(), new_key()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/a.conf").write_text(conf(boot_a))
    (tmp_path / "secrets/b.conf").write_text(conf(boot_b))
    if standby and standby_key:
        (tmp_path / "secrets/c.conf").write_text(conf(boot_c))
    s = Settings(vpn_conf=tmp_path / "secrets/a.conf", state_dir=tmp_path / "state",
                 owners=["o@github"], probe_conf=tmp_path / "secrets/b.conf",
                 standby_conf=tmp_path / "secrets/c.conf",
                 key_dirs={"a": tmp_path / "keys-a", "b": tmp_path / "keys-b",
                           "c": tmp_path / "keys-c"},
                 ipv6_gate_file=tmp_path / "run/ipv6", active_file=tmp_path / "run/active")
    keys = KeyStore(s.key_dirs, {"a": s.vpn_conf, "b": s.probe_conf, "c": s.standby_conf})
    world = World()
    wg = FakeWg(clock, boot_a, start.pubkey, start.endpoint(51820), "wg0", world)
    wg1 = route = None
    if standby:
        wg1 = (FakeWg(clock, boot_c, start.pubkey, start.endpoint(51820), "wg1", world)
               if standby_key else FakeWg(clock, None, None, None, "wg1", world))
        route = FakeRoute()
    prober = FakeProberClient(keys, prober_answer)
    node = build(s, exit_conf=WgConf("10.2.0.2", "10.2.0.1", start.pubkey, start.endpoint(51820)),
                 wg=wg, probe=FakeHealth(wg, clock), catalog=catalog, keys=keys, prober=prober,
                 clock=clock, rng=random.Random(0), pubkey=fake_pubkey,
                 **_standby_parts(wg1, clock, route))
    return Rig(node, wg, clock, start, keys, prober, boot_a, s, wg1, route, catalog)


class AllowAll:
    """A whois that says every caller is the owner."""

    async def lookup(self, ip, port):
        return {"UserProfile": {"LoginName": "o@github"}, "Node": {"Tags": []}}


def prefs_for(countries, **kw):
    """Prefs pinned to these countries (regions given as Region objects go in `regions`)."""
    from exit_vpn.prefs import Prefs
    from exit_vpn.servers import Region
    kw = {k: tuple(v) if isinstance(v, list) else v for k, v in kw.items()}
    return Prefs(regions=kw.pop("regions", tuple(Region(c) for c in countries)), **kw)


class Stranger:
    """A whois that knows the caller, who is not an owner."""

    async def lookup(self, ip, port):
        return {"UserProfile": {"LoginName": "eve@github"}, "Node": {"Tags": []}}


def client(rig: Rig, whois=None) -> TestClient:
    """The controller's HTTP API for this rig; every caller is the owner unless `whois` says."""
    return TestClient(create_app(rig.node, whois or AllowAll(), ["o@github"]))


async def strikes(rig: Rig, n: int = 3) -> None:
    """`n` failed health cycles, past the warm-up."""
    rig.clock.t += 30
    for _ in range(n):
        await rig.svc.step()


# Find jobs ------------------------------------------------------------------------------
REAL_SLEEP = asyncio.sleep   # `make` patches asyncio.sleep to return at once


class ScriptedProber:
    """A prober client answering each probe with the next outcome in `outcomes`."""

    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    async def probe(self, srv, *, country_code, url, mode="auto", js=False, forbid_ips=()):
        self.calls.append((srv.name, country_code, tuple(forbid_ips)))
        return {"server": srv.name, "outcome": self.outcomes.pop(0), "signals": []}


async def job_done(job) -> None:
    for _ in range(200):
        if job.state != "running":
            return
        await REAL_SLEEP(0.01)


# Pushed catalogs ------------------------------------------------------------------------
def pushed_rows(n: int = 60, **over) -> list[dict]:
    """A pushed server list (exitctl's rows): New York servers, every tenth in maintenance."""
    out = []
    for i in range(n):
        r = {"name": f"US-NY#{i}", "country_code": "US", "city": "New York",
             "hostname": f"node-us-{i:02d}.protonvpn.net", "ip": f"185.159.157.{i + 1}",
             "pubkey": new_key(), "load": (i * 7) % 100, "enabled": i % 10 != 0}
        r.update(over)
        out.append(r)
    return out


def country_names(code: str) -> str | None:
    return {"US": "United States"}.get(code)


# Contracts ------------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "contracts"))
import shape  # noqa: E402,F401  (the samples in contracts/)

SRC = Path(__file__).resolve().parents[1] / "src" / "exit_vpn"


def emitted_kinds() -> set[str]:
    """Event kinds the controller's code can emit (every `publish("kind"` call)."""
    import re
    return {m for f in SRC.glob("*.py") for m in re.findall(r'\bpublish\(\s*"([a-z0-9_]+)"', f.read_text())}


KINDS = emitted_kinds()
