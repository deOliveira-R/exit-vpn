"""The slot-b WireGuard config (boundary): what the prober reads from it, and which file it reads."""

import pytest

from prober.keyfile import KeyFile, KeyFileError

CONF = ("[Interface]\n# NetShield = 0\nPrivateKey = abc=\n"
        "Address = 10.2.0.2/32, 2a07:b944::2:2/128\nDNS = 10.2.0.1\n")


def test_the_private_key_and_first_ipv4_address_are_read():
    k = KeyFile.parse(CONF)
    assert (k.private_key, k.address) == ("abc=", "10.2.0.2/32")


def test_an_address_without_a_prefix_gets_slash_32():
    assert KeyFile.parse(CONF.replace("10.2.0.2/32", "10.2.0.2")).address == "10.2.0.2/32"


@pytest.mark.parametrize("text", ["[Interface]\nAddress = 10.2.0.2/32\n",
                                  "[Interface]\nPrivateKey = abc=\nAddress = 2a07:b944::2:2/128\n"])
def test_a_config_without_a_private_key_or_ipv4_address_is_refused(text):
    with pytest.raises(KeyFileError):
        KeyFile.parse(text)


def test_the_key_never_appears_in_its_repr():
    assert "abc" not in repr(KeyFile.parse(CONF))


def test_an_uploaded_slot_b_key_wins_over_the_bootstrap_config(tmp_path, monkeypatch):
    boot = tmp_path / "b.conf"
    monkeypatch.setenv("PROBE_VPN_CONF", str(boot))
    monkeypatch.setenv("KEY_DIR_B", str(tmp_path / "keys"))
    assert KeyFile.path() == boot
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys/wg.conf").write_text("x")
    assert KeyFile.path() == tmp_path / "keys/wg.conf"
