import pytest

from prober.tunnel import TunnelError, conf_fields, key_conf


def test_conf_fields():
    text = ("[Interface]\n# NetShield = 0\nPrivateKey = abc=\n"
            "Address = 10.2.0.2/32, 2a07:b944::2:2/128\nDNS = 10.2.0.1\n")
    assert conf_fields(text) == ("abc=", "10.2.0.2/32")
    assert conf_fields(text.replace("10.2.0.2/32", "10.2.0.2"))[1] == "10.2.0.2/32"
    with pytest.raises(TunnelError):
        conf_fields("[Interface]\nAddress = 10.2.0.2/32\n")


def test_key_conf_prefers_slot(tmp_path, monkeypatch):
    boot = tmp_path / "b.conf"
    monkeypatch.setenv("PROBE_VPN_CONF", str(boot))
    monkeypatch.setenv("KEY_DIR_B", str(tmp_path / "keys"))
    assert key_conf() == boot
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys/wg.conf").write_text("x")
    assert key_conf() == tmp_path / "keys/wg.conf"
