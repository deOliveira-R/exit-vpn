"""Provider adapters (D20). Add a provider by adding a module and registering it here."""

from __future__ import annotations

from .base import Provider


def get(name: str) -> Provider:
    if name == "proton":
        from .proton import ProtonProvider
        return ProtonProvider()
    if name == "manual":
        from .manual import ManualProvider
        return ManualProvider()
    raise KeyError(name)


NAMES = ("proton", "manual")
