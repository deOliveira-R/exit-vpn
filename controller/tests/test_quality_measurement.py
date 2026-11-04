"""Measuring both exit tunnels' quality: pings on the tunnels that hold a server, `quality`
events on changes, the quality in status, a fresh meter on every new server, the quality log
(component: the real node on two fake tunnels, a fake clock)."""

import json

import pytest

from harness import make, strikes


@pytest.fixture
def two(catalog, tmp_path, monkeypatch):
    return make(catalog, tmp_path, monkeypatch, standby=True)


async def ready(rig):
    await rig.svc.start()
    await rig.svc.step()
    await rig.svc.standby_step()
    assert rig.svc.standby_view()["state"] == "ready"


async def seconds(rig, n):
    for _ in range(n):
        rig.clock.t += 1
        await rig.svc.quality_step()


def quality_events(rig):
    return [e for e in rig.node.events.recent() if e["kind"] == "quality"]


async def test_both_tunnels_become_healthy_and_status_shows_their_quality(two):
    await ready(two)
    await seconds(two, 30)
    st = two.svc.snapshot()
    assert st["quality"]["state"] == "healthy" and st["quality"]["rtt_ms"] == 20.0
    assert st["quality"]["pings"] == 30 and st["quality"]["loss"] == 0
    assert st["standby"]["quality"]["state"] == "healthy"
    assert {(e["role"], e["state"]) for e in quality_events(two)} == {
        ("active", "healthy"), ("standby", "healthy")}


async def test_a_dropped_active_server_is_critical_within_seconds_and_nothing_switches(two):
    await ready(two)
    await seconds(two, 40)
    two.world.dead_peers.add(two.start.pubkey)
    await seconds(two, 5)
    assert two.svc.snapshot()["quality"]["state"] == "critical"
    ev = quality_events(two)[-1]
    assert (ev["role"], ev["state"], ev["server"]) == ("active", "critical", two.start.name)
    assert "handover" not in two.kinds() and not two.wg.swaps, "measurement only"


async def test_a_slow_server_is_degraded(two):
    await ready(two)
    await seconds(two, 40)
    two.world.rtt[two.start.pubkey] = 200.0
    await seconds(two, 20)
    q = two.svc.snapshot()["quality"]
    assert q["state"] == "degraded" and q["reasons"][0].startswith("RTT p90 200 ms")


async def test_quality_moves_with_the_tunnel_and_a_new_server_starts_afresh(two):
    await ready(two)
    await seconds(two, 40)
    standby = two.svc.state.standby
    two.world.dead_peers.add(two.start.pubkey)
    await strikes(two)   # handed over to the standby
    assert "handover" in two.kinds() and two.svc.state.active == standby
    q = two.svc.snapshot()["quality"]
    assert q["state"] == "healthy" and q["pings"] == 40, "the new active's own record, kept"
    await two.svc.standby_step()   # the former active's server is blacklisted: the standby moves
    assert two.svc.state.standby.pubkey != two.start.pubkey
    assert two.svc.snapshot()["standby"]["quality"]["pings"] == 0, "its new server: no history"


async def test_a_searching_standby_is_not_measured(catalog, tmp_path, monkeypatch):
    rig = make(catalog, tmp_path, monkeypatch, standby=True, standby_key=False)
    await rig.svc.start()
    await rig.svc.step()
    await seconds(rig, 3)
    assert rig.svc.snapshot()["standby"]["quality"] is None
    assert [role for role, _, _ in rig.svc._measured()] == ["active"]


async def test_the_quality_log_keeps_a_summary_per_tunnel_each_minute(two):
    await ready(two)
    await seconds(two, 130)
    lines = [json.loads(x) for x in two.svc.quality_log.path.read_text().splitlines()]
    assert {x["role"] for x in lines} == {"active", "standby"}
    assert len(lines) == 6, "at 0, 60 and 120 s, for both"
    last = lines[-1]
    assert last["state"] == "healthy" and last["rtt_p50"] == 20.0 and last["interface"] in ("wg0", "wg1")


def test_the_quality_log_rotates_at_its_size_limit(tmp_path):
    from exit_vpn.persistence import QualityLog
    log = QualityLog(tmp_path / "q.jsonl", max_bytes=100)
    for i in range(10):
        log.append({"i": i, "pad": "x" * 30})
    assert (tmp_path / "q.jsonl.1").exists()
    assert (tmp_path / "q.jsonl").stat().st_size <= 100 + 50


async def test_a_fresh_sessions_warm_up_drops_are_not_measured(two):
    await two.svc.start()                       # adopted at start: warming up until a cycle passes
    two.world.dead_peers.add(two.start.pubkey)  # the warm-up drops traffic
    await seconds(two, 10)
    assert two.svc.snapshot()["quality"]["pings"] == 0 and not quality_events(two)
    two.world.dead_peers.discard(two.start.pubkey)
    await two.svc.step()                        # the first passing cycle ends the warm-up
    await seconds(two, 30)
    q = two.svc.snapshot()["quality"]
    assert q["state"] == "healthy" and q["loss"] == 0
    assert [e["state"] for e in quality_events(two) if e["role"] == "active"] == ["healthy"]
