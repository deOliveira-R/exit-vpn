"""Key slots (Phase 3b): validation, try-then-keep-or-roll-back, views, no key echoes."""

import base64

import pytest
from fastapi.testclient import TestClient

from exit_vpn.api import create_app
from exit_vpn.keys import KeyRejected, load_text
from exit_vpn.slots import KeyFailed
from exit_vpn.wgconf import WgConf

from harness import AllowAll, conf, fake_pubkey, new_key


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


async def test_slot_a_good_key_is_kept(rig):
    wg, store, slots = rig.wg, rig.keys, rig.node.slots
    await rig.svc.start()
    assert store.source("a") == "bootstrap"
    k = new_key()
    view = await slots.put("a", conf(k, netshield="1"), {"provider": "proton",
                           "cert_id": "fp1", "expires_at": 4_000_000_000}, "o@github")
    assert wg.key == k and store.source("a") == "uploaded"
    assert store.current("a").private_key == k
    assert view["options"]["NetShield"] == "1" and view["meta"]["cert_id"] == "fp1"
    assert view["meta"]["uploaded_by"] == "o@github"
    assert rig.svc.exit_conf.options["NetShield"] == "1"
    assert rig.svc.snapshot()["key_options"]["NetShield"] == "1"
    assert "key_replaced" in rig.kinds()


async def test_slot_a_dead_key_rolls_back(rig):
    wg, store, slots, boot_a = rig.wg, rig.keys, rig.node.slots, rig.boot_a
    await rig.svc.start()
    bad = new_key()
    wg.dead_keys.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("a", conf(bad), {}, "o@github")
    assert wg.keys == [boot_a, bad, boot_a]
    assert store.source("a") == "bootstrap"
    assert "key_rejected" in rig.kinds()


async def test_slot_a_second_upload_keeps_prev(rig):
    store, slots = rig.keys, rig.node.slots
    await rig.svc.start()
    k1, k2 = new_key(), new_key()
    await slots.put("a", conf(k1), {}, "o@github")
    await slots.put("a", conf(k2), {}, "o@github")
    assert store.current("a").private_key == k2
    assert k1 in (store.dirs["a"] / "wg.conf.prev").read_text()
    assert oct((store.dirs["a"] / "wg.conf").stat().st_mode & 0o777) == "0o600"


async def test_slot_b_good_and_bad(rig):
    store, slots, prober = rig.keys, rig.node.slots, rig.prober
    await rig.svc.start()
    k = new_key()
    await slots.put("b", conf(k), {"cert_id": "fp-b"}, "o@github")
    assert prober.key == k and store.source("b") == "uploaded"
    bad = new_key()
    prober.dead.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("b", conf(bad), {"cert_id": "fp-bad"}, "o@github")
    assert prober.key == k and store.current("b").private_key == k
    assert store.meta("b")["cert_id"] == "fp-b"


async def test_slot_b_first_upload_failure_returns_to_bootstrap(rig):
    store, slots, prober = rig.keys, rig.node.slots, rig.prober
    await rig.svc.start()
    bad = new_key()
    prober.dead.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("b", conf(bad), {}, "o@github")
    assert store.source("b") == "bootstrap"


async def test_expiry_warning(rig):
    slots = rig.node.slots
    await rig.svc.start()
    await slots.put("a", conf(new_key()), {"expires_at": int(rig.clock()) + 5 * 86400 + 60}, "o@github")
    assert rig.svc.snapshot()["warnings"] == ["slot a key certificate expires in 5 days"]


async def test_api_never_echoes_the_key(rig):
    await rig.svc.start()
    app = create_app(rig.node, AllowAll(), ["o@github"])
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


async def test_meta_update_only_for_the_slots_key(rig):
    slots, boot_a = rig.node.slots, rig.boot_a
    await rig.svc.start()
    pub = await fake_pubkey(boot_a)
    with pytest.raises(KeyRejected):
        await slots.put_meta("a", {"wg_public_key": "someone else", "expires_at": 1}, "o@github")
    view = await slots.put_meta("a", {"wg_public_key": pub, "provider": "proton",
                                      "expires_at": 4_000_000_000}, "o@github")
    assert view["source"] == "bootstrap" and view["meta"]["provider"] == "proton"
    # a new key makes the old facts irrelevant
    await slots.put("a", conf(new_key()), {"provider": "manual"}, "o@github")
    assert (await slots.view("a"))["meta"]["provider"] == "manual"


async def test_accepted_keys_are_mirrored_to_bootstrap(rig):
    wg, store, slots, prober = rig.wg, rig.keys, rig.node.slots, rig.prober
    await rig.svc.start()
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
    wg.dead_keys.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("a", conf(bad), {}, "o@github")
    assert k in store.bootstrap["a"].read_text()    # never committed
