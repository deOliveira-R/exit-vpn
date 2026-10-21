"""The command line: argument parsing, generated lever flags, the interactive login (component)."""

import pytest
from harness import LOGIN, captcha_error

from exitctl import cli


def test_each_lever_gets_its_own_flag():
    args = cli.build_parser().parse_args(["proton", "rotate", "--slot", "a", "--netshield", "1",
                                          "--moderate-nat", "on", "--json"])
    assert args.opt_netshield == "1" and args.opt_moderate_nat == "on" and args.json


@pytest.mark.parametrize("argv", [
    ["--json", "--url", "http://x:1", "keys"], ["keys", "--json", "--url", "http://x:1"],
    ["--json", "proton", "list"], ["proton", "--url", "http://x:1", "list", "--json"]])
def test_json_and_url_count_wherever_they_are(argv):
    a = cli.build_parser().parse_args(argv)
    assert a.json and getattr(a, "url", "http://x:1") == "http://x:1"


class Terminal:
    """Answers the prompts; the CAPTCHA is solved in the browser, then Enter."""

    def __init__(self):
        self.msgs = []

    def ask(self, label, secret=False):
        return {"Proton VPN username": "u"}.get(label, "")

    def info(self, m):
        self.msgs.append(m)


async def test_the_interactive_login_shows_the_captcha_link_then_retries_with_its_token(proton):
    p, sess = proton
    seen, real = [], sess.async_authenticate

    async def auth(user, password, additional_headers=None):
        seen.append(additional_headers)
        if not additional_headers:
            raise captcha_error()
        return await real(user, password)
    sess.async_authenticate = auth
    term = Terminal()
    assert (await cli.interactive_login(p, LOGIN, term)).logged_in
    assert "verify.proton.me" in term.msgs[0]
    assert seen[-1] == {"x-pm-human-verification-token": "hvtok",
                        "x-pm-human-verification-token-type": "captcha"}
