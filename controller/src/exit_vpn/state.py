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


FEATURES = ("port_forward", "stream", "ipv6")   # what a server profile can keep (PLAN Phase 5)
HARD_FEATURES = ("port_forward",)              # P2P: never fail over to a server without it


@dataclass
class Prefs:
    """`regions`: pinned regions in priority order, each {"country": .., "city": ..|None}."""
    regions: list[dict]
    auto_failover: bool = True
    secure_core: bool = False   # on: only Secure Core servers; off: none (works with any key)
    allow_free: bool = False    # free-tier servers are crowded
    require: list[str] = field(default_factory=list)  # features every candidate must have
    favorites: list[str] = field(default_factory=list)  # logical server names, tried first

    @property
    def countries(self) -> list[str]:
        """The regions' countries, in order, without repeats."""
        out: list[str] = []
        for r in self.regions:
            if r["country"].lower() not in [c.lower() for c in out]:
                out.append(r["country"])
        return out

    def view(self) -> dict:
        return {**vars(self), "countries": self.countries}

    @classmethod
    def from_dict(cls, d: dict, default_countries: list[str]) -> Prefs:
        d = dict(d)
        countries = d.pop("countries", None) or default_countries
        if "regions" not in d:   # state from before pinned regions: countries become regions
            d["regions"] = regions_from_countries(countries)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def regions_from_countries(countries: list[str]) -> list[dict]:
    return [{"country": c, "city": None} for c in countries]


@dataclass
class Current:
    name: str
    pubkey: str
    endpoint: str
    country: str = ""
    city: str = ""
    since: float = 0.0
    profile: list[str] = field(default_factory=list)  # features failover keeps (manual pick)


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
        prefs = Prefs.from_dict(d.get("prefs", {}), default_countries)
        cur = Current(**d["current"]) if d.get("current") else None
        return cls(prefs, cur, d.get("blacklist", {}), d.get("events", []))


class StateStore:
    def __init__(self, path: Path, peer_env: Path, default_countries: list[str]):
        self.path, self.peer_env, self.default_countries = path, peer_env, default_countries

    def load(self) -> State:
        try:
            return State.from_json(self.path.read_text(), self.default_countries)
        except FileNotFoundError:
            return State(Prefs(regions_from_countries(self.default_countries)))

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
