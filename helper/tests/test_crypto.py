"""Key derivation and WireGuard config rendering (logic)."""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from harness import PEER

from exitctl.providers.proton.crypto import (
    Ed25519Pair,
    ed25519_to_x25519_public,
    wg_public_for_client_key,
)
from exitctl.wgkeys import b64, render_conf, x25519_public


def test_the_wireguard_key_derived_from_the_ed25519_pair_matches_the_registered_client_key():
    for _ in range(20):
        pair = Ed25519Pair.generate()
        assert x25519_public(pair.x25519_private()) == b64(ed25519_to_x25519_public(pair.public))
        assert wg_public_for_client_key(b64(pair.public)) == x25519_public(pair.x25519_private())


def test_a_key_pair_never_shows_its_seed():
    pair = Ed25519Pair.generate()
    assert "seed" not in repr(pair) and b64(pair.seed) not in repr(pair)


def test_a_pem_client_key_gives_the_same_wireguard_key_as_the_raw_one():
    k = Ed25519PrivateKey.generate()
    pem = k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    raw = b64(k.public_key().public_bytes_raw())
    assert wg_public_for_client_key(pem) == wg_public_for_client_key(raw) != ""


def test_a_rendered_config_starts_with_its_name_and_options_as_comments():
    text = render_conf("PRIV", "10.2.0.2/32", "10.2.0.1", PEER, "exit-proton-a-20261020",
                       {"NetShield": "1", "VPN Accelerator": "on"})
    assert text.splitlines()[:4] == ["[Interface]", "# Key for exit-proton-a-20261020",
                                     "# NetShield = 1", "# VPN Accelerator = on"]


def test_a_rendered_config_names_its_peer_and_endpoint():
    text = render_conf("PRIV", "10.2.0.2/32", "10.2.0.1", PEER, "k", {})
    assert "Endpoint = 192.0.2.10:51820" in text and "# US-NY#1" in text
