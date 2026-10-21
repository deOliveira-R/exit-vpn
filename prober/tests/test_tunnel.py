"""wg-probe (boundary): the `wg` and `ip` commands that attach, detach and re-key it."""

import pytest

from prober.keyfile import KeyFile
from prober.model import Target
from prober.tunnel import ProbeTunnel, TunnelError

from harness import FakeRun

TARGET = Target("US-NY#1", "198.51.100.1", "pk=")


async def test_attach_replaces_every_other_peer_with_the_target():
    run = FakeRun(peers="old=\n")
    await ProbeTunnel(run=run).attach(TARGET)
    assert run.calls[1] == ("wg", "set", "wg-probe", "peer", "old=", "remove", "peer", "pk=", "endpoint",
                            "198.51.100.1:51820", "allowed-ips", "0.0.0.0/0", "persistent-keepalive", "25")


async def test_attach_brings_the_link_and_its_route_back():
    """The link may have been taken down by hand or by a test; a probe must still work."""
    run = FakeRun()
    await ProbeTunnel(run=run).attach(TARGET)
    assert ("ip", "link", "set", "wg-probe", "up") in run.calls
    assert ("ip", "route", "replace", "default", "dev", "wg-probe", "table", "200") in run.calls


async def test_a_session_detaches_even_when_the_probe_fails():
    run = FakeRun(peers="old=\n")
    with pytest.raises(RuntimeError):
        async with ProbeTunnel(run=run).session(TARGET):
            raise RuntimeError("the probe failed")
    assert run.calls[-1] == ("wg", "set", "wg-probe", "peer", "old=", "remove")


async def test_reload_key_sets_the_new_private_key_through_stdin():
    run = FakeRun(addr="5: wg-probe    inet 10.2.0.2/32 scope global wg-probe\n")
    await ProbeTunnel(run=run).reload_key(KeyFile("new=", "10.2.0.2/32"))
    assert run.calls[-1] == ("wg", "set", "wg-probe", "private-key", "/dev/stdin")
    assert run.stdin[-1] == "new=\n" and all("new=" not in c for c in run.calls), "never on the command line"


async def test_reload_key_refuses_a_key_for_another_address():
    """The kill switch and NAT were set up for wg-probe's address."""
    run = FakeRun(addr="5: wg-probe    inet 10.2.0.2/32 scope global wg-probe\n")
    with pytest.raises(TunnelError, match="differs"):
        await ProbeTunnel(run=run).reload_key(KeyFile("new=", "10.2.0.9/32"))
    assert not any("private-key" in c for c in run.calls)
