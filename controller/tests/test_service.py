"""The exit service's health cycle: starting, failover, degraded and recovery, restarts
(component: the real node on a fake tunnel and clock)."""

from exit_vpn.health import Health
from exit_vpn.state import ActiveServer
from exit_vpn.wg import Peer
from exit_vpn.wgconf import WgConf

from harness import strikes


# Starting ------------------------------------------------------------------------------
async def test_start_adopts_the_configured_peer_and_saves_it(rig):
    await rig.svc.start()
    a = rig.svc.state.active
    assert a.name == rig.start.name and a.city == "New York"
    assert "PEER_PUBKEY" in rig.svc.store.peer_env.read_text()


async def test_a_peer_not_in_the_catalog_under_its_ip_is_not_misidentified(rig):
    """Same key, other IP: adopted by the config's name, never by a pubkey-only match."""
    rig.wg.peer = Peer(rig.start.pubkey, "203.0.113.5:51820", 0)
    await rig.svc.start()
    assert rig.svc.state.active.name == "unknown peer" and rig.svc.state.active.country == ""


async def test_the_configs_peer_name_replaces_a_stale_saved_name(rig):
    svc = rig.svc
    rig.wg.peer = Peer("cfgkey=", "203.0.113.9:51820", 0)
    svc.exit_conf = WgConf("10.2.0.2", "10.2.0.1", "cfgkey=", "203.0.113.9:51820", "US-CA#556")
    svc.state.active = ActiveServer("SE-US#1", "cfgkey=", "203.0.113.9:51820", "United States", "", 5.0)
    await svc.start()
    assert svc.state.active.name == "US-CA#556" and svc.state.active.since == 5.0


async def test_an_unknown_servers_country_is_learned_from_the_exit(rig):
    rig.wg.peer = Peer("cfgkey=", "203.0.113.9:51820", 0)
    await rig.svc.start()
    await rig.svc.step()   # FakeHealth reports loc=US
    assert rig.svc.state.active.country == "United States"
    assert rig.svc.test_servers(3), "servers in the learned country"
    assert '"country": "United States"' in rig.svc.store.path.read_text(), "and it is saved"


# Failover ------------------------------------------------------------------------------
async def test_a_silently_dead_server_fails_over_after_three_strikes(rig):
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


async def test_with_automatic_failover_off_an_unhealthy_exit_only_reports(rig):
    await rig.svc.start()
    await rig.svc.update_prefs("o@github", auto_failover=False)
    rig.wg.dead_peers.add(rig.start.pubkey)
    await strikes(rig)
    assert not rig.wg.swaps and "unhealthy_no_failover" in rig.kinds()


# Degraded ------------------------------------------------------------------------------
async def test_with_every_server_down_the_exit_degrades_then_recovers(rig, catalog):
    """A retry that finds the server working again recovers without a new swap."""
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


async def test_a_degraded_retry_fails_over_again(rig, catalog):
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


async def test_degraded_with_automatic_failover_off_only_waits(rig, catalog):
    svc = rig.svc
    await svc.start()
    rig.wg.dead_peers.update(s.pubkey for s in catalog.servers)
    await strikes(rig)
    await svc.update_prefs("o@github", auto_failover=False)
    swaps = len(rig.wg.swaps)
    await svc.step()
    assert svc.health == Health.DEGRADED and len(rig.wg.swaps) == swaps


# Restarts ------------------------------------------------------------------------------
async def test_events_and_state_survive_a_restart(rig, catalog):
    await rig.svc.start()
    await rig.svc.rotate("country", "o@github")
    await rig.svc.step()
    before = rig.kinds()
    again = rig.rebuild(catalog)
    assert again.kinds() == before, "the event log is kept across restarts"
    assert again.svc.state.active == rig.svc.state.active
