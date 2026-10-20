"""Persisted controller state: current server, preferences, blacklist, recent events."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_EVENTS = 50


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


@dataclass
class Prefs:
    countries: list[str]
    auto_failover: bool = True
    secure_core: bool = False   # works with any key (PLAN Phase 0); off by default
    allow_free: bool = False    # free-tier servers are crowded


@dataclass
class Current:
    name: str
    pubkey: str
    endpoint: str
    country: str = ""
    city: str = ""
    since: float = 0.0


@dataclass
class State:
    prefs: Prefs
    current: Current | None = None
    blacklist: dict[str, float] = field(default_factory=dict)  # pubkey -> expiry
    events: list[dict] = field(default_factory=list)

    def blacklisted(self, pubkey: str, now: float) -> bool:
        return self.blacklist.get(pubkey, 0.0) > now

    def blacklist_add(self, pubkey: str, until: float) -> None:
        self.blacklist[pubkey] = until

    def prune(self, now: float) -> None:
        self.blacklist = {k: v for k, v in self.blacklist.items() if v > now}

    def event(self, kind: str, now: float | None = None, **data) -> dict:
        ev = {"ts": now if now is not None else time.time(), "kind": kind, **data}
        self.events.append(ev)
        del self.events[:-MAX_EVENTS]
        return ev

    # Persistence
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def from_json(cls, text: str, default_countries: list[str]) -> State:
        d = json.loads(text)
        prefs = Prefs(**{"countries": default_countries, **d.get("prefs", {})})
        cur = Current(**d["current"]) if d.get("current") else None
        return cls(prefs, cur, d.get("blacklist", {}), d.get("events", []))


class StateStore:
    def __init__(self, path: Path, peer_env: Path, default_countries: list[str]):
        self.path, self.peer_env, self.default_countries = path, peer_env, default_countries

    def load(self) -> State:
        try:
            return State.from_json(self.path.read_text(), self.default_countries)
        except FileNotFoundError:
            return State(Prefs(list(self.default_countries)))

    def save(self, state: State) -> None:
        atomic_write(self.path, state.to_json())
        if state.current:  # read by init-wg on the next start (last-good peer)
            c = state.current
            atomic_write(self.peer_env, "".join(
                f"{k}={_shell_quote(v)}\n"
                for k, v in (("PEER_NAME", c.name), ("PEER_PUBKEY", c.pubkey),
                             ("PEER_ENDPOINT", c.endpoint))))


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
