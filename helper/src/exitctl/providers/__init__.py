"""Provider adapters (D20). Add a provider by adding a module and registering it here."""

from __future__ import annotations

from ..model import ExitctlError
from .base import Provider

NAMES = ("proton", "manual")


def get(name: str) -> Provider:
    if name == "proton":
        from .proton import ProtonProvider
        return ProtonProvider()
    if name == "manual":
        from .manual import ManualProvider
        return ManualProvider()
    raise ExitctlError(f"unknown provider {name!r} (known: {', '.join(NAMES)})")
