"""Building the controller (the composition root): every object, its collaborators, and
how they connect. `__main__` builds the real node; the tests build one with fakes.

    ExitService ── Tunnel ── WireGuard (wg0), HealthProbe (HealthTracker)
        │  └── selection (pure), Catalog, Prefs, State → StateStore, CatalogCache
        ├── EventBus → EventStore, /v1/events subscribers
    KeySlots ── ExitService.replace_exit_key, KeyStore, ProberClient
    FindService ── ExitService.find_candidates / switch_to, ProberClient
    CatalogSource ── CatalogCache (startup, push, revert)
    api ── all of the above, through their public methods
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .catalog import Catalog
from .catalog_source import CatalogSource
from .config import Settings
from .events import EventBus
from .finder import FindService
from .health import HealthTracker
from .keys import KeyStore
from .persistence import CatalogCache, EventStore, StateStore
from .probes import HealthProbe
from .prober_client import ProberClient
from .service import ExitService
from .slots import KeySlots
from .tunnel import Tunnel
from .wg import WireGuard, public_key
from .wgconf import WgConf


@dataclass
class Node:
    settings: Settings
    service: ExitService
    events: EventBus
    slots: KeySlots
    finder: FindService | None
    catalogs: CatalogSource


def build(settings: Settings, *, exit_conf: WgConf, wg: WireGuard, probe: HealthProbe,
          catalog: Catalog, keys: KeyStore, prober: ProberClient | None = None,
          forbid_ips: Callable[[], list[str]] = lambda: [],
          clock: Callable[[], float] = time.time, rng: random.Random | None = None,
          pubkey: Callable[[str], Awaitable[str]] = public_key) -> Node:
    s = settings
    store = StateStore(s.state_file, s.peer_env_file, s.countries)
    events = EventBus(EventStore(s.events_file), clock, initial=store.load()[1])
    cache = CatalogCache(s.catalog_file)
    tunnel = Tunnel(wg, probe, HealthTracker(s.strikes, s.warmup), clock)
    service = ExitService(s, tunnel, catalog, store, events, exit_conf, catalog_cache=cache,
                          clock=clock, rng=rng)

    def code_for(country: str) -> str:
        return service.catalog.code_for_country(country)

    slots = KeySlots(service, keys, events, prober, code_for, forbid_ips, pubkey,
                     s.key_expiry_warn_days)
    service.warning_sources.append(slots.warnings)
    finder = FindService(service, prober, events, code_for, forbid_ips) if prober else None
    return Node(s, service, events, slots, finder, CatalogSource(s, cache))
