"""Provider sessions in the macOS Keychain (via `keyring`). Never written elsewhere."""

from __future__ import annotations

import json

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "exitctl"


def load(name: str) -> dict | None:
    raw = keyring.get_password(SERVICE, name)
    return json.loads(raw) if raw else None


def save(name: str, data: dict) -> None:
    keyring.set_password(SERVICE, name, json.dumps(data))


def delete(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass
