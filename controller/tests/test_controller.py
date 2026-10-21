"""The exit service: health cycle, failover, degraded/recovery, switching, preferences, on a
fake tunnel and a fake clock."""

import pytest

from exit_vpn.errors import PolicyError, SwitchError
from exit_vpn.health import Health
from exit_vpn.servers import Region
from exit_vpn.state import ActiveServer
from exit_vpn.wg import Peer
from exit_vpn.wgconf import WgConf



async def strikes(rig, n=3):
    rig.clock.t += 30   # past the warm-up
    for _ in range(n):
        await rig.svc.step()


async def test_start_adopts_peer_and_persists(rig):
    await rig.svc.start()
    a = rig.svc.state.active
    assert a.name == rig.start.name and a.city == "New York"
    assert "PEER_PUBKEY" in rig.svc.store.peer_env.read_text()


async def test_silent_death_fails_over_after_three_strikes(rig):
    svc = rig.svc
    await svc.start()
    await svc.step()
    assert svc.health == Health.HEALTHY
    rig.wg.dead_peers.add(rig.start.pubkey)
    await strikes(rig, 2)
    assert svc.health == Health.SUSPECT and not rig.wg.swaps
    await svc.step()
    assert rig.wg.swaps, "third strike must fail over"
    assert svc.health == Health.HEALTHY and rig.wg.peer.pubkey != rig.start.pubkey
    assert svc.state.blacklist.contains(rig.start.pubkey, rig.clock())
    assert svc.state.active.country == "United States"   # same city/country tier first
    assert {"failover_start", "failover_done"} <= set(rig.kinds())


async def test_dead_candidates_are_skipped_and_blacklisted(rig, catalog):
    svc = rig.svc
    await svc.start()
    rig.wg.dead_peers.update(s.pubkey for s in catalog.filter("United States", "New York"))
    await strikes(rig)
    assert svc.state.active.city != "New York" and svc.health == Health.HEALTHY
    assert all(svc.state.blacklist.contains(p, rig.clock()) for p in rig.wg.swaps[:-1])


async def test_everything_dead_degrades_then_recovers(rig, catalog):
    """The degraded branch of the one health cycle: with every server down the exit
    degrades; a retry that finds the server working again recovers."""
    svc = rig.svc
    await svc.start()
    rig.wg.dead_peers.update(s.pubkey for s in catalog.servers)
    await strikes(rig)
    assert svc.health == Health.DEGRADED and "degraded" in rig.kinds()
    tried = len(rig.wg.swaps)
    rig.wg.dead_peers.clear()        # the provider is back
    await svc.step()
    assert svc.health == Health.HEALTHY and rig.kinds()[-1] == "recovered"
    assert len(rig.wg.swaps) == tried, "the server it was on works again: no new swap"


async def test_degraded_retry_fails_over_again(rig, catalog):
    svc = rig.svc
    await svc.start()
    rig.wg.dead_peers.update(s.pubkey for s in catalog.servers)
    await strikes(rig)
    assert svc.health == Health.DEGRADED
    stuck = rig.wg.peer.pubkey
    rig.wg.dead_peers = {stuck}               # everything is back but the server it is on
    rig.clock.t += svc.s.blacklist_ttl + 1    # and the blacklist has expired
    await svc.step()
    assert svc.health == Health.HEALTHY and rig.wg.peer.pubkey != stuck
    assert rig.kinds()[-1] == "failover_done"


async def test_degraded_without_auto_failover_only_waits(rig, catalog):
    svc = rig.svc
    await svc.start()
    rig.wg.dead_peers.update(s.pubkey for s in catalog.servers)
    await strikes(rig)
    await svc.update_prefs("o@github", auto_failover=False)
    swaps = len(rig.wg.swaps)
    await svc.step()
    assert svc.health == Health.DEGRADED and len(rig.wg.swaps) == swaps


async def test_auto_failover_off(rig):
    await rig.svc.start()
    await rig.svc.update_prefs("o@github", auto_failover=False)
    rig.wg.dead_peers.add(rig.start.pubkey)
    await strikes(rig)
    assert not rig.wg.swaps and "unhealthy_no_failover" in rig.kinds()


async def test_rotate_country(rig):
    await rig.svc.start()
    new = await rig.svc.rotate("country", "o@github")
    assert new.pubkey != rig.start.pubkey and new.country == "United States"
    assert rig.svc.state.active.pubkey == new.pubkey == rig.wg.peer.pubkey


async def test_select_failure_reverts(rig, catalog):
    await rig.svc.start()
    rig.wg.dead_peers.update(s.pubkey for s in catalog.filter("Switzerland"))
    with pytest.raises(SwitchError):
        await rig.svc.select_region("Switzerland", None, "o@github", "test")
    assert rig.wg.peer.pubkey == rig.start.pubkey == rig.svc.state.active.pubkey
    assert "switch_reverted" in rig.kinds()


async def test_select_region_and_unknown_places(rig, catalog):
    await rig.svc.start()
    srv = await rig.svc.select_region("switzerland", None, "o@github", "test")
    assert srv.country == "Switzerland"
    with pytest.raises(PolicyError, match="unknown country"):
        await rig.svc.select_region("Atlantis", None, "o@github", "test")


async def test_select_by_name(rig, catalog):
    await rig.svc.start()
    target = catalog.filter("Switzerland")[0]
    await rig.svc.select_server(target.name, None, "test")
    assert rig.svc.state.active.name == target.name


async def test_start_unknown_config_peer_is_not_misidentified(rig):
    """The config's peer is not in the catalog under its own IP (Phase 3 finding):
    adopt it by name from the config, never by a pubkey-only match."""
    rig.wg.peer = Peer(rig.start.pubkey, "203.0.113.5:51820", 0)   # same key, other IP
    await rig.svc.start()
    assert rig.svc.state.active.name == "unknown peer" and rig.svc.state.active.country == ""


async def test_start_prefers_config_name_over_stale_state(rig):
    """A stale saved name (e.g. from a pubkey-only match) is replaced at start."""
    svc = rig.svc
    rig.wg.peer = Peer("cfgkey=", "203.0.113.9:51820", 0)
    svc.exit_conf = WgConf("10.2.0.2", "10.2.0.1", "cfgkey=", "203.0.113.9:51820", "US-CA#556")
    svc.state.active = ActiveServer("SE-US#1", "cfgkey=", "203.0.113.9:51820", "United States", "", 5.0)
    await svc.start()
    assert svc.state.active.name == "US-CA#556" and svc.state.active.since == 5.0


async def test_unknown_server_gets_catalog_country_from_exit(rig):
    rig.wg.peer = Peer("cfgkey=", "203.0.113.9:51820", 0)
    await rig.svc.start()
    await rig.svc.step()   # FakeHealth reports loc=US
    assert rig.svc.state.active.country == "United States"
    assert rig.svc.test_servers(3), "servers in the learned country"
    assert '"country": "United States"' in rig.svc.store.path.read_text(), "and it is saved"


async def test_exact_pick_keeps_p2p_through_failover(rig, catalog):
    svc = rig.svc
    await svc.start()
    target = next(s for s in catalog.filter("United States", "Chicago") if s.port_forward)
    await svc.select_server(target.name, target.ip, "test")
    assert list(svc.state.active.profile) == target.features() == ["port_forward", "stream"]
    rig.wg.dead_peers.add(target.pubkey)
    await strikes(rig)
    new = svc.current_server()
    assert new.port_forward and new.city == "Chicago"
    assert list(svc.state.active.profile) == target.features()
    # rotation keeps the profile; a region select clears it
    assert (await svc.rotate("country", "o@github")).port_forward
    await svc.select_region("Switzerland", None, "o@github", "test")
    assert svc.state.active.profile == ()


async def test_exact_pick_rules(rig, catalog):
    await rig.svc.start()
    sc = next(s for s in catalog.servers if s.secure_core)
    with pytest.raises(PolicyError, match="turn it on"):
        await rig.svc.select_server(sc.name, None, "test")
    with pytest.raises(PolicyError, match="no server"):
        await rig.svc.select_server(catalog.servers[0].name, "203.0.113.1", "test")


async def test_secure_core_toggle_switches_mode(rig):
    svc = rig.svc
    await svc.start()
    await svc.update_prefs("o@github", secure_core=True)
    assert svc.current_server().secure_core and svc.prefs.secure_core
    assert svc.snapshot()["entry"] in ("CH", "SE", "IS")
    await svc.update_prefs("o@github", secure_core=False)
    assert not svc.current_server().secure_core and not svc.prefs.secure_core


async def test_prefs_change_is_all_or_nothing(rig, catalog):
    """A Secure Core switch that fails keeps every other change out too (it used to
    save regions and favourites before the switch, then answer 409)."""
    svc = rig.svc
    await svc.start()
    before = svc.prefs
    rig.wg.dead_peers.update(s.pubkey for s in catalog.servers if s.secure_core)
    with pytest.raises(SwitchError):
        await svc.update_prefs("o@github", secure_core=True, regions=[Region("Switzerland")],
                               favorites=[catalog.servers[0].name])
    assert svc.prefs == before and rig.wg.peer.pubkey == rig.start.pubkey
    assert "prefs" not in rig.kinds()
    with pytest.raises(PolicyError):    # one bad region: nothing changes either
        await svc.update_prefs("o@github", auto_failover=False, regions=[Region("Atlantis")])
    assert svc.prefs == before


async def test_events_and_state_survive_a_restart(rig, catalog):
    await rig.svc.start()
    await rig.svc.rotate("country", "o@github")
    await rig.svc.step()
    before = rig.kinds()
    again = rig.rebuild(catalog)
    assert again.kinds() == before, "the event log is kept across restarts"
    assert again.svc.state.active == rig.svc.state.active
