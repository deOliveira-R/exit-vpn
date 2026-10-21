"""Server rows for the exit node from Proton's logicals (logic)."""

from exitctl.providers.proton.catalog import catalog_rows

LOGICALS = [
    {"Name": "CH-US#1", "ExitCountry": "US", "City": "New York", "Tier": 2, "Features": 1 | 8 | 16,
     "Status": 1, "Load": 33, "Servers": [
         {"EntryIP": "185.159.157.9", "Domain": "node-ch-01.protonvpn.net", "X25519PublicKey": "k1", "Status": 1},
         {"EntryIP": "185.159.157.10", "Domain": "node-ch-02.protonvpn.net", "X25519PublicKey": "k2", "Status": 0},
         {"EntryIP": "185.159.157.9", "Domain": "dup", "X25519PublicKey": "k1", "Status": 1}]},
    {"Name": "US-FREE#1", "ExitCountry": "US", "Tier": 0, "Features": 0, "Status": 0, "Load": 90,
     "Servers": [{"EntryIP": "1.2.3.4", "Domain": "d", "X25519PublicKey": "k3", "Status": 1}]}]


def test_one_row_per_physical_server_enabled_only_when_it_and_its_logical_are_up():
    assert [(r["hostname"], r["enabled"]) for r in catalog_rows(LOGICALS)] == [
        ("node-ch-01.protonvpn.net", True), ("node-ch-02.protonvpn.net", False), ("d", False)]


def test_feature_bits_and_the_free_tier_become_flags():
    rows = catalog_rows(LOGICALS)
    assert rows[0]["secure_core"] and rows[0]["stream"] and rows[0]["ipv6"] and not rows[0]["free"]
    assert rows[2]["free"] and not rows[2]["ipv6"]


def test_rows_carry_the_logicals_load():
    rows = catalog_rows(LOGICALS)
    assert rows[0]["load"] == 33 and rows[2]["load"] == 90
