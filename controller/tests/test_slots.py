"""Key slots: an uploaded key is validated, tried, then kept or rolled back, and mirrored
to the bootstrap files (component)."""

import base64

import pytest

from exit_vpn.keys import KeyRejected, load_text
from exit_vpn.slots import KeyFailed
from exit_vpn.wgconf import WgConf

from harness import conf, fake_pubkey, new_key


def test_an_uploaded_config_is_validated_and_never_shows_its_key():
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


async def test_a_working_exit_key_is_kept_with_its_options_and_facts(rig):
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


async def test_an_exit_key_that_carries_no_traffic_is_rolled_back(rig):
    wg, store, slots, boot_a = rig.wg, rig.keys, rig.node.slots, rig.boot_a
    await rig.svc.start()
    bad = new_key()
    wg.dead_keys.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("a", conf(bad), {}, "o@github")
    assert wg.keys == [boot_a, bad, boot_a]
    assert store.source("a") == "bootstrap"
    assert "key_rejected" in rig.kinds()


async def test_a_second_exit_key_keeps_the_first_as_prev(rig):
    store, slots = rig.keys, rig.node.slots
    await rig.svc.start()
    k1, k2 = new_key(), new_key()
    await slots.put("a", conf(k1), {}, "o@github")
    await slots.put("a", conf(k2), {}, "o@github")
    assert store.current("a").private_key == k2
    assert k1 in (store.dirs["a"] / "wg.conf.prev").read_text()
    assert oct((store.dirs["a"] / "wg.conf").stat().st_mode & 0o777) == "0o600"


async def test_a_prober_key_is_kept_only_if_the_prober_works_with_it(rig):
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


async def test_a_failed_first_prober_key_returns_to_the_bootstrap_key(rig):
    store, slots, prober = rig.keys, rig.node.slots, rig.prober
    await rig.svc.start()
    bad = new_key()
    prober.dead.add(bad)
    with pytest.raises(KeyFailed):
        await slots.put("b", conf(bad), {}, "o@github")
    assert store.source("b") == "bootstrap"


async def test_a_key_near_expiry_is_a_status_warning(rig):
    slots = rig.node.slots
    await rig.svc.start()
    await slots.put("a", conf(new_key()), {"expires_at": int(rig.clock()) + 5 * 86400 + 60}, "o@github")
    assert rig.svc.snapshot()["warnings"] == ["slot a key certificate expires in 5 days"]


async def test_key_facts_are_updated_only_for_the_slots_own_key(rig):
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


async def test_accepted_keys_are_mirrored_to_the_bootstrap_files(rig):
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
