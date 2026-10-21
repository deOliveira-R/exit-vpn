"""The files the exit's shell scripts share with the controller: peer.env (init-wg sources
it), the exit key choice and tunnel addresses in common.sh, the IPv6 gate file (boundary:
real sh)."""

import subprocess
from pathlib import Path

from exit_vpn.config import Settings
from exit_vpn.keys import KeyStore
from exit_vpn.persistence import StateStore
from exit_vpn.prefs import Prefs
from exit_vpn.state import ActiveServer, State
from exit_vpn.wgconf import parse

COMMON = Path(__file__).resolve().parents[2] / "exit/rootfs/usr/local/lib/exit-vpn/common.sh"


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


def test_the_shell_scripts_pick_the_same_exit_key_as_the_controller(tmp_path):
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


def _shell_conf(conf: Path, **env) -> str:
    """What common.sh's load_conf makes of a config: the IPv4 and IPv6 tunnel addresses."""
    return subprocess.run(["sh", "-c", '. "$1"; load_conf; printf "%s|%s" "$VPN_SRC" "$VPN_SRC6"',
                           "sh", str(COMMON)],
                          env={"EXIT_VPN_CONF": str(conf), "KEY_DIR_A": str(conf.parent / "none"),
                               "PATH": "/usr/bin:/bin", **env},
                          capture_output=True, text=True, check=True).stdout


def test_the_shell_scripts_read_the_same_tunnel_addresses_as_the_controller(tmp_path):
    """init-wg and init-killswitch (common.sh) and wgconf agree on both addresses, and on a
    config without IPv6."""
    f = tmp_path / "a.conf"
    body = ("[Interface]\nPrivateKey = K=\nAddress = {}\nDNS = 10.2.0.1, 2a07:b944::2:1\n\n"
            "[Peer]\nPublicKey = pk=\nEndpoint = 1.2.3.4:51820\n")
    for addr in ("10.2.0.2/32, 2a07:b944::2:2/128", "2a07:b944::2:2/128,10.2.0.2/32", "10.2.0.2/32"):
        f.write_text(body.format(addr))
        c = parse(f)
        assert _shell_conf(f) == f"{c.address}|{c.address6}", addr


def test_ipv6_off_leaves_the_tunnel_without_an_ipv6_address(tmp_path):
    f = tmp_path / "a.conf"
    f.write_text("[Interface]\nPrivateKey = K=\nAddress = 10.2.0.2/32, 2a07:b944::2:2/128\n"
                 "DNS = 10.2.0.1\n\n[Peer]\nPublicKey = pk=\nEndpoint = 1.2.3.4:51820\n")
    assert _shell_conf(f, EXIT_IPV6="off") == "10.2.0.2|"
    assert Settings.from_env({"EXIT_VPN_CONF": str(f), "EXIT_IPV6": "off"}).ipv6 is False
    assert Settings.from_env({"EXIT_VPN_CONF": str(f)}).ipv6 is True


def test_the_ipv6_gate_file_is_where_route_keeper_looks(tmp_path):
    out = subprocess.run(["sh", "-c", '. "$1"; printf %s "$IPV6_GATE"', "sh", str(COMMON)],
                         env={"EXIT_VPN_CONF": "/dev/null", "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True, check=True).stdout
    assert out == str(Settings(vpn_conf=Path("x")).ipv6_gate_file)
