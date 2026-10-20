"""Controller flows with a fake tunnel and fake probes on a fake clock."""

import random
from pathlib import Path

import pytest

from exit_vpn.config import Settings
from exit_vpn.controller import Controller, SwitchError
from exit_vpn.health import Health
from exit_vpn.probes import CycleResult
from exit_vpn.state import StateStore
from exit_vpn.wg import Peer
from exit_vpn.wgconf import WgConf


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeWg:
    def __init__(self, clock, pubkey, endpoint):
        self.clock, self.peer = clock, Peer(pubkey, endpoint, int(clock()), 0, 0)
        self.swaps = []

    async def peers(self):
        return [self.peer]

    async def swap_peer(self, pubkey, endpoint):
        self.swaps.append(pubkey)
        self.peer = Peer(pubkey, endpoint, int(self.clock()), 0, 0)


class FakeProber:
    """Healthy unless the current peer's pubkey is in `dead`."""

    def __init__(self, wg, clock, dead=()):
        self.wg, self.clock, self.dead = wg, clock, set(dead)

    async def cycle(self, latest_handshake, now=None):
        self.clock.t += 1.0  # each cycle takes time, so warm-up deadlines are reached
        ok = self.wg.peer.pubkey not in self.dead
        return CycleResult(ok, ok, ok, 1.0, "9.9.9.9" if ok else None, "US" if ok else None,
                           [] if ok else ["https: timeout"])


@pytest.fixture
def setup(catalog, tmp_path, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr("exit_vpn.controller.asyncio.sleep", no_sleep)
    clock = Clock()
    start = catalog.filter("United States", "New York")[0]
    wg = FakeWg(clock, start.pubkey, start.endpoint(51820))
    prober = FakeProber(wg, clock)
    s = Settings(vpn_conf=Path("/dev/null"), state_dir=tmp_path, owners=["o@github"])
    conf = WgConf("10.2.0.2", "10.2.0.1", start.pubkey, start.endpoint(51820))
    ctl = Controller(s, conf, wg, prober, catalog,
                     StateStore(s.state_file, s.peer_env_file, s.countries),
                     clock=clock, rng=random.Random(0))
    return ctl, wg, prober, clock, start


async def test_init_adopts_peer_and_persists(setup):
    ctl, wg, _, _, start = setup
    await ctl.init()
    assert ctl.state.current.name == start.name and ctl.state.current.city == "New York"
    assert "PEER_PUBKEY" in ctl.store.peer_env.read_text()


async def test_silent_death_fails_over_after_three_strikes(setup):
    ctl, wg, prober, clock, start = setup
    await ctl.init()
    await ctl.health_step()
    assert ctl.tracker.state == Health.HEALTHY
    prober.dead.add(start.pubkey)
    clock.t += 30  # past the warm-up
    await ctl.health_step(); await ctl.health_step()
    assert ctl.tracker.state == Health.SUSPECT and not wg.swaps
    await ctl.health_step()
    assert wg.swaps, "third strike must fail over"
    assert ctl.tracker.state == Health.HEALTHY and wg.peer.pubkey != start.pubkey
    assert ctl.state.blacklisted(start.pubkey, clock())
    assert ctl.state.current.country == "United States"  # same city/country tier first
    kinds = [e["kind"] for e in ctl.state.events]
    assert "failover_start" in kinds and "failover_done" in kinds


async def test_dead_candidates_are_skipped_and_blacklisted(setup, catalog):
    ctl, wg, prober, clock, start = setup
    await ctl.init()
    ny = [s.pubkey for s in catalog.filter("United States", "New York")]
    prober.dead.update(ny)  # the whole city is down
    clock.t += 30
    for _ in range(3):
        await ctl.health_step()
    assert ctl.state.current.city != "New York" and ctl.tracker.state == Health.HEALTHY
    assert all(ctl.state.blacklisted(p, clock()) for p in wg.swaps[:-1])


async def test_everything_dead_degrades(setup, catalog):
    ctl, wg, prober, clock, _ = setup
    await ctl.init()
    prober.dead.update(s.pubkey for s in catalog.servers)
    clock.t += 30
    for _ in range(3):
        await ctl.health_step()
    assert ctl.tracker.state == Health.DEGRADED
    assert "degraded" in [e["kind"] for e in ctl.state.events]


async def test_auto_failover_off(setup):
    ctl, wg, prober, clock, start = setup
    await ctl.init()
    ctl.state.prefs.auto_failover = False
    prober.dead.add(start.pubkey)
    clock.t += 30
    for _ in range(3):
        await ctl.health_step()
    assert not wg.swaps
    assert "unhealthy_no_failover" in [e["kind"] for e in ctl.state.events]


async def test_rotate_country(setup):
    ctl, wg, _, _, start = setup
    await ctl.init()
    new = await ctl.switch(ctl.candidates("country"), "test")
    assert new.pubkey != start.pubkey and new.country == "United States"
    assert ctl.state.current.pubkey == new.pubkey == wg.peer.pubkey


async def test_select_failure_reverts(setup, catalog):
    ctl, wg, prober, _, start = setup
    await ctl.init()
    ch = catalog.filter("Switzerland")
    prober.dead.update(s.pubkey for s in ch)
    with pytest.raises(SwitchError):
        await ctl.switch(ctl.select_candidates(None, "Switzerland", None), "test")
    assert wg.peer.pubkey == start.pubkey and ctl.state.current.pubkey == start.pubkey
    assert "switch_reverted" in [e["kind"] for e in ctl.state.events]


async def test_select_by_name(setup, catalog):
    ctl, wg, _, _, _ = setup
    await ctl.init()
    target = catalog.filter("Switzerland")[0]
    await ctl.switch(ctl.select_candidates(target.name, None, None), "test")
    assert ctl.state.current.name == target.name


async def test_init_unknown_config_peer_is_not_misidentified(setup, catalog):
    """The config's peer is not in the catalog under its own IP (Phase 3 finding):
    adopt it by name from the config, never by a pubkey-only match."""
    ctl, wg, _, _, start = setup
    wg.peer = Peer(start.pubkey, "203.0.113.5:51820", 0, 0, 0)  # same key, other IP
    await ctl.init()
    assert ctl.state.current.name == "unknown peer" and ctl.state.current.country == ""


async def test_init_prefers_config_name_over_stale_state(setup):
    """A stale saved name (e.g. from a pubkey-only match) is replaced at start."""
    from exit_vpn.state import Current
    ctl, wg, _, _, _ = setup
    wg.peer = Peer("cfgkey=", "203.0.113.9:51820", 0, 0, 0)
    ctl.conf = WgConf("10.2.0.2", "10.2.0.1", "cfgkey=", "203.0.113.9:51820", "US-CA#556")
    ctl.state.current = Current("SE-US#1", "cfgkey=", "203.0.113.9:51820", "United States", "", 5.0)
    await ctl.init()
    assert ctl.state.current.name == "US-CA#556" and ctl.state.current.since == 5.0


async def test_unknown_server_gets_catalog_country_from_exit(setup):
    ctl, wg, _, _, _ = setup
    wg.peer = Peer("cfgkey=", "203.0.113.9:51820", 0, 0, 0)
    await ctl.init()
    await ctl.health_step()  # FakeProber reports loc=US
    assert ctl.state.current.country == "United States"
    assert ctl.candidates("country"), "rotate within the country must find candidates"
