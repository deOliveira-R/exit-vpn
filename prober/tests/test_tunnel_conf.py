import pytest

from prober.keyfile import KeyFile, KeyFileError


def test_conf_fields():
    text = ("[Interface]\n# NetShield = 0\nPrivateKey = abc=\n"
            "Address = 10.2.0.2/32, 2a07:b944::2:2/128\nDNS = 10.2.0.1\n")
    k = KeyFile.parse(text)
    assert (k.private_key, k.address) == ("abc=", "10.2.0.2/32") and "abc" not in repr(k)
    assert KeyFile.parse(text.replace("10.2.0.2/32", "10.2.0.2")).address == "10.2.0.2/32"
    with pytest.raises(KeyFileError):
        KeyFile.parse("[Interface]\nAddress = 10.2.0.2/32\n")


def test_key_conf_prefers_slot(tmp_path, monkeypatch):
    boot = tmp_path / "b.conf"
    monkeypatch.setenv("PROBE_VPN_CONF", str(boot))
    monkeypatch.setenv("KEY_DIR_B", str(tmp_path / "keys"))
    assert KeyFile.path() == boot
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys/wg.conf").write_text("x")
    assert KeyFile.path() == tmp_path / "keys/wg.conf"


class FakeRun:
    def __init__(self, peers=""):
        self.calls, self.peers = [], peers

    async def __call__(self, *args, stdin=None):
        self.calls.append(args)
        return self.peers if args[:2] == ("wg", "show") else ""


async def test_attach_replaces_peers_and_the_session_always_detaches():
    from prober.model import Target
    from prober.tunnel import ProbeTunnel
    run = FakeRun(peers="old=\n")
    t = ProbeTunnel(run=run)
    with pytest.raises(RuntimeError):
        async with t.session(Target("US-NY#1", "198.51.100.1", "pk=")):
            raise RuntimeError("the probe failed")
    sets = [c for c in run.calls if c[:2] == ("wg", "set")]
    assert sets[0] == ("wg", "set", "wg-probe", "peer", "old=", "remove", "peer", "pk=", "endpoint",
                       "198.51.100.1:51820", "allowed-ips", "0.0.0.0/0", "persistent-keepalive", "25")
    assert ("ip", "route", "replace", "default", "dev", "wg-probe", "table", "200") in run.calls
    assert sets[-1] == ("wg", "set", "wg-probe", "peer", "old=", "remove"), "detached afterwards"


async def test_exit_check_rules():
    from prober.exitcheck import ExitCheck, ExitCheckError
    answers = [(None, "", "ConnectError"), (200, "ip=203.0.113.7\nloc=CH\n", "")]

    async def fetch(url, timeout):
        return answers.pop(0) if len(answers) > 1 else answers[0]
    import prober.exitcheck as ec
    real_sleep = ec.asyncio.sleep
    ec.asyncio.sleep = lambda s: real_sleep(0)
    try:
        info = await ExitCheck(fetch).verify("ch", set())
        assert (info.ip, info.loc) == ("203.0.113.7", "CH")
        with pytest.raises(ExitCheckError, match="forbidden"):
            await ExitCheck(fetch).verify("CH", {"203.0.113.7"})
        with pytest.raises(ExitCheckError, match="expected US"):
            await ExitCheck(fetch).verify("US", set())
        assert (await ExitCheck(fetch).verify("", set())).loc == "CH", "no country: no check"
    finally:
        ec.asyncio.sleep = real_sleep
