"""exitctl: key derivation, Proton API payloads (fake session), rotate/renew flows."""

import base64

import pytest

from exitctl import cli, keychain
from exitctl.controller import ControllerError
from exitctl.providers.base import NeedInput, ProviderError
from exitctl.providers.proton import OPTIONS, ProtonProvider, features
from exitctl.wgkeys import (Ed25519Pair, Peer, b64, ed25519_to_x25519_public, render_conf,
                            wg_public_for_client_key, x25519_public)

PEER = Peer("US-NY#1", "cGVlcg==", "192.0.2.10:51820")


@pytest.fixture(autouse=True)
def memory_keychain(monkeypatch):
    store = {}
    monkeypatch.setattr(keychain, "load", lambda n: store.get(n))
    monkeypatch.setattr(keychain, "save", lambda n, d: store.__setitem__(n, d))
    monkeypatch.setattr(keychain, "delete", lambda n: store.pop(n, None))
    return store


def test_x25519_derivation_matches_the_registered_ed25519_key():
    for _ in range(20):
        pair = Ed25519Pair.generate()
        assert x25519_public(pair.x25519_private()) == b64(ed25519_to_x25519_public(pair.public))
        assert wg_public_for_client_key(b64(pair.public)) == x25519_public(pair.x25519_private())
    assert "seed" not in repr(pair) and b64(pair.seed) not in repr(pair)


def test_pem_client_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    pem = k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    raw = b64(k.public_key().public_bytes_raw())
    assert wg_public_for_client_key(pem) == wg_public_for_client_key(raw) != ""


def test_render_conf_layout():
    text = render_conf("PRIV", "10.2.0.2/32", "10.2.0.1", PEER, "exit-proton-a-20261020",
                       {"NetShield": "1", "VPN Accelerator": "on"})
    assert text.splitlines()[:4] == ["[Interface]", "# Key for exit-proton-a-20261020",
                                     "# NetShield = 1", "# VPN Accelerator = on"]
    assert "Endpoint = 192.0.2.10:51820" in text and "# US-NY#1" in text


def test_options_and_features():
    by = {o.key: o for o in OPTIONS}
    assert by["netshield"].parse("2") == 2 and by["accelerator"].parse("off") is False
    with pytest.raises(ProviderError):
        by["netshield"].parse("3")
    assert features({"netshield": 1, "accelerator": True, "moderate_nat": True,
                     "port_forward": False}) == {"NetShieldLevel": 1, "SplitTCP": True,
                                                 "RandomNAT": False, "PortForwarding": False}
    p = ProtonProvider(session_factory=lambda: None)
    assert p.values_from_conf_options({"NetShield": "0", "Moderate NAT": "off",
                                       "VPN Accelerator": "on"}) == {
        "netshield": 0, "moderate_nat": False, "accelerator": True}


class FakeSession:
    """Just enough of proton.session.Session."""

    def __init__(self):
        self.UID, self.AccountName, self.needs_twofa = None, None, False
        self.calls, self.certs, self.observers, self.twofa = [], [], [], None

    def __setstate__(self, state):
        self.UID, self.AccountName = state.get("UID"), state.get("AccountName")
        self.needs_twofa = state.get("needs_twofa", False)

    def register_persistence_observer(self, o):
        self.observers.append(o)

    def _persist(self):
        for o in self.observers:
            o._release_session_lock(self.AccountName, {"UID": self.UID, "AccountName": self.AccountName,
                                                       "needs_twofa": self.needs_twofa})

    async def async_authenticate(self, user, password, additional_headers=None):
        if password != "pw":
            return False
        self.UID, self.AccountName, self.needs_twofa = "uid", user, self.twofa is not None
        self._persist()
        return True

    async def async_validate_2fa_code(self, code):
        ok = code == self.twofa
        if ok:
            self.needs_twofa = False
            self._persist()
        return ok

    async def async_api_request(self, endpoint, jsondata=None, method=None, params=None):
        self.calls.append((endpoint, method, jsondata, params))
        if endpoint == "/vpn/v1/certificate" and method is None:
            c = {"ClientKeyFingerprint": f"fp{len(self.certs)}", "ClientKey": jsondata["ClientPublicKey"],
                 "SerialNumber": "s", "ExpirationTime": 2_000_000_000 + (1000 if jsondata.get("Renew") else 0),
                 "DeviceName": jsondata["DeviceName"], "Features": jsondata["Features"], "Mode": "persistent"}
            if not jsondata.get("Renew"):
                self.certs.append(c)
            return c
        if endpoint == "/vpn/v1/certificate/all":
            off = params["Offset"]
            return {"Certificates": self.certs[off:off + params["Limit"]], "Total": len(self.certs)}
        if endpoint == "/vpn/v1/certificate" and method == "delete":
            before = len(self.certs)
            self.certs = [c for c in self.certs if c["ClientKeyFingerprint"] != jsondata["ClientPublicKeyFingerprint"]]
            return {"Count": before - len(self.certs)}
        raise AssertionError(endpoint)


@pytest.fixture
def proton():
    sess = FakeSession()
    return ProtonProvider(session_factory=lambda: sess), sess


async def test_login_with_2fa_in_two_steps(proton, memory_keychain):
    p, sess = proton
    sess.twofa = "123456"
    with pytest.raises(NeedInput) as e:
        await p.login(None, {"username": "u", "password": "pw"})
    assert e.value.what == "totp"
    assert "pw" not in str(memory_keychain)
    p2 = ProtonProvider(session_factory=lambda: sess)   # a new process: state from the Keychain
    assert (await p2.login(None, {"totp": "123456"}))["logged_in"]
    with pytest.raises(ProviderError):
        await p.login(None, {"username": "u", "password": "wrong"})


async def test_create_key_payload_and_conf(proton):
    p, sess = proton
    await p.login(None, {"username": "u", "password": "pw"})
    new = await p.create_key("exit-proton-a-20261020", {"netshield": 1}, PEER)
    ep, method, body, _ = sess.calls[-1]
    assert ep == "/vpn/v1/certificate" and body["Mode"] == "persistent"
    assert body["Features"] == {"NetShieldLevel": 1, "SplitTCP": True, "RandomNAT": True,
                                "PortForwarding": False}
    assert len(base64.b64decode(body["ClientPublicKey"])) == 32
    priv = next(l.split(" = ")[1] for l in new.conf.splitlines() if l.startswith("PrivateKey"))
    assert x25519_public(priv) == new.meta["wg_public_key"]
    assert new.meta["wg_public_key"] == wg_public_for_client_key(body["ClientPublicKey"])
    assert "# NetShield = 1" in new.conf and "# Moderate NAT = off" in new.conf
    assert priv not in repr(new) and new.meta["cert_id"] == "fp0"


async def test_not_logged_in(proton):
    p, _ = proton
    with pytest.raises(ProviderError, match="not logged in"):
        await p.list_keys()


class FakeController:
    base_url = "http://exit-proton:8080"

    def __init__(self, slot_pub="", fail=False):
        self.slot = {"slot": "a", "role": "exit", "source": "bootstrap", "public_key": slot_pub,
                     "options": {"NetShield": "0", "VPN Accelerator": "on"}, "meta": {},
                     "expires_in_days": None}
        self.fail, self.puts, self.metas = fail, [], []

    async def status(self):
        return {"server": "US-NY#1", "pubkey": "cGVlcg==", "endpoint": "192.0.2.10:51820"}

    async def key(self, slot):
        return self.slot

    async def keys(self):
        return [self.slot]

    async def put_key(self, slot, conf, meta):
        self.puts.append((slot, meta))
        if self.fail:
            raise ControllerError(409, "the new key carried no traffic")
        self.slot = {**self.slot, "source": "uploaded", "public_key": meta["wg_public_key"], "meta": meta}
        return self.slot

    async def put_meta(self, slot, meta):
        self.metas.append(meta)
        self.slot = {**self.slot, "meta": meta}
        return self.slot


async def test_rotate_replaces_and_deletes_the_old_cert(proton):
    p, sess = proton
    await p.login(None, {"username": "u", "password": "pw"})
    old = await p.create_key("dashboard-key", {}, PEER)     # stands in for a dashboard key
    ctl = FakeController(slot_pub=old.meta["wg_public_key"])
    out = await cli.rotate(p, ctl, "a", {"netshield": 2})
    assert out["deleted_old"] == "fp0"
    assert [c["ClientKeyFingerprint"] for c in sess.certs] == ["fp1"]
    meta = ctl.puts[0][1]
    assert meta["options"]["netshield"] == 2 and meta["options"]["accelerator"] is True
    assert meta["device_name"].startswith("exit-proton-a-")


async def test_rotate_rejected_deletes_the_new_cert(proton):
    p, sess = proton
    await p.login(None, {"username": "u", "password": "pw"})
    old = await p.create_key("dashboard-key", {}, PEER)
    ctl = FakeController(slot_pub=old.meta["wg_public_key"], fail=True)
    with pytest.raises(ControllerError):
        await cli.rotate(p, ctl, "a", {})
    assert [c["ClientKeyFingerprint"] for c in sess.certs] == ["fp0"]


async def test_renew_a_dashboard_key(proton):
    p, sess = proton
    await p.login(None, {"username": "u", "password": "pw"})
    old = await p.create_key("dashboard-key", {"netshield": 0}, PEER)
    ctl = FakeController(slot_pub=old.meta["wg_public_key"])
    out = await cli.renew(p, ctl, "a")
    body = sess.calls[-1][2]
    assert body["Renew"] is True and body["ClientPublicKey"] == old.meta["client_public_key"]
    assert out["key"]["meta"]["expires_at"] == 2_000_001_000
    assert ctl.metas[0]["wg_public_key"] == old.meta["wg_public_key"]


async def test_list_flags_slot_and_orphans(proton):
    p, sess = proton
    await p.login(None, {"username": "u", "password": "pw"})
    used = await p.create_key("exit-proton-a-20261001", {}, PEER)
    await p.create_key("exit-proton-b-20261001", {}, PEER)
    await p.create_key("my-phone", {}, PEER)
    certs = await cli.list_certs(p, FakeController(slot_pub=used.meta["wg_public_key"]))
    assert [(c["device_name"], c["slot"], c["orphan"]) for c in certs] == [
        ("exit-proton-a-20261001", "a", False), ("exit-proton-b-20261001", None, True),
        ("my-phone", None, False)]


def test_cli_parses_generated_flags():
    args = cli.build_parser().parse_args(["proton", "rotate", "--slot", "a", "--netshield", "1",
                                          "--moderate-nat", "on", "--json"])
    assert args.opt_netshield == "1" and args.opt_moderate_nat == "on" and args.json


async def test_login_captcha_then_retry_with_token(proton):
    from proton.session.exceptions import ProtonAPIError
    p, sess = proton
    seen = []
    real = sess.async_authenticate

    async def auth(user, password, additional_headers=None):
        seen.append(additional_headers)
        if not additional_headers:
            raise ProtonAPIError(422, {}, {"Code": 9001, "Error": "complete CAPTCHA",
                                           "Details": {"HumanVerificationToken": "hvtok",
                                                       "Methods": ["captcha"]}})
        return await real(user, password)
    sess.async_authenticate = auth
    with pytest.raises(NeedInput) as e:
        await p.login(None, {"username": "u", "password": "pw"})
    assert e.value.what == "human_verification" and "token=hvtok" in e.value.info["url"]

    class Answer:
        def __init__(self): self.msgs = []
        def ask(self, label, secret=False): return ""
        def info(self, m): self.msgs.append(m)
    ans = Answer()
    assert (await p.login(ans, {"username": "u", "password": "pw"}))["logged_in"]
    assert seen[-1] == {"x-pm-human-verification-token": "hvtok",
                        "x-pm-human-verification-token-type": "captcha"}
    assert "verify.proton.me" in ans.msgs[0]
