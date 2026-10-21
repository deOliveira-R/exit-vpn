"""Test harness: a controller node built by `node.build`, as in production, with a fake
tunnel, fake health probes and a fake clock. Traffic works unless the peer's public key or
the interface's private key is marked dead."""

from __future__ import annotations

import asyncio
import base64
import os
import random
from dataclasses import dataclass
from pathlib import Path

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


class FakeWg:
    ifname = "wg0"

    def __init__(self, clock, key, pubkey, endpoint):
        self.clock, self.key = clock, key
        self.peer = Peer(pubkey, endpoint, int(clock()))
        self.swaps, self.keys = [], [key]
        self.dead_peers, self.dead_keys = set(), set()

    async def peers(self):
        return [self.peer]

    async def swap_peer(self, pubkey, endpoint):
        self.swaps.append(pubkey)
        self.peer = Peer(pubkey, endpoint, int(self.clock()))

    async def set_private_key(self, key):
        self.key = key
        self.keys.append(key)


class FakeHealth:
    """Each cycle takes a second (so warm-up deadlines are reached)."""

    def __init__(self, wg, clock):
        self.wg, self.clock = wg, clock

    async def cycle(self, latest_handshake, now=None):
        self.clock.t += 1.0
        ok = self.wg.peer.pubkey not in self.wg.dead_peers and self.wg.key not in self.wg.dead_keys
        return CycleResult(ok, ok, ok, 1.0, "9.9.9.9" if ok else None, "US" if ok else None,
                           [] if ok else ["https: timeout"])


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

    @property
    def svc(self):
        return self.node.service

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.node.events.recent()]

    def rebuild(self, catalog) -> Rig:
        """The same state directory and tunnel, as after a controller restart."""
        node = build(self.settings, exit_conf=self.svc.exit_conf, wg=self.wg,
                     probe=FakeHealth(self.wg, self.clock), catalog=catalog, keys=self.keys,
                     prober=self.prober, clock=self.clock, rng=random.Random(0),
                     pubkey=fake_pubkey)
        return Rig(node, self.wg, self.clock, self.start, self.keys, self.prober, self.boot_a,
                   self.settings)


def make(catalog, tmp_path: Path, monkeypatch, prober_answer: dict | None = None) -> Rig:
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    clock = Clock()
    start = catalog.filter("United States", "New York")[0]
    boot_a, boot_b = new_key(), new_key()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/a.conf").write_text(conf(boot_a))
    (tmp_path / "secrets/b.conf").write_text(conf(boot_b))
    s = Settings(vpn_conf=tmp_path / "secrets/a.conf", state_dir=tmp_path / "state",
                 owners=["o@github"], probe_conf=tmp_path / "secrets/b.conf",
                 key_dirs={"a": tmp_path / "keys-a", "b": tmp_path / "keys-b"})
    keys = KeyStore(s.key_dirs, {"a": s.vpn_conf, "b": s.probe_conf})
    wg = FakeWg(clock, boot_a, start.pubkey, start.endpoint(51820))
    prober = FakeProberClient(keys, prober_answer)
    node = build(s, exit_conf=WgConf("10.2.0.2", "10.2.0.1", start.pubkey, start.endpoint(51820)),
                 wg=wg, probe=FakeHealth(wg, clock), catalog=catalog, keys=keys, prober=prober,
                 clock=clock, rng=random.Random(0), pubkey=fake_pubkey)
    return Rig(node, wg, clock, start, keys, prober, boot_a, s)


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
