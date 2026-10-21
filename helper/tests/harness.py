"""exitctl's test harness: a fake Proton session, a fake exit node, a CLI runner, shared data.
Test files import from here; conftest.py turns these into fixtures."""

import io
import json
import sys
from pathlib import Path

import pytest

from exitctl import cli, providers
from exitctl.model import NodeError
from exitctl.providers.base import Credentials
from exitctl.providers.proton import ProtonProvider, catalog_rows
from exitctl.sessions import MemoryStore
from exitctl.wgkeys import Peer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "contracts"))
import shape

PEER = Peer("US-NY#1", "cGVlcg==", "192.0.2.10:51820")
LOGIN = Credentials(username="u", password="pw")

LOGICALS = [{"Name": "US-NY#1", "ExitCountry": "US", "City": "New York", "Tier": 2, "Features": 4 | 8,
             "Status": 1, "Load": 33, "Servers": [{"EntryIP": "185.159.157.9", "X25519PublicKey": "k1",
                                                   "Domain": "node-us-01.protonvpn.net", "Status": 1}]}]


def captcha_error():
    """What proton-core raises when Proton wants a CAPTCHA (a plain ProtonAPIError, 422/9001)."""
    from proton.session.exceptions import ProtonAPIError
    return ProtonAPIError(422, {}, {"Code": 9001, "Error": "complete CAPTCHA",
                                    "Details": {"HumanVerificationToken": "hvtok", "Methods": ["captcha"]}})


class FakeSession:
    """Just enough of proton.session.Session: password "pw", optional 2FA code, certificates."""

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


class FakeExitNode:
    """The exit node's controller API as KeyService and the CLI use it: one slot `a`."""

    base_url = "http://exit-proton:8080"
    node_name = "exit-proton"

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
            raise NodeError(409, "the new key carried no traffic")
        self.slot = {**self.slot, "source": "uploaded", "public_key": meta["wg_public_key"], "meta": meta}
        return self.slot

    async def put_meta(self, slot, meta):
        self.metas.append(meta)
        self.slot = {**self.slot, "meta": meta}
        return self.slot

    async def put_catalog(self, provider, rows):
        return shape.load("controller/status")["catalog"]

    async def delete_catalog(self):
        return shape.load("controller/status")["catalog"]


class Runner:
    """`run(argv, stdin=None) -> (exit code, parsed stdout)`: `exitctl … --json` as the app runs
    it, against one fake account and a fake exit node."""

    def __init__(self, monkeypatch):
        self.mp, self.sess, self.node = monkeypatch, FakeSession(), FakeExitNode()
        self.proton = ProtonProvider(store=MemoryStore(), session_factory=lambda: self.sess)
        real_get = providers.get
        monkeypatch.setattr(providers, "get", lambda n: self.proton if n == "proton" else real_get(n))
        monkeypatch.setattr(cli, "ExitNode", lambda url=None: self.node)

        async def servers():
            return catalog_rows(LOGICALS)
        self.proton.servers = servers

    def __call__(self, argv, stdin=None):
        self.mp.setattr(sys, "stdin", io.StringIO(json.dumps(stdin) if stdin is not None else ""))
        out = io.StringIO()
        self.mp.setattr(sys, "stdout", out)
        with pytest.raises(SystemExit) as e:
            cli.main([*argv, "--json"])
        return e.value.code, json.loads(out.getvalue())

    def login(self):
        assert self(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})[0] == 0
