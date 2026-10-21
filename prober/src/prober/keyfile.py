"""The prober's key (slot b): the uploaded config if there is one, else the
bootstrap config. entrypoint.sh makes the same choice at start; `POST /key/reload` re-reads
it here."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


class KeyFileError(ValueError):
    pass


@dataclass(frozen=True)
class KeyFile:
    private_key: str
    address: str          # first IPv4 Address, with prefix

    def __repr__(self) -> str:   # never print the key
        return f"KeyFile(address={self.address})"

    @staticmethod
    def path(env=os.environ) -> Path:
        up = Path(env.get("KEY_DIR_B", "/keys/b")) / "wg.conf"
        return up if up.exists() else Path(env.get("PROBE_VPN_CONF", "/secrets/proton_b.conf"))

    @classmethod
    def parse(cls, text: str) -> KeyFile:
        key = re.search(r"^\s*PrivateKey\s*=\s*(\S+)", text, re.M | re.I)
        addr = re.search(r"^\s*Address\s*=\s*([^#\n]+)", text, re.M | re.I)
        v4 = next((a.strip() for a in (addr.group(1).split(",") if addr else [])
                   if re.fullmatch(r"\d+(\.\d+){3}(/\d+)?", a.strip())), "")
        if not key or not v4:
            raise KeyFileError("config has no PrivateKey or IPv4 Address")
        return cls(key.group(1), v4 if "/" in v4 else v4 + "/32")

    @classmethod
    def current(cls, env=os.environ) -> KeyFile:
        return cls.parse(cls.path(env).read_text())
