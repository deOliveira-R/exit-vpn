"""Measuring a server's quality through wg-probe (component: the measurer over a faked
tunnel, exit check, pings and HTTPS fetch)."""

from prober.measure import SETTLE, answer_ok, dns_query

from harness import TARGET, FakeExitCheck, FakeTunnel, Pings, measurer, prober, request

MEASURE = {"server": {"name": "US-NY#1", "ip": "198.51.100.1", "pubkey": "pk=", "country_code": "US"},
           "seconds": 10}


async def test_a_measurement_attaches_proves_the_exit_settles_then_samples_each_second():
    t, pings = FakeTunnel(), Pings(*([None] * SETTLE), 31.0, None)
    r = await measurer(prober(t), pings).run(TARGET, 10)
    assert t.attached == [("pk=", "198.51.100.1")] and t.detached == 1
    assert r["exit_ip"] == "203.0.113.7" and r["error"] == ""
    assert r["pings"][:3] == [31.0, None, 20.0] and len(r["pings"]) == 10, "the settle is not counted"
    assert r["https"] == [150.0, 150.0], "every fifth ping"
    assert pings.servers == {"10.64.0.1"}, "the resolver the key's config names (any provider)"


async def test_a_key_without_a_dns_server_cannot_be_measured():
    t = FakeTunnel()
    r = await measurer(prober(t), resolver="").run(TARGET, 10)
    assert "DNS" in r["error"] and r["pings"] == [] and t.attached == []


async def test_a_failed_https_check_is_none_and_a_failed_exit_check_an_error():
    r = await measurer(prober(), https=(None, "", "timeout")).run(TARGET, 10)
    assert r["https"] == [None, None]
    r = await measurer(prober(FakeTunnel(), FakeExitCheck(fail="no traffic"))).run(TARGET, 10)
    assert r["error"] == "no traffic" and r["pings"] == []


async def test_measure_over_http_and_its_limits():
    p = prober()
    r = await request(p, "POST", "/measure", MEASURE, measurer=measurer(p))
    assert r.status_code == 200 and len(r.json()["pings"]) == 10
    bad = await request(p, "POST", "/measure", {**MEASURE, "seconds": 3600}, measurer=measurer(p))
    assert bad.status_code == 422


def test_a_dns_ping_answer_must_match_its_query():
    q = dns_query("www.cloudflare.com", 7)
    resp = bytearray(q)
    resp[2:4] = (0x8180).to_bytes(2, "big")
    resp[6:8] = (1).to_bytes(2, "big")
    assert answer_ok(bytes(resp), 7) and not answer_ok(bytes(resp), 8) and not answer_ok(q, 7)
