"""The files the exit's shell scripts share with the controller: peer.env (init-wg sources
it) and the exit key choice in common.sh (boundary: real sh)."""

import subprocess
from pathlib import Path

from exit_vpn.keys import KeyStore
from exit_vpn.persistence import StateStore
from exit_vpn.prefs import Prefs
from exit_vpn.state import ActiveServer, State

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
