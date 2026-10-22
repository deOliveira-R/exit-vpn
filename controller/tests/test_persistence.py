"""What the controller saves: state and peer.env, the event log, the catalog cache, and
old state files (boundary: real files)."""

import json

from exit_vpn.events import EventBus
from exit_vpn.persistence import CatalogCache, EventStore, StateStore
from exit_vpn.servers import Region
from exit_vpn.state import TunnelServer


def test_state_is_saved_privately_and_read_back(tmp_path):
    store = StateStore(tmp_path / "state.json", tmp_path / "peer.env", ["United States"])
    st, old_events = store.load()
    assert st.prefs.countries == ["United States"] and st.active is None and old_events == []
    st.active = TunnelServer("US-NY#1", "pk=", "1.2.3.4:51820", "United States", "New York", 1.0)
    st.blacklist.add("bad=", 100.0)
    store.save(st)
    again, _ = store.load()
    assert again.active == st.active
    assert again.blacklist.to_dict() == {"bad=": 100.0}
    assert again.prefs.regions == (Region("United States"),)
    env = (tmp_path / "peer.env").read_text()
    assert "PEER_PUBKEY='pk='" in env and "PEER_ENDPOINT='1.2.3.4:51820'" in env
    assert oct((tmp_path / "state.json").stat().st_mode & 0o777) == "0o600"


def test_the_event_log_is_kept_and_bounded(tmp_path):
    store = EventStore(tmp_path / "events.json")
    bus = EventBus(store, clock=lambda: 5.0, initial=[{"ts": 1.0, "kind": "old"}])
    for i in range(60):
        bus.publish("x", i=i)
    again = EventBus(store)
    assert len(again.recent()) == 50 and again.recent()[-1]["i"] == 59
    assert EventBus(EventStore(tmp_path / "none.json"), initial=[{"kind": "old"}]).recent() == [{"kind": "old"}]


def test_old_state_is_migrated(tmp_path):
    """countries → regions; events kept in state.json are handed to the event log."""
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"prefs": {"countries": ["Switzerland", "Germany"], "secure_core": False,
                                       "gone_field": 1},
                             "current": {"name": "X", "pubkey": "k=", "endpoint": "1.1.1.1:51820"},
                             "events": [{"ts": 1.0, "kind": "started", "server": "X"}]}))
    st, events = StateStore(f, tmp_path / "peer.env", ["United States"]).load()
    assert st.prefs.regions == (Region("Switzerland"), Region("Germany"))
    assert st.prefs.countries == ["Switzerland", "Germany"] and st.active.profile == ()
    assert events == [{"ts": 1.0, "kind": "started", "server": "X"}]


def test_the_catalog_cache_is_saved_privately_and_read_back(catalog, tmp_path):
    p = tmp_path / "catalog.json"
    CatalogCache(p).save(catalog)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    again = CatalogCache(p).load()
    assert again.servers == catalog.servers and again.source == "fixture"
