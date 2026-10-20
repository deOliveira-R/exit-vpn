import struct

from exit_vpn.auth import decide
from exit_vpn.probes import dns_answer_ok, dns_query, parse_trace
from exit_vpn.state import Current, Prefs, State, StateStore
from exit_vpn.wg import parse_dump
from exit_vpn.wgconf import parse

OWNERS = ["owner@github"]


def test_auth_owner_device():
    ok, who = decide({"Node": {"ComputedName": "mac", "Tags": None},
                      "UserProfile": {"LoginName": "owner@github"}}, OWNERS)
    assert ok and "mac" in who


def test_auth_rejects_tagged_and_strangers():
    assert not decide({"Node": {"Tags": ["tag:server"]},
                       "UserProfile": {"LoginName": "tagged-devices"}}, OWNERS)[0]
    assert not decide({"Node": {"Tags": []}, "UserProfile": {"LoginName": "eve@github"}}, OWNERS)[0]
    assert not decide({}, OWNERS)[0]


def test_trace():
    assert parse_trace("fl=1\nip=1.2.3.4\nloc=US\n")["ip"] == "1.2.3.4"


def test_dns_query_and_answer():
    q = dns_query("www.example.com", 0x1234)
    assert q[:2] == b"\x12\x34" and q.endswith(b"\x00\x01\x00\x01")
    resp = struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0)
    assert dns_answer_ok(resp, 0x1234)
    assert not dns_answer_ok(resp, 0x9999)                                       # wrong id
    assert not dns_answer_ok(struct.pack("!HHHHHH", 0x1234, 0x8183, 1, 0, 0, 0), 0x1234)  # NXDOMAIN


def test_wg_dump():
    dump = ("PRIV\tPUB\t51820\toff\n"
            "peerkey=\t(none)\t1.2.3.4:51820\t0.0.0.0/0,::/0\t1700000000\t100\t200\t25\n")
    (p,) = parse_dump(dump)
    assert p.pubkey == "peerkey=" and p.endpoint == "1.2.3.4:51820" and p.latest_handshake == 1700000000


def test_wgconf_never_reads_private_key(tmp_path):
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


def test_state_roundtrip_and_peer_env(tmp_path):
    store = StateStore(tmp_path / "state.json", tmp_path / "peer.env", ["United States"])
    st = store.load()
    assert st.prefs.countries == ["United States"] and st.current is None
    st.current = Current("US-NY#1", "pk=", "1.2.3.4:51820", "United States", "New York", 1.0)
    st.blacklist_add("bad=", 100.0)
    for i in range(60):
        st.event("x", float(i), i=i)
    store.save(st)
    again = store.load()
    assert again.current == st.current and len(again.events) == 50 and again.events[-1]["i"] == 59
    assert again.blacklisted("bad=", 50) and not again.blacklisted("bad=", 150)
    assert again.prefs.regions == [{"country": "United States", "city": None}]
    env = (tmp_path / "peer.env").read_text()
    assert "PEER_PUBKEY='pk='" in env and "PEER_ENDPOINT='1.2.3.4:51820'" in env
    assert oct((tmp_path / "state.json").stat().st_mode & 0o777) == "0o600"


def test_old_state_countries_migrate_to_regions(tmp_path):
    import json
    from exit_vpn.state import Prefs
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"prefs": {"countries": ["Switzerland", "Germany"], "secure_core": False,
                                       "gone_field": 1},
                             "current": {"name": "X", "pubkey": "k=", "endpoint": "1.1.1.1:51820"}}))
    st = StateStore(f, tmp_path / "peer.env", ["United States"]).load()
    assert st.prefs.regions == [{"country": "Switzerland", "city": None},
                                {"country": "Germany", "city": None}]
    assert st.prefs.countries == ["Switzerland", "Germany"] and st.current.profile == []
    assert Prefs([{"country": "US", "city": "A"}, {"country": "us", "city": None}]).countries == ["US"]
