import json
import sys
from pathlib import Path

import pytest

from exit_vpn.catalog import Catalog, parse_gluetun

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_catalog() -> dict:
    return json.loads((FIXTURES / "protonvpn-excerpt.json").read_text())


@pytest.fixture
def catalog(raw_catalog) -> Catalog:
    return Catalog(parse_gluetun(raw_catalog, ".protonvpn.net"), "fixture", 0.0)


# Contracts (PLAN R1): every event any test emits must match contracts/controller/events.json
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "contracts"))
import shape  # noqa: E402

from exit_vpn.events import EventBus  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "exit_vpn"


def emitted_kinds() -> set[str]:
    """Event kinds the controller's code can emit (every `publish("kind"` call)."""
    import re
    return {m for f in SRC.glob("*.py") for m in re.findall(r'\bpublish\(\s*"([a-z_]+)"', f.read_text())}


KINDS = emitted_kinds()


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
    from harness import make
    return make(catalog, tmp_path, monkeypatch)
