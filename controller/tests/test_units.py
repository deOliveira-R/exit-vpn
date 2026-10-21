import struct
import subprocess
from pathlib import Path

from exit_vpn.auth import decide
from exit_vpn.events import EventBus
from exit_vpn.keys import KeyStore
from exit_vpn.persistence import EventStore, StateStore
from exit_vpn.prefs import Prefs
from exit_vpn.probes import dns_answer_ok, dns_query, parse_trace
from exit_vpn.servers import Region
from exit_vpn.state import ActiveServer, State
from exit_vpn.wg import parse_dump
from exit_vpn.wgconf import parse

from harness import prefs_for

COMMON = Path(__file__).resolve().parents[2] / "exit/rootfs/usr/local/lib/exit-vpn/common.sh"

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
    st, old_events = store.load()
    assert st.prefs.countries == ["United States"] and st.active is None and old_events == []
    st.active = ActiveServer("US-NY#1", "pk=", "1.2.3.4:51820", "United States", "New York", 1.0)
    st.blacklist.add("bad=", 100.0)
    store.save(st)
    again, _ = store.load()
    assert again.active == st.active
    assert again.blacklist.contains("bad=", 50) and not again.blacklist.contains("bad=", 150)
    assert again.prefs.regions == (Region("United States"),)
    env = (tmp_path / "peer.env").read_text()
    assert "PEER_PUBKEY='pk='" in env and "PEER_ENDPOINT='1.2.3.4:51820'" in env
    assert oct((tmp_path / "state.json").stat().st_mode & 0o777) == "0o600"


def test_peer_env_is_what_init_wg_sources(tmp_path):
    """The shell contract: init-wg sources peer.env (POSIX sh); odd names stay intact."""
    store = StateStore(tmp_path / "state.json", tmp_path / "peer.env", ["United States"])
    name = "it's US-NY#1 $(boom)"
    store.save(State(Prefs.default(["United States"]),
                     ActiveServer(name, "pk+/=", "1.2.3.4:51820")))
    out = subprocess.run(["sh", "-c", '. "$1"; printf "%s|%s|%s" "$PEER_NAME" "$PEER_PUBKEY" '
                          '"$PEER_ENDPOINT"', "sh", str(tmp_path / "peer.env")],
                         capture_output=True, text=True, check=True).stdout
    assert out == f"{name}|pk+/=|1.2.3.4:51820"


def test_init_scripts_pick_the_same_exit_key_as_the_controller(tmp_path):
    """common.sh and KeyStore.path("a") agree: an uploaded key wins over the bootstrap."""
    boot, keydir = tmp_path / "boot.conf", tmp_path / "keys-a"
    boot.write_text("bootstrap")
    keys = KeyStore({"a": keydir, "b": tmp_path / "keys-b"}, {"a": boot, "b": None})

    def shell_choice():
        return subprocess.run(["sh", "-c", '. "$1"; printf %s "$EXIT_VPN_CONF"', "sh", str(COMMON)],
                              env={"EXIT_VPN_CONF": str(boot), "KEY_DIR_A": str(keydir),
                                   "PATH": "/usr/bin:/bin"},
                              capture_output=True, text=True, check=True).stdout
    assert shell_choice() == str(keys.path("a")) == str(boot)
    keydir.mkdir()
    (keydir / "wg.conf").write_text("uploaded")
    assert shell_choice() == str(keys.path("a")) == str(keydir / "wg.conf")


def test_event_log_is_kept_and_bounded(tmp_path):
    store = EventStore(tmp_path / "events.json")
    bus = EventBus(store, clock=lambda: 5.0, initial=[{"ts": 1.0, "kind": "old"}])
    for i in range(60):
        bus.publish("x", i=i)
    again = EventBus(store)
    assert len(again.recent()) == 50 and again.recent()[-1]["i"] == 59
    assert EventBus(EventStore(tmp_path / "none.json"), initial=[{"kind": "old"}]).recent() == [{"kind": "old"}]


def test_old_state_migrates(tmp_path):
    """countries → regions; events kept in state.json are handed to the event log."""
    import json
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"prefs": {"countries": ["Switzerland", "Germany"], "secure_core": False,
                                       "gone_field": 1},
                             "current": {"name": "X", "pubkey": "k=", "endpoint": "1.1.1.1:51820"},
                             "events": [{"ts": 1.0, "kind": "started", "server": "X"}]}))
    st, events = StateStore(f, tmp_path / "peer.env", ["United States"]).load()
    assert st.prefs.regions == (Region("Switzerland"), Region("Germany"))
    assert st.prefs.countries == ["Switzerland", "Germany"] and st.active.profile == ()
    assert events == [{"ts": 1.0, "kind": "started", "server": "X"}]
    assert prefs_for([], regions=(Region("US", "A"), Region("us"))).countries == ["US"]
