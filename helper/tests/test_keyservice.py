"""Rotating, renewing and listing keys across Proton and the exit node (component)."""

import pytest
from harness import PEER, FakeExitNode

from exitctl.keyservice import KeyService
from exitctl.model import NodeError


async def dashboard_key(p, options=None):
    """A key made outside exitctl (it stands in for one from Proton's dashboard)."""
    return await p.create_key("dashboard-key", options or {}, PEER)


async def test_rotate_deletes_the_old_certificate_once_the_node_keeps_the_new_key(logged_in):
    p, sess = logged_in
    old = await dashboard_key(p)
    out = await KeyService(p, FakeExitNode(slot_pub=old.meta.wg_public_key)).rotate("a", {"netshield": 2})
    assert out["deleted_old"] == "fp0"
    assert [c["ClientKeyFingerprint"] for c in sess.certs] == ["fp1"]


async def test_rotate_records_the_merged_levers_and_a_device_name_for_the_slot(logged_in):
    p, _ = logged_in
    old = await dashboard_key(p)
    node = FakeExitNode(slot_pub=old.meta.wg_public_key)
    await KeyService(p, node).rotate("a", {"netshield": 2})
    meta = node.puts[0][1]
    assert meta["options"]["netshield"] == 2 and meta["options"]["accelerator"] is True
    assert meta["device_name"].startswith("exit-proton-a-")


async def test_a_rotation_the_node_refuses_deletes_the_new_certificate_and_keeps_the_old(logged_in):
    p, sess = logged_in
    old = await dashboard_key(p)
    with pytest.raises(NodeError):
        await KeyService(p, FakeExitNode(slot_pub=old.meta.wg_public_key, fail=True)).rotate("a", {})
    assert [c["ClientKeyFingerprint"] for c in sess.certs] == ["fp0"]


async def test_renew_extends_the_same_client_key_and_records_the_new_expiry(logged_in):
    p, sess = logged_in
    old = await dashboard_key(p, {"netshield": 0})
    node = FakeExitNode(slot_pub=old.meta.wg_public_key)
    out = await KeyService(p, node).renew("a")
    body = sess.calls[-1][2]
    assert body["Renew"] is True and body["ClientPublicKey"] == old.meta.client_public_key
    assert out["key"]["meta"]["expires_at"] == 2_000_001_000
    assert node.metas[0]["wg_public_key"] == old.meta.wg_public_key


async def test_the_listing_marks_the_slots_key_and_the_nodes_orphans(logged_in):
    p, _ = logged_in
    used = await p.create_key("exit-proton-a-20261001", {}, PEER)
    await p.create_key("exit-proton-b-20261001", {}, PEER)
    await p.create_key("my-phone", {}, PEER)
    certs = await KeyService(p, FakeExitNode(slot_pub=used.meta.wg_public_key)).certificates()
    assert [(c["device_name"], c["slot"], c["orphan"]) for c in certs] == [
        ("exit-proton-a-20261001", "a", False), ("exit-proton-b-20261001", None, True),
        ("my-phone", None, False)]


async def test_nothing_is_an_orphan_when_the_node_is_unreachable(logged_in):
    p, _ = logged_in
    await p.create_key("exit-proton-b-20261001", {}, PEER)

    class Unreachable(FakeExitNode):
        async def keys(self):
            raise NodeError(0, "unreachable")
    certs = await KeyService(p, Unreachable()).certificates()
    assert certs and not any(c["orphan"] for c in certs)
