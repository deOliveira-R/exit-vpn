"""Key slots (PLAN Phase 3b): `a` = the exit tunnel, `b` = the prober's tunnel.

An uploaded key lives in its slot directory (a named volume) as `wg.conf` (0600) plus
`meta.json` (public facts from the provider: certificate id, expiry, options). While a
slot is empty, the bootstrap config from `secrets/` is used. Every accepted key is also
mirrored to that bootstrap file, so the fallback is never a dead key (a rotation deletes
the old key's certificate). The previous uploaded config is kept as `wg.conf.prev`. Private keys are only ever read
from these files and passed on via stdin; they never appear in responses or logs.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .state import atomic_write
from .wgconf import WgConf, parse_text

log = logging.getLogger("exit_vpn")
SLOTS = ("a", "b")
CONF, PREV, META = "wg.conf", "wg.conf.prev", "meta.json"
MAX_CONF_BYTES = 8192
_PRIVATE = re.compile(r"^\s*PrivateKey\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE)


class KeyRejected(ValueError):
    """The uploaded config is malformed or does not fit the running tunnel."""


@dataclass(frozen=True)
class KeyFile:
    text: str            # the whole config, secret
    conf: WgConf         # the non-secret parts
    private_key: str

    def __repr__(self) -> str:  # never print the key, even by accident
        return f"KeyFile(peer={self.conf.peer_name or self.conf.peer_endpoint})"


def private_key(text: str) -> str:
    m = _PRIVATE.search(text)
    if not m:
        raise KeyRejected("no [Interface] PrivateKey")
    try:
        raw = base64.b64decode(m.group(1), validate=True)
    except (binascii.Error, ValueError):
        raise KeyRejected("PrivateKey is not base64") from None
    if len(raw) != 32:
        raise KeyRejected("PrivateKey is not 32 bytes")
    return m.group(1)


def load_text(text: str, like: WgConf | None = None) -> KeyFile:
    """Validate an uploaded config. `like`: the running tunnel's config; Address and DNS
    must match it, because the kill switch and policy routing are built on them."""
    if len(text.encode()) > MAX_CONF_BYTES:
        raise KeyRejected("config too large")
    try:
        conf = parse_text(text, "uploaded config")
    except ValueError as e:
        raise KeyRejected(str(e)) from None
    key = private_key(text)
    if like and (conf.address, conf.dns) != (like.address, like.dns):
        raise KeyRejected(f"Address/DNS {conf.address}/{conf.dns} differ from the running "
                          f"tunnel's {like.address}/{like.dns}")
    return KeyFile(text, conf, key)


class KeyStore:
    def __init__(self, dirs: dict[str, Path], bootstrap: dict[str, Path | None]):
        self.dirs, self.bootstrap = dirs, bootstrap

    def path(self, slot: str) -> Path | None:
        """The config in effect for the slot: uploaded, else bootstrap."""
        up = self.dirs[slot] / CONF
        if up.exists():
            return up
        b = self.bootstrap.get(slot)
        return b if b and b.exists() else None

    def source(self, slot: str) -> str:
        p = self.path(slot)
        return "none" if p is None else ("uploaded" if p.parent == self.dirs[slot] else "bootstrap")

    def current(self, slot: str) -> KeyFile | None:
        p = self.path(slot)
        return load_text(p.read_text()) if p else None

    def meta(self, slot: str) -> dict:
        """Recorded facts about the slot's key. May describe a bootstrap key too (after a
        renew); callers check `wg_public_key` against the key in effect."""
        try:
            return json.loads((self.dirs[slot] / META).read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def commit(self, slot: str, key: KeyFile, meta: dict) -> None:
        """Make `key` the slot's config; the previous uploaded one becomes `.prev`."""
        d = self.dirs[slot]
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        if (d / CONF).exists():
            os.replace(d / CONF, d / PREV)
        atomic_write(d / CONF, key.text if key.text.endswith("\n") else key.text + "\n")
        self.write_meta(slot, meta)
        self._mirror(slot, key.text)

    def _mirror(self, slot: str, text: str) -> None:
        b = self.bootstrap.get(slot)
        if not b:
            return
        try:
            atomic_write(b, text if text.endswith("\n") else text + "\n")
        except OSError as e:   # e.g. secrets/ mounted read-only: the fallback goes stale
            log.warning("could not mirror slot %s key to %s: %s", slot, b, e)

    def write_meta(self, slot: str, meta: dict) -> None:
        self.dirs[slot].mkdir(parents=True, exist_ok=True)
        atomic_write(self.dirs[slot] / META, json.dumps(meta, indent=1, sort_keys=True))

    def restore(self, slot: str, previous: KeyFile | None, previous_meta: dict,
                was_uploaded: bool) -> None:
        """Undo a `commit` (used for slot b, which is committed before it is tested)."""
        d = self.dirs[slot]
        if previous:
            self._mirror(slot, previous.text)
        if was_uploaded and previous:
            atomic_write(d / CONF, previous.text)
            self.write_meta(slot, previous_meta)
        else:
            (d / CONF).unlink(missing_ok=True)
            if previous_meta:
                self.write_meta(slot, previous_meta)
            else:
                (d / META).unlink(missing_ok=True)
