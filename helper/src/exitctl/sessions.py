"""Where provider sessions are kept: the macOS Keychain (via `keyring`), never a file (D18).
A session grants the whole provider account, so it never leaves this Mac. `MemoryStore` is
for tests."""

from __future__ import annotations

import json
from typing import Protocol

SERVICE = "exitctl"


class SessionStore(Protocol):
    def load(self, name: str) -> dict | None: ...
    def save(self, name: str, data: dict) -> None: ...
    def delete(self, name: str) -> None: ...


class KeychainStore:
    def load(self, name: str) -> dict | None:
        import keyring
        raw = keyring.get_password(SERVICE, name)
        return json.loads(raw) if raw else None

    def save(self, name: str, data: dict) -> None:
        import keyring
        keyring.set_password(SERVICE, name, json.dumps(data))

    def delete(self, name: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError
        try:
            keyring.delete_password(SERVICE, name)
        except PasswordDeleteError:
            pass


class MemoryStore:
    def __init__(self):
        self.data: dict[str, dict] = {}

    def load(self, name: str) -> dict | None:
        return self.data.get(name)

    def save(self, name: str, data: dict) -> None:
        self.data[name] = data

    def delete(self, name: str) -> None:
        self.data.pop(name, None)
