"""Our side of system interfaces: `wg` output, WireGuard configs, the DNS and trace probes,
Tailscale whois (boundary)."""

import struct

from exit_vpn.auth import decide
from exit_vpn import probes
from exit_vpn.probes import HealthProbe, dns_answer_ok, dns_query, parse_trace
from exit_vpn.routes import GateFile
from exit_vpn.wg import parse_dump
from exit_vpn.wgconf import parse

OWNERS = ["owner@github"]


def test_whois_accepts_an_owners_untagged_device():
    ok, who = decide({"Node": {"ComputedName": "mac", "Tags": None},
                      "UserProfile": {"LoginName": "owner@github"}}, OWNERS)
    assert ok and "mac" in who


def test_whois_refuses_tagged_devices_and_strangers():
    assert not decide({"Node": {"Tags": ["tag:server"]},
                       "UserProfile": {"LoginName": "tagged-devices"}}, OWNERS)[0]
    assert not decide({"Node": {"Tags": []}, "UserProfile": {"LoginName": "eve@github"}}, OWNERS)[0]
    assert not decide({}, OWNERS)[0]


def test_the_trace_answer_is_parsed():
    assert parse_trace("fl=1\nip=1.2.3.4\nloc=US\n")["ip"] == "1.2.3.4"


def test_a_dns_answer_must_match_the_query_and_succeed():
    q = dns_query("www.example.com", 0x1234)
    assert q[:2] == b"\x12\x34" and q.endswith(b"\x00\x01\x00\x01")
    resp = struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0)
    assert dns_answer_ok(resp, 0x1234)
    assert not dns_answer_ok(resp, 0x9999)                                       # wrong id
    assert not dns_answer_ok(struct.pack("!HHHHHH", 0x1234, 0x8183, 1, 0, 0, 0), 0x1234)  # NXDOMAIN


def test_wg_dump_gives_the_peers_endpoint_and_handshake():
    dump = ("PRIV\tPUB\t51820\toff\n"
            "peerkey=\t(none)\t1.2.3.4:51820\t0.0.0.0/0,::/0\t1700000000\t100\t200\t25\n")
    (p,) = parse_dump(dump)
    assert p.pubkey == "peerkey=" and p.endpoint == "1.2.3.4:51820" and p.latest_handshake == 1700000000


def test_a_wireguard_config_is_read_without_its_private_key(tmp_path):
    f = tmp_path / "a.conf"
    f.write_text("[Interface]\n# Key for exit-proton\n# NetShield = 0\n# VPN Accelerator = on\n"
                 "PrivateKey = SECRETSECRETSECRET=\nAddress = 10.2.0.2/32, 2a07:b944::2:2/128\n"
                 "DNS = 10.2.0.1, 2a07:b944::2:1\n\n[Peer]\n# US-CA#556\nPublicKey = pk=\n"
                 "AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = 1.2.3.4:51820\n")
    c = parse(f)
    assert (c.address, c.dns, c.peer_pubkey, c.peer_endpoint, c.peer_name) == \
        ("10.2.0.2", "10.2.0.1", "pk=", "1.2.3.4:51820", "US-CA#556")
    assert c.options == {"NetShield": "0", "VPN Accelerator": "on"}
    assert "SECRET" not in repr(c)


def test_a_wireguard_configs_ipv6_address_is_read_and_optional(tmp_path):
    f = tmp_path / "a.conf"
    body = ("[Interface]\nPrivateKey = K=\nAddress = {}\nDNS = 10.2.0.1\n\n"
            "[Peer]\nPublicKey = pk=\nEndpoint = 1.2.3.4:51820\n")
    f.write_text(body.format("10.2.0.2/32, 2a07:b944::2:2/128"))
    assert (parse(f).address, parse(f).address6) == ("10.2.0.2", "2a07:b944::2:2")
    f.write_text(body.format("10.2.0.2/32"))
    assert parse(f).address6 == ""


def _probe(source6):
    return HealthProbe("10.2.0.2", "10.2.0.1", ["https://v4/cdn-cgi/trace"], "n", 1, 1, 180,
                       source6=source6, url6="https://v6/cdn-cgi/trace")


async def test_the_ipv6_probe_is_bound_to_the_tunnels_ipv6_address_and_never_decides_health(monkeypatch):
    calls = []

    async def https(url, source, timeout):
        calls.append((url, source))
        if source == "2a07:b944::2:2":
            return False, {}, f"{url}: ConnectTimeout"
        return True, {"ip": "198.51.100.1", "loc": "US"}, ""

    async def dns(*a):
        return True, ""
    monkeypatch.setattr(probes, "https_probe", https)
    monkeypatch.setattr(probes, "dns_probe", dns)
    r = await _probe("2a07:b944::2:2").cycle(1000, now=1001)
    assert ("https://v6/cdn-cgi/trace", "2a07:b944::2:2") in calls
    assert r.ok and r.errors == [], "IPv6 failing is not a health failure"
    assert r.ipv6_ok is False and r.exit_ip6 is None and "ipv6:" in r.ipv6_error


async def test_a_working_ipv6_probe_reports_the_ipv6_exit_address(monkeypatch):
    async def https(url, source, timeout):
        return True, {"ip": "2001:db8::1" if ":" in source else "198.51.100.1"}, ""

    async def dns(*a):
        return True, ""
    monkeypatch.setattr(probes, "https_probe", https)
    monkeypatch.setattr(probes, "dns_probe", dns)
    r = await _probe("2a07:b944::2:2").cycle(1000, now=1001)
    assert (r.ipv6_ok, r.exit_ip6, r.exit_ip) == (True, "2001:db8::1", "198.51.100.1")


async def test_without_an_ipv6_address_there_is_no_ipv6_probe(monkeypatch):
    calls = []

    async def https(url, source, timeout):
        calls.append(source)
        return True, {}, ""

    async def dns(*a):
        return True, ""
    monkeypatch.setattr(probes, "https_probe", https)
    monkeypatch.setattr(probes, "dns_probe", dns)
    r = await _probe("").cycle(1000, now=1001)
    assert r.ipv6_ok is None and calls == ["10.2.0.2"]


def test_the_ipv6_gate_file_exists_only_while_open(tmp_path):
    f = GateFile(tmp_path / "run/exit-vpn/ipv6")
    f.set(True)
    assert f.is_set()
    f.set(False)
    assert not f.is_set()
    f.set(False)   # closing twice is fine
