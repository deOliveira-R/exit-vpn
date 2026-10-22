"""The standby tunnel and the handover: the standby kept ready on another server of the exit's
region, a failing active tunnel handed over in one route change, the second opinion when both
fail (the uplink), owner switches made on the standby first, the standby's key (component:
the real node on two fake tunnels, a fake route and clock)."""

import pytest

from exit_vpn.errors import SwitchError
from exit_vpn.health import Health

from harness import conf, make, new_key, strikes


@pytest.fixture
def two(catalog, tmp_path, monkeypatch):
    """A node with both tunnels; wg1 starts on wg0's server, so its standby must search."""
    return make(catalog, tmp_path, monkeypatch, standby=True)


async def ready(rig):
    await rig.svc.start()
    await rig.svc.step()
    await rig.svc.standby_step()
    assert rig.svc.standby_view()["state"] == "ready"


def standby_srv(rig):
    return rig.svc.state.standby


# The standby ------------------------------------------------------------------------------
async def test_the_standby_comes_up_on_another_server_in_the_active_servers_city(two):
    await ready(two)
    sb = standby_srv(two)
    assert sb.pubkey != two.start.pubkey and sb.city == "New York"
    assert two.wg1.peer.pubkey == sb.pubkey and not two.wg.swaps, "the active tunnel untouched"
    st = two.svc.snapshot()["standby"]
    assert st["state"] == "ready" and st["server"] == sb.name and st["interface"] == "wg1"
    assert "standby" in two.kinds()
    assert "STANDBY" not in two.svc.store.standby_env.read_text() and \
        sb.pubkey in two.svc.store.standby_env.read_text(), "init-wg brings it back after a restart"


async def test_a_standby_whose_server_dies_moves_while_the_active_tunnel_is_untouched(two):
    await ready(two)
    dead = standby_srv(two).pubkey
    two.world.dead_peers.add(dead)
    two.clock.t += 30
    for _ in range(3):
        await two.svc.step()
        await two.svc.standby_step()
    sb = standby_srv(two)
    assert sb is not None and sb.pubkey != dead and not two.wg.swaps
    assert not two.svc.state.blacklist.contains(dead, two.clock()), \
        "the standby's failures never steer the active tunnel's failover"


async def test_a_standby_key_that_carries_nothing_is_suspected_and_blacklists_nothing(two):
    two.world.dead_keys.add(two.wg1.key)
    await two.svc.start()
    await two.svc.step()
    await two.svc.standby_step()   # five servers in a row fail
    st = two.svc.snapshot()
    assert st["standby"]["state"] == "key_suspect"
    assert any("standby key (slot c)" in w for w in st["warnings"])
    assert not two.svc.state.blacklist.active(two.clock())
    await two.svc.standby_step()
    assert st["standby"]["state"] == "key_suspect", "it stops searching"


async def test_the_standby_never_sits_on_the_active_server(two):
    await two.svc.start()
    assert two.svc.state.standby is None, "init-wg left wg1 on wg0's server"
    await two.svc.standby_step()
    assert standby_srv(two).pubkey != two.svc.state.active.pubkey


# The handover ---------------------------------------------------------------------------
async def test_a_dead_active_server_is_handed_over_to_the_standby_in_one_route_change(two):
    await ready(two)
    sb = standby_srv(two)
    two.world.dead_peers.add(two.start.pubkey)
    await strikes(two)
    assert two.route.sets == ["wg1"], "one route change"
    assert not two.wg.swaps, "no server change on the failing tunnel to get there"
    assert two.svc.tunnel.ifname == "wg1" and two.svc.state.active.pubkey == sb.pubkey
    assert two.svc.health == Health.HEALTHY
    assert two.svc.state.blacklist.contains(two.start.pubkey, two.clock())
    ev = [e for e in two.node.events.recent() if e["kind"] == "handover"][-1]
    assert ev["old"] == two.start.name and ev["new"] == sb.name
    assert "failover_start" not in two.kinds()


async def test_after_a_handover_the_former_active_tunnel_finds_a_new_standby(two):
    await ready(two)
    two.world.dead_peers.add(two.start.pubkey)
    await strikes(two)
    await two.svc.standby_step()
    sb = standby_srv(two)
    assert sb is not None and sb.pubkey not in (two.start.pubkey, two.svc.state.active.pubkey)
    assert two.wg.peer.pubkey == sb.pubkey, "wg0, now the standby, moved"


async def test_when_both_tunnels_fail_the_exit_holds_instead_of_switching(two):
    """The second opinion: the uplink (or the test site) is down, not the servers."""
    await ready(two)
    swaps = len(two.wg1.swaps)
    two.world.uplink_down = True
    await strikes(two)
    await two.svc.standby_step()
    assert not two.route.sets and not two.wg.swaps and len(two.wg1.swaps) == swaps
    assert not two.svc.state.blacklist.active(two.clock())
    assert "uplink_suspect" in two.kinds() and "failover_start" not in two.kinds()
    assert standby_srv(two) is not None, "the standby kept its server too"
    two.world.uplink_down = False
    await two.svc.step()
    assert two.svc.health == Health.HEALTHY and not two.route.sets


async def test_after_the_uplink_hold_the_exit_fails_over_after_all(two):
    await ready(two)
    two.world.uplink_down = True
    await strikes(two)
    two.clock.t += two.settings.uplink_hold
    await two.svc.step()
    assert "failover_start" in two.kinds()


async def test_without_a_standby_key_failover_works_as_before_and_status_says_why(
        catalog, tmp_path, monkeypatch):
    rig = make(catalog, tmp_path, monkeypatch, standby=True, standby_key=False)
    await rig.svc.start()
    await rig.svc.standby_step()
    st = rig.svc.snapshot()
    assert st["standby"]["state"] == "off"
    assert any("slot c has no key" in w for w in st["warnings"])
    rig.world.dead_peers.add(rig.start.pubkey)
    await strikes(rig)
    assert rig.wg.swaps and not rig.route.sets and "failover_done" in rig.kinds()


async def test_an_exit_without_a_second_tunnel_has_no_standby(rig):
    await rig.svc.start()
    assert rig.svc.snapshot()["standby"] is None
    assert not any("standby" in w for w in rig.svc.snapshot()["warnings"])


# Owner switches: made on the standby first ------------------------------------------------
async def test_a_rotation_is_made_on_the_standby_then_handed_over(two):
    await ready(two)
    seen = []
    swap = two.wg.swap_peer

    async def watching(pubkey, endpoint):
        seen.append(pubkey)
        await swap(pubkey, endpoint)
    two.wg.swap_peer = watching
    new = await two.svc.rotate("country", "o@github")
    assert two.route.sets == ["wg1"] and not seen, "wg0 carried traffic throughout"
    assert two.svc.state.active.pubkey == new.pubkey == two.wg1.peer.pubkey
    assert standby_srv(two).pubkey == two.start.pubkey, "the former server stays as the standby"
    ev = [e for e in two.node.events.recent() if e["kind"] == "switched"][-1]
    assert ev["handover"] is True


async def test_picking_the_standbys_own_server_is_an_instant_handover(two):
    await ready(two)
    sb = standby_srv(two)
    swaps = len(two.wg1.swaps)
    await two.svc.select_server(sb.name, sb.ip, "test")
    assert two.route.sets == ["wg1"] and len(two.wg1.swaps) == swaps


async def test_a_switch_to_another_country_moves_the_standby_there(two):
    await ready(two)
    await two.svc.select_region("Switzerland", None, "o@github", "test")
    assert two.svc.state.active.country == "Switzerland"
    await two.svc.standby_step()
    assert standby_srv(two).country == "Switzerland"


async def test_a_switch_whose_picks_all_fail_leaves_the_exit_as_it_was(two):
    await ready(two)
    two.world.dead_peers.update(s.pubkey for s in two.catalog.filter("Switzerland"))
    with pytest.raises(SwitchError):
        await two.svc.select_region("Switzerland", None, "o@github", "test")
    assert not two.route.sets and not two.wg.swaps
    assert two.svc.state.active.pubkey == two.start.pubkey and two.svc.health == Health.HEALTHY


# IPv6 and restarts -------------------------------------------------------------------------
async def test_the_ipv6_gate_goes_with_the_active_role(two):
    await ready(two)
    gate = two.settings.ipv6_gate_file
    assert gate.exists()
    two.world.dead_peers.add(two.start.pubkey)
    await strikes(two)
    assert not two.svc.standby.probe.ipv6_on and two.svc.tunnel.probe.ipv6_on
    await two.svc.step()
    assert gate.exists() and two.svc.snapshot()["ipv6"], "proven again on the new active server"


async def test_a_restart_keeps_the_roles_and_the_standby(two):
    await ready(two)
    two.world.dead_peers.add(two.start.pubkey)
    await strikes(two)
    await two.svc.standby_step()
    active, sb = two.svc.state.active, standby_srv(two)
    again = two.rebuild(two.catalog)
    await again.svc.start()
    assert again.svc.tunnel.ifname == "wg1", "the route file says wg1 is active"
    assert again.svc.state.active.pubkey == active.pubkey
    assert again.svc.state.standby.pubkey == sb.pubkey


# The standby's key -------------------------------------------------------------------------
async def test_a_first_standby_key_brings_the_standby_up(catalog, tmp_path, monkeypatch):
    rig = make(catalog, tmp_path, monkeypatch, standby=True, standby_key=False)
    await rig.svc.start()
    await rig.svc.step()
    view = await rig.node.slots.put("c", conf(new_key()), {}, "o@github")
    assert view["role"] == "standby" and view["source"] == "uploaded"
    assert rig.svc.standby_view()["state"] == "ready" and not rig.wg.swaps


async def test_a_standby_key_that_carries_nothing_is_not_kept(catalog, tmp_path, monkeypatch):
    rig = make(catalog, tmp_path, monkeypatch, standby=True, standby_key=False)
    await rig.svc.start()
    key = new_key()
    rig.world.dead_keys.add(key)
    with pytest.raises(Exception, match="no traffic"):
        await rig.node.slots.put("c", conf(key), {}, "o@github")
    assert rig.keys.source("c") == "none" and rig.svc.standby_view()["state"] == "off"
