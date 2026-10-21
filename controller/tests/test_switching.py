"""Changing the server on purpose: rotate, select a region or an exact server, Secure Core
mode, preference updates (component: the real node on a fake tunnel and clock)."""

import pytest

from exit_vpn.errors import PolicyError, SwitchError
from exit_vpn.servers import Region

from harness import strikes


def p2p_in_chicago(catalog):
    return next(s for s in catalog.filter("United States", "Chicago") if s.port_forward)


# Rotate and select a region ------------------------------------------------------------
async def test_rotating_within_the_country_moves_wg0_to_another_server(rig):
    await rig.svc.start()
    new = await rig.svc.rotate("country", "o@github")
    assert new.pubkey != rig.start.pubkey and new.country == "United States"
    assert rig.svc.state.active.pubkey == new.pubkey == rig.wg.peer.pubkey


async def test_selecting_a_region_takes_the_catalogs_spelling(rig):
    await rig.svc.start()
    srv = await rig.svc.select_region("switzerland", None, "o@github", "test")
    assert srv.country == "Switzerland"


async def test_selecting_an_unknown_country_is_refused(rig):
    await rig.svc.start()
    with pytest.raises(PolicyError, match="unknown country"):
        await rig.svc.select_region("Atlantis", None, "o@github", "test")


async def test_a_switch_where_nothing_works_reverts_to_the_old_server(rig, catalog):
    await rig.svc.start()
    rig.wg.dead_peers.update(s.pubkey for s in catalog.filter("Switzerland"))
    with pytest.raises(SwitchError):
        await rig.svc.select_region("Switzerland", None, "o@github", "test")
    assert rig.wg.peer.pubkey == rig.start.pubkey == rig.svc.state.active.pubkey
    assert "switch_reverted" in rig.kinds()


# Exact servers -------------------------------------------------------------------------
async def test_a_server_is_selected_by_name(rig, catalog):
    await rig.svc.start()
    target = catalog.filter("Switzerland")[0]
    await rig.svc.select_server(target.name, None, "test")
    assert rig.svc.state.active.name == target.name


async def test_an_exact_pick_keeps_its_features_through_failover(rig, catalog):
    svc = rig.svc
    await svc.start()
    target = p2p_in_chicago(catalog)
    await svc.select_server(target.name, target.ip, "test")
    assert list(svc.state.active.profile) == target.features() == ["port_forward", "stream"]
    rig.wg.dead_peers.add(target.pubkey)
    await strikes(rig)
    new = svc.current_server()
    assert new.port_forward and new.city == "Chicago"
    assert list(svc.state.active.profile) == target.features()


async def test_rotation_keeps_an_exact_picks_features_and_a_region_select_clears_them(rig, catalog):
    svc = rig.svc
    await svc.start()
    target = p2p_in_chicago(catalog)
    await svc.select_server(target.name, target.ip, "test")
    assert (await svc.rotate("country", "o@github")).port_forward
    await svc.select_region("Switzerland", None, "o@github", "test")
    assert svc.state.active.profile == ()


async def test_an_exact_pick_at_an_ip_it_does_not_have_is_refused(rig, catalog):
    await rig.svc.start()
    with pytest.raises(PolicyError, match="no server"):
        await rig.svc.select_server(catalog.servers[0].name, "203.0.113.1", "test")


async def test_an_exact_pick_must_match_the_secure_core_mode(rig, catalog):
    svc = rig.svc
    await svc.start()
    sc = next(s for s in catalog.servers if s.secure_core)
    with pytest.raises(PolicyError, match="turn it on"):
        await svc.select_server(sc.name, None, "test")
    await svc.update_prefs("o@github", secure_core=True)
    with pytest.raises(PolicyError, match="pick a Secure Core server"):
        await svc.select_server(p2p_in_chicago(catalog).name, None, "test")


# Secure Core and preference updates ----------------------------------------------------
async def test_turning_secure_core_on_and_off_switches_servers(rig):
    svc = rig.svc
    await svc.start()
    await svc.update_prefs("o@github", secure_core=True)
    assert svc.current_server().secure_core and svc.prefs.secure_core
    assert svc.snapshot()["entry"] in ("CH", "SE", "IS")
    await svc.update_prefs("o@github", secure_core=False)
    assert not svc.current_server().secure_core and not svc.prefs.secure_core


async def test_a_secure_core_change_publishes_one_prefs_event_after_the_switch(rig):
    svc = rig.svc
    await svc.start()
    n = len(rig.node.events.recent())
    await svc.update_prefs("o@github", secure_core=True)
    new = rig.node.events.recent()[n:]
    assert [e["prefs"]["secure_core"] for e in new if e["kind"] == "prefs"] == [True]
    assert [e["kind"] for e in new][-1] == "prefs", "after the switch"


async def test_secure_core_with_no_such_server_in_reach_is_refused(rig, catalog):
    """The fixture has no Secure Core exit in Switzerland: pinned there, nothing to switch to."""
    svc = rig.svc
    await svc.start()
    await svc.update_prefs("o@github", regions=[Region("Switzerland")])
    await svc.select_region("Switzerland", None, "o@github", "test")
    with pytest.raises(SwitchError, match="pinned regions"):
        await svc.update_prefs("o@github", secure_core=True)
    assert not svc.prefs.secure_core and not svc.current_server().secure_core


async def test_a_failed_secure_core_switch_keeps_every_other_change_out(rig, catalog):
    svc = rig.svc
    await svc.start()
    before = svc.prefs
    rig.wg.dead_peers.update(s.pubkey for s in catalog.servers if s.secure_core)
    with pytest.raises(SwitchError):
        await svc.update_prefs("o@github", secure_core=True, regions=[Region("Switzerland")],
                               favorites=[catalog.servers[0].name])
    assert svc.prefs == before and rig.wg.peer.pubkey == rig.start.pubkey
    assert "prefs" not in rig.kinds()


async def test_a_refused_preference_update_changes_nothing(rig):
    svc = rig.svc
    await svc.start()
    before = svc.prefs
    with pytest.raises(PolicyError):
        await svc.update_prefs("o@github", auto_failover=False, regions=[Region("Atlantis")])
    assert svc.prefs == before and "prefs" not in rig.kinds()
