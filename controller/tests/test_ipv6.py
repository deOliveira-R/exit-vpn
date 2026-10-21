"""IPv6 through the exit: the IPv6 gate in the health cycle, in switches and key changes, and
what status, events and warnings say about it (component: the real node on a fake tunnel)."""

import dataclasses

from exit_vpn.health import Health
from exit_vpn.wgconf import WgConf

from harness import FakeHealth, strikes


def gate(rig) -> bool:
    return rig.settings.ipv6_gate_file.exists()


async def test_the_gate_opens_after_a_cycle_proves_ipv6(rig):
    await rig.svc.start()
    assert not gate(rig) and not rig.svc.snapshot()["ipv6"]
    await rig.svc.step()
    st = rig.svc.snapshot()
    assert gate(rig) and st["ipv6"] and st["exit_ip6"] == "2001:db8::9"


async def test_start_closes_a_gate_left_open(rig):
    rig.settings.ipv6_gate_file.parent.mkdir(parents=True)
    rig.settings.ipv6_gate_file.touch()
    await rig.svc.start()
    assert not gate(rig)


async def test_a_server_without_ipv6_keeps_the_gate_closed_and_is_not_failed_over(rig):
    rig.wg.no_ipv6.add(rig.start.pubkey)
    await rig.svc.start()
    for _ in range(5):
        await rig.svc.step()
    st = rig.svc.snapshot()
    assert rig.svc.health == Health.HEALTHY and not rig.wg.swaps
    assert not gate(rig) and not st["ipv6"] and st["exit_ip6"] is None and st["errors"] == []


async def test_ipv6_failing_closes_the_gate_but_never_fails_over(rig):
    await rig.svc.start()
    await rig.svc.step()
    assert gate(rig)
    rig.wg.no_ipv6.add(rig.start.pubkey)
    await rig.svc.step()
    assert gate(rig), "one failed probe is not enough"
    await rig.svc.step()
    assert not gate(rig) and not rig.wg.swaps and rig.svc.health == Health.HEALTHY


async def test_a_switch_closes_the_gate_before_the_swap_and_the_new_server_reopens_it(rig):
    await rig.svc.start()
    await rig.svc.step()
    seen = []
    swap = rig.wg.swap_peer

    async def watching(pubkey, endpoint):
        seen.append(gate(rig))
        await swap(pubkey, endpoint)
    rig.wg.swap_peer = watching
    await rig.svc.rotate("country", "o@github")
    assert seen == [False], "closed before the peer changed"
    assert gate(rig), "proven again on the new server"


async def test_a_switch_to_a_server_without_ipv6_leaves_the_gate_closed(rig, catalog):
    await rig.svc.start()
    await rig.svc.step()
    rig.wg.no_ipv6 |= {s.pubkey for s in catalog.servers if s.pubkey != rig.start.pubkey}
    await rig.svc.rotate("country", "o@github")
    assert not gate(rig) and not rig.svc.snapshot()["ipv6"]


async def test_a_failover_closes_the_gate_until_the_new_server_proves_ipv6(rig):
    await rig.svc.start()
    await rig.svc.step()
    rig.wg.dead_peers.add(rig.start.pubkey)
    rig.wg.no_ipv6.add(rig.start.pubkey)
    await strikes(rig)
    assert rig.wg.peer.pubkey != rig.start.pubkey and gate(rig)


async def test_a_key_change_closes_the_gate_until_proven_again(rig):
    await rig.svc.start()
    await rig.svc.step()
    seen = []
    set_key = rig.wg.set_private_key

    async def watching(key):
        seen.append(gate(rig))
        await set_key(key)
    rig.wg.set_private_key = watching
    conf = WgConf("10.2.0.2", "10.2.0.1", rig.start.pubkey, "e", address6="2a07:b944::2:2")
    assert await rig.svc.replace_exit_key("new=", rig.boot_a, conf, "test")
    assert seen == [False] and gate(rig)


async def test_each_change_of_the_gate_is_one_ipv6_event(rig):
    await rig.svc.start()
    for _ in range(3):
        await rig.svc.step()
    ev = [e for e in rig.node.events.recent() if e["kind"] == "ipv6"]
    assert [(e["open"], e["exit_ip6"]) for e in ev] == [(True, "2001:db8::9")]
    rig.wg.no_ipv6.add(rig.start.pubkey)
    for _ in range(3):
        await rig.svc.step()
    ev = [e for e in rig.node.events.recent() if e["kind"] == "ipv6"]
    assert [e["open"] for e in ev] == [True, False] and ev[-1]["server"] == rig.start.name


async def test_a_switch_between_two_ipv6_servers_reports_no_change(rig):
    await rig.svc.start()
    await rig.svc.step()
    await rig.svc.rotate("country", "o@github")
    assert rig.kinds(ipv6=True).count("ipv6") == 1


async def test_a_flagged_server_whose_ipv6_fails_gets_a_warning(rig, monkeypatch):
    flagged = dataclasses.replace(rig.start, ipv6=True)
    monkeypatch.setattr(rig.svc, "current_server", lambda: flagged)
    rig.wg.no_ipv6.add(rig.start.pubkey)
    await rig.svc.start()
    await rig.svc.step()
    assert not any("IPv6" in w for w in rig.svc.snapshot()["warnings"]), "not after one probe"
    await rig.svc.step()
    assert any("IPv6 fails on this server" in w for w in rig.svc.snapshot()["warnings"])


async def test_an_unflagged_server_without_ipv6_gets_no_warning(rig):
    rig.wg.no_ipv6.add(rig.start.pubkey)
    await rig.svc.start()
    for _ in range(3):
        await rig.svc.step()
    assert rig.svc.snapshot()["warnings"] == []


async def test_without_a_tunnel_ipv6_address_the_gate_never_opens_and_says_why(rig):
    rig.svc.tunnel.probe = FakeHealth(rig.wg, rig.clock, source6="")
    await rig.svc.start()
    await rig.svc.step()
    assert not gate(rig)
    assert any("rotate the exit key" in w for w in rig.svc.snapshot()["warnings"])
    rig.svc.exit_conf = dataclasses.replace(rig.svc.exit_conf, address6="2a07:b944::2:2")
    assert any("restart the exit" in w for w in rig.svc.snapshot()["warnings"])


async def test_with_ipv6_off_no_warning_is_given(rig, monkeypatch):
    rig.svc.tunnel.probe = FakeHealth(rig.wg, rig.clock, source6="")
    monkeypatch.setattr(rig.svc, "s", dataclasses.replace(rig.svc.s, ipv6=False))
    await rig.svc.start()
    await rig.svc.step()
    assert rig.svc.snapshot()["warnings"] == [] and not gate(rig)
