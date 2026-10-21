"""Making a key: the certificate request to Proton and the config it yields (component)."""

import base64

from harness import PEER

from exitctl.providers.proton.crypto import wg_public_for_client_key
from exitctl.wgkeys import x25519_public


async def test_a_new_key_requests_a_persistent_certificate_with_the_levers_as_features(logged_in):
    p, sess = logged_in
    await p.create_key("exit-proton-a-20261020", {"netshield": 1}, PEER)
    ep, method, body, _ = sess.calls[-1]
    assert ep == "/vpn/v1/certificate" and method is None and body["Mode"] == "persistent"
    assert body["Features"] == {"NetShieldLevel": 1, "SplitTCP": True, "RandomNAT": True,
                                "PortForwarding": False}
    assert len(base64.b64decode(body["ClientPublicKey"])) == 32


async def test_the_new_config_holds_the_private_key_matching_the_certificate(logged_in):
    p, sess = logged_in
    new = await p.create_key("exit-proton-a-20261020", {"netshield": 1}, PEER)
    body = sess.calls[-1][2]
    priv = next(l.split(" = ")[1] for l in new.conf.splitlines() if l.startswith("PrivateKey"))
    assert x25519_public(priv) == new.meta.wg_public_key == wg_public_for_client_key(body["ClientPublicKey"])
    assert new.meta.cert_id == "fp0"


async def test_the_new_config_records_every_lever(logged_in):
    p, _ = logged_in
    new = await p.create_key("k", {"netshield": 1}, PEER)
    assert "# NetShield = 1" in new.conf and "# Moderate NAT = off" in new.conf


async def test_a_new_key_never_shows_its_private_key(logged_in):
    p, _ = logged_in
    new = await p.create_key("k", {}, PEER)
    priv = next(l.split(" = ")[1] for l in new.conf.splitlines() if l.startswith("PrivateKey"))
    assert priv not in repr(new)


async def test_the_new_config_carries_the_in_tunnel_ipv4_and_ipv6_addresses(logged_in):
    p, _ = logged_in
    new = await p.create_key("k", {}, PEER)
    assert "Address = 10.2.0.2/32, 2a07:b944::2:2/128" in new.conf.splitlines()
    assert "DNS = 10.2.0.1, 2a07:b944::2:1" in new.conf.splitlines()
