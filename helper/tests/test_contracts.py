"""`exitctl --json` output and exit codes against contracts/exitctl/, as the app runs them (contract)."""

import io
import json
import sys

import pytest
from harness import captcha_error, shape

from exitctl import cli


def test_whoami_and_login_match_their_samples(run):
    code, out = run(["proton", "whoami"])
    assert code == 0
    shape.check("exitctl/logged_out", out)
    code, out = run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    assert code == 0
    shape.check("exitctl/login", out)
    code, out = run(["proton", "whoami"])
    assert code == 0
    shape.check("exitctl/whoami", out)


def test_login_steps_that_need_input_exit_3_with_their_samples(run):
    run.sess.twofa = "123456"
    code, out = run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    assert code == 3 and out["need"] == "totp"
    shape.check("exitctl/need_totp", out)

    async def captcha(user, password, additional_headers=None):
        raise captcha_error()
    run.sess.async_authenticate = captcha
    code, out = run(["proton", "login", "--stdin"], {"username": "u", "password": "pw"})
    assert code == 3 and out["need"] == "human_verification"
    shape.check("exitctl/need_human_verification", out)


def test_providers_options_and_keys_match_their_samples(run):
    code, out = run(["providers"])
    assert code == 0
    shape.check("exitctl/providers", out)
    code, out = run(["proton", "options"])
    assert code == 0
    shape.check("exitctl/options", out, maps={"$[].choices"})
    code, out = run(["keys"])
    assert code == 0
    shape.check("exitctl/keys", out, maps={"$.slots[].options"}, optional={"$.slots[].meta"})


def test_rotate_list_and_renew_match_their_samples(run):
    run.login()
    code, out = run(["proton", "rotate", "--slot", "a", "--netshield", "1"])
    assert code == 0
    shape.check("exitctl/rotate", out, maps={"$.key.options", "$.key.meta.options"})
    code, out = run(["proton", "list"])
    assert code == 0
    shape.check("exitctl/list", out, maps={"$[].options"})
    code, out = run(["proton", "renew", "--slot", "a"])
    assert code == 0
    shape.check("exitctl/renew", out, maps={"$.key.options", "$.key.meta.options"})


def test_a_server_list_push_matches_its_sample(run):
    run.login()
    code, out = run(["proton", "servers", "--push"])
    assert code == 0
    shape.check("exitctl/servers_push", out)


def test_a_provider_error_exits_1_with_the_error_sample(run):
    code, out = run(["proton", "list"])                    # not logged in
    assert code == 1
    shape.check("exitctl/error", out)


def test_a_key_the_node_refuses_exits_2_with_the_error_sample(run):
    run.login()
    run.node.fail = True
    code, out = run(["proton", "rotate", "--slot", "a"])
    assert code == 2
    shape.check("exitctl/error", out)


def test_unreadable_input_is_a_json_error_not_a_traceback(run, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    with pytest.raises(SystemExit) as e:
        cli.main(["proton", "login", "--stdin", "--json"])
    assert e.value.code == 1
    shape.check("exitctl/error", json.loads(out.getvalue()))


def test_a_slot_the_node_does_not_have_is_a_json_error(run):
    from exitctl.exitnode import ExitNode

    async def no_slots():
        return []
    run.node.keys = no_slots
    run.node.key = lambda slot: ExitNode.key(run.node, slot)
    run.login()
    code, out = run(["proton", "renew", "--slot", "a"])
    assert code == 1 and "no slot" in out["error"]
