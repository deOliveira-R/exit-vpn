import json
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
