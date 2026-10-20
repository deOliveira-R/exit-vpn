"""Key slots (Phase 3b): validation, try-then-keep-or-roll-back, views, no key echoes."""

import base64
import os
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exit_vpn.api import create_app
from exit_vpn.config import Settings
from exit_vpn.controller import Controller
from exit_vpn.keys import KeyRejected, KeyStore, load_text
from exit_vpn.probes import CycleResult
from exit_vpn.slots import KeyFailed, SlotManager
from exit_vpn.state import StateStore
from exit_vpn.wg import Peer
from exit_vpn.wgconf import WgConf

from test_controller import Clock


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def conf(key: str, addr="10.2.0.2/32", dns="10.2.0.1", netshield="0") -> str:
    return (f"[Interface]\n# Key for exit-proton-a\n# NetShield = {netshield}\n"
            f"PrivateKey = {key}\nAddress = {addr}\nDNS = {dns}\n\n"
            "[Peer]\n# US-NY#1\nPublicKey = cGVlcnB1YmtleXBlZXJwdWJrZXlwZWVycHViazE=\n"
            "AllowedIPs = 0.0.0.0/0\nEndpoint = 192.0.2.10:51820\n")


class KeyWg:
    """A tunnel whose traffic works unless its private key is in `dead`."""

    def __init__(self, clock, key, peer):
        self.clock, self.key, self.peer, self.dead = clock, key, peer, set()
        self.keys = [key]

    async def peers(self):
        return [self.peer]

    async def swap_peer(self, pubkey, endpoint):
        self.peer = Peer(pubkey, endpoint, int(self.clock()), 0, 0)

    async def set_private_key(self, key):
        self.key = key
        self.keys.append(key)


class KeyProbes:
    def __init__(self, wg, clock):
        self.wg, self.clock = wg, clock

    async def cycle(self, latest_handshake, now=None):
        self.clock.t += 1.0
        ok = self.wg.key not in self.wg.dead
        return CycleResult(ok, ok, ok, 1.0, "9.9.9.9" if ok else None, "US" if ok else None,
                           [] if ok else ["https: timeout"])


class FakeProberClient:
    def __init__(self, store):
        self.store, self.reloads, self.dead = store, 0, set()

    async def reload_key(self):
        self.reloads += 1
        self.key = self.store.current("b").private_key
        return {"ok": True}

    async def probe(self, srv, code, url, mode, js, forbid):
        return {"outcome": "error" if self.key in self.dead else "ok"}


async def fake_pubkey(private):
    return "pub:" + private[:6]


@pytest.fixture
def env(catalog, tmp_path, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr("exit_vpn.controller.asyncio.sleep", no_sleep)
    clock = Clock()
    start = catalog.filter("United States", "New York")[0]
    boot_a, boot_b = new_key(), new_key()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/a.conf").write_text(conf(boot_a))
    (tmp_path / "secrets/b.conf").write_text(conf(boot_b))
    s = Settings(vpn_conf=tmp_path / "secrets/a.conf", state_dir=tmp_path / "state",
                 owners=["o@github"], probe_conf=tmp_path / "secrets/b.conf",
                 key_dirs={"a": tmp_path / "keys-a", "b": tmp_path / "keys-b"})
    wg = KeyWg(clock, boot_a, Peer(start.pubkey, start.endpoint(51820), int(clock()), 0, 0))
    ctl = Controller(s, WgConf("10.2.0.2", "10.2.0.1", start.pubkey, start.endpoint(51820)),
                     wg, KeyProbes(wg, clock), catalog,
                     StateStore(s.state_file, s.peer_env_file, s.countries),
                     clock=clock, rng=random.Random(0))
    store = KeyStore(s.key_dirs, {"a": s.vpn_conf, "b": s.probe_conf})
    prober = FakeProberClient(store)
    slots = SlotManager(ctl, store, prober, pubkey=fake_pubkey)
    return ctl, wg, store, slots, prober, boot_a


def test_validation():
    k = new_key()
    assert load_text(conf(k)).private_key == k
    like = WgConf("10.2.0.2", "10.2.0.1", "p", "e")
    with pytest.raises(KeyRejected, match="differ"):
        load_text(conf(k, addr="10.2.0.3/32"), like)
    with pytest.raises(KeyRejected, match="32 bytes"):
        load_text(conf(base64.b64encode(b"short").decode()))
    with pytest.raises(KeyRejected, match="base64"):
        load_text(conf("not!base64"))
    with pytest.raises(KeyRejected, match="missing"):
        load_text("[Interface]\nPrivateKey = " + k + "\n")
    assert k not in repr(load_text(conf(k)))


async def test_slot_a_good_key_is_kept(env):
    ctl, wg, store, slots, _, boot_a = env
    await ctl.init()
    assert store.source("a") == "bootstrap"
    k = new_key()
    view = await slots.put("a", conf(k, netshield="1"), {"provider": "proton",
                           "cert_id": "fp1", "expires_at": 4_000_000_000}, "o@github")
    assert wg.key == k and store.source("a") == "uploaded"
    assert store.current("a").private_key == k
    assert view["options"]["NetShield"] == "1" and view["meta"]["cert_id"] == "fp1"
    assert view["meta"]["uploaded_by"] == "o@github"
    assert ctl.conf.options["NetShield"] == "1"
    assert ctl.status()["key_options"]["NetShield"] == "1"
    assert any(e["kind"] == "key_replaced" for e in ctl.state.events)


async def test_slot_a_dead_key_rolls_back(env):
    ctl, wg, store, slots, _, boot_a = env
    await ctl.init()
    bad = new_key()
    wg.dead.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("a", conf(bad), {}, "o@github")
    assert wg.keys == [boot_a, bad, boot_a]
    assert store.source("a") == "bootstrap"
    assert any(e["kind"] == "key_rejected" for e in ctl.state.events)


async def test_slot_a_second_upload_keeps_prev(env):
    ctl, wg, store, slots, _, _ = env
    await ctl.init()
    k1, k2 = new_key(), new_key()
    await slots.put("a", conf(k1), {}, "o@github")
    await slots.put("a", conf(k2), {}, "o@github")
    assert store.current("a").private_key == k2
    assert k1 in (store.dirs["a"] / "wg.conf.prev").read_text()
    assert oct((store.dirs["a"] / "wg.conf").stat().st_mode & 0o777) == "0o600"


async def test_slot_b_good_and_bad(env):
    ctl, wg, store, slots, prober, _ = env
    await ctl.init()
    k = new_key()
    await slots.put("b", conf(k), {"cert_id": "fp-b"}, "o@github")
    assert prober.key == k and store.source("b") == "uploaded"
    bad = new_key()
    prober.dead.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("b", conf(bad), {"cert_id": "fp-bad"}, "o@github")
    assert prober.key == k and store.current("b").private_key == k
    assert store.meta("b")["cert_id"] == "fp-b"


async def test_slot_b_first_upload_failure_returns_to_bootstrap(env):
    ctl, wg, store, slots, prober, _ = env
    await ctl.init()
    bad = new_key()
    prober.dead.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("b", conf(bad), {}, "o@github")
    assert store.source("b") == "bootstrap"


async def test_expiry_warning(env):
    ctl, wg, store, slots, _, _ = env
    await ctl.init()
    import time
    await slots.put("a", conf(new_key()), {"expires_at": int(time.time()) + 5 * 86400}, "o@github")
    assert ctl.status()["warnings"] == ["slot a key certificate expires in 5 days"]


class AllowAll:
    async def lookup(self, ip, port):
        return {"UserProfile": {"LoginName": "o@github"}, "Node": {"Tags": []}}


async def test_api_never_echoes_the_key(env):
    ctl, wg, store, slots, _, _ = env
    await ctl.init()
    app = create_app(ctl, AllowAll(), ["o@github"], slots=slots)
    k = new_key()
    with TestClient(app) as c:
        # schema violation (meta wrong type): default FastAPI would echo the body
        r = c.put("/v1/keys/a", json={"conf": conf(k), "meta": "x"})
        assert r.status_code == 422 and k not in r.text
        r = c.put("/v1/keys/a", json={"conf": conf(k, addr="10.9.9.9/32")})
        assert r.status_code == 400 and k not in r.text
        r = c.put("/v1/keys/z", json={"conf": conf(k)})
        assert r.status_code == 400
        r = c.get("/v1/keys")
        assert r.status_code == 200 and k not in r.text
        assert [s["source"] for s in r.json()["slots"]] == ["bootstrap", "bootstrap"]


async def test_meta_update_only_for_the_slots_key(env):
    ctl, wg, store, slots, _, boot_a = env
    await ctl.init()
    pub = await fake_pubkey(boot_a)
    with pytest.raises(KeyRejected):
        await slots.put_meta("a", {"wg_public_key": "someone else", "expires_at": 1}, "o@github")
    view = await slots.put_meta("a", {"wg_public_key": pub, "provider": "proton",
                                      "expires_at": 4_000_000_000}, "o@github")
    assert view["source"] == "bootstrap" and view["meta"]["provider"] == "proton"
    # a new key makes the old facts irrelevant
    await slots.put("a", conf(new_key()), {"provider": "manual"}, "o@github")
    assert (await slots.view("a"))["meta"]["provider"] == "manual"


async def test_accepted_keys_are_mirrored_to_bootstrap(env):
    ctl, wg, store, slots, prober, _ = env
    await ctl.init()
    k = new_key()
    await slots.put("a", conf(k), {}, "o@github")
    assert k in store.bootstrap["a"].read_text()
    kb = new_key()
    await slots.put("b", conf(kb), {}, "o@github")
    assert kb in store.bootstrap["b"].read_text()
    bad = new_key()
    prober.dead.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("b", conf(bad), {}, "o@github")
    assert kb in store.bootstrap["b"].read_text()   # the mirror is rolled back too
    wg.dead.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("a", conf(bad), {}, "o@github")
    assert k in store.bootstrap["a"].read_text()    # never committed
