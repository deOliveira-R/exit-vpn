"""exitctl's side of its contract with the app (PLAN R1): `exitctl --json` output and exit
codes must match contracts/exitctl/. Commands run through `cli.main` with a fake Proton
session and a fake controller, as the app runs them."""

import io
import json
import sys
from pathlib import Path

import pytest

from exitctl import cli, providers
from exitctl.providers.proton import ProtonProvider, catalog_rows

from test_helper import FakeController, FakeSession, memory_keychain  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "contracts"))
import shape  # noqa: E402


class Node(FakeController):
    """The fake controller, answering catalog calls with the controller's own sample."""

    async def put_catalog(self, provider, rows):
        return shape.load("controller/status")["catalog"]

    async def delete_catalog(self):
        return shape.load("controller/status")["catalog"]


LOGICALS = [{"Name": "US-NY#1", "ExitCountry": "US", "City": "New York", "Tier": 2, "Features": 4 | 8,
             "Status": 1, "Load": 33, "Servers": [{"EntryIP": "185.159.157.9", "X25519PublicKey": "k1",
                                                   "Domain": "node-us-01.protonvpn.net", "Status": 1}]}]


class Runner:
    """`runner(argv, stdin=None) -> (exit code, parsed stdout)` against one fake account."""

    def __init__(self, monkeypatch):
        self.mp, self.sess, self.node = monkeypatch, FakeSession(), Node()
        self.proton = ProtonProvider(session_factory=lambda: self.sess)
        real_get = providers.get
        monkeypatch.setattr(providers, "get", lambda n: self.proton if n == "proton" else real_get(n))
        monkeypatch.setattr(cli, "Controller", lambda url=None: self.node)

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


@pytest.fixture
def run(monkeypatch):
    return Runner(monkeypatch)


def test_account_commands(run):
    code, out = run(["proton", "whoami"])
    assert code == 0
    shape.check("exitctl/logged_out", out)
    code, out = run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    assert code == 0
    shape.check("exitctl/login", out)
    code, out = run(["proton", "whoami"])
    assert code == 0
    shape.check("exitctl/whoami", out)


def test_login_steps_need_input(run):
    run.sess.twofa = "123456"
    code, out = run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    assert code == 3 and out["need"] == "totp"
    shape.check("exitctl/need_totp", out)

    from proton.session.exceptions import ProtonAPIError

    async def captcha(user, password, additional_headers=None):
        raise ProtonAPIError(422, {}, {"Code": 9001, "Error": "complete CAPTCHA",
                                       "Details": {"HumanVerificationToken": "hvtok",
                                                   "Methods": ["captcha"]}})
    run.sess.async_authenticate = captcha
    code, out = run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    assert code == 3 and out["need"] == "human_verification"
    shape.check("exitctl/need_human_verification", out)


def test_listing_commands(run):
    code, out = run(["providers"])
    assert code == 0
    shape.check("exitctl/providers", out)
    code, out = run(["proton", "options"])
    assert code == 0
    shape.check("exitctl/options", out, maps={"$[].choices"})
    code, out = run(["keys"])
    assert code == 0
    shape.check("exitctl/keys", out, maps={"$.slots[].options"}, optional={"$.slots[].meta"})


def test_key_commands(run):
    run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    code, out = run(["proton", "rotate", "--slot", "a", "--netshield", "1"])
    assert code == 0
    shape.check("exitctl/rotate", out, maps={"$.key.options", "$.key.meta.options"})
    code, out = run(["proton", "list"])
    assert code == 0
    shape.check("exitctl/list", out, maps={"$[].options"})
    code, out = run(["proton", "renew", "--slot", "a"])
    assert code == 0
    shape.check("exitctl/renew", out, maps={"$.key.options", "$.key.meta.options"})


def test_server_list_push(run):
    run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    code, out = run(["proton", "servers", "--push"])
    assert code == 0
    shape.check("exitctl/servers_push", out)


def test_errors(run):
    code, out = run(["proton", "list"])                    # not logged in
    assert code == 1
    shape.check("exitctl/error", out)
    run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    run.node.fail = True                                    # the node refuses the new key
    code, out = run(["proton", "rotate", "--slot", "a"])
    assert code == 2
    shape.check("exitctl/error", out)
