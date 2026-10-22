"""What the controller keeps between cycles: the active server and the blacklist (logic)."""

from exit_vpn.state import TunnelServer, Blacklist


def test_a_blacklist_entry_lasts_until_it_expires():
    b = Blacklist()
    b.add("bad=", 100.0)
    assert b.contains("bad=", 50) and not b.contains("bad=", 150)
    assert b.active(50) == {"bad="} and b.active(150) == set()
    b.prune(150)
    assert b.to_dict() == {}


def test_the_active_server_round_trips_and_knows_its_ip():
    a = TunnelServer("US-NY#1", "pk=", "1.2.3.4:51820", "United States", "New York", 1.0, ("port_forward",))
    assert a.ip == "1.2.3.4"
    assert TunnelServer.from_dict(a.to_dict()) == a
    assert TunnelServer.from_dict({"name": "X", "pubkey": "k=", "endpoint": "1.1.1.1:51820"}).profile == ()
