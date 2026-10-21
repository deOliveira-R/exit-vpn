"""Fixtures: the catalog fixture, the rig (harness.py), and the event contract, checked for
every event any test publishes (contracts/controller/events.json)."""

import json
from pathlib import Path

import pytest

from exit_vpn.catalog import Catalog, parse_gluetun
from exit_vpn.events import EventBus

from harness import KINDS, make, shape

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_catalog() -> dict:
    return json.loads((FIXTURES / "protonvpn-excerpt.json").read_text())


@pytest.fixture
def catalog(raw_catalog) -> Catalog:
    return Catalog(parse_gluetun(raw_catalog, ".protonvpn.net"), "fixture", 0.0)


@pytest.fixture(autouse=True)
def event_contract(monkeypatch):
    original = EventBus.publish

    def checked(self, kind, **data):
        ev = original(self, kind, **data)
        if kind in KINDS:   # tests also publish made-up kinds
            shape.check_event("controller/events", json.loads(json.dumps(ev)))
        return ev
    monkeypatch.setattr(EventBus, "publish", checked)


@pytest.fixture
def rig(catalog, tmp_path, monkeypatch):
    """A controller node with a fake tunnel (harness.py)."""
    return make(catalog, tmp_path, monkeypatch)
