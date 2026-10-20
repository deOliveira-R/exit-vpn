"""For providers without an API: import a .conf downloaded from their dashboard."""

from __future__ import annotations

from .base import IMPORT, Provider


class ManualProvider(Provider):
    name = "manual"
    title = "Manual (.conf import)"
    capabilities = frozenset({IMPORT})
