"""Building the controller (the composition root): every object, its collaborators, and
how they connect. `__main__` builds the real node; the tests build one with fakes.

    ExitService ── Tunnel (active) ── WireGuard (wg0), HealthProbe (HealthTracker, IPv6Gate → GateFile,
        │  │                           QualityMeter → QualityTracker)
        │  ├── Tunnel (standby) ── WireGuard (wg1), HealthProbe, HealthTracker
        │  ├── ActiveRoute (the handover: table 100's default)
        │  └── selection (pure), Catalog, Prefs, State → StateStore, CatalogCache
        ├── QualityLog (a summary per tunnel each minute)
        ├── EventBus → EventStore, /v1/events subscribers
    KeySlots ── ExitService.replace_exit_key, KeyStore, ProberClient
    FindService ── ExitService.find_candidates / switch_to, ProberClient ─┐ prober_lock
    SurveyService ── ExitService.survey_candidates, ProberClient ┘
        └── QualityLog (survey.jsonl), SurveyScheduleStore (the schedule, run by itself)
    CatalogSource ── CatalogCache (startup, push, revert)
    api ── all of the above, through their public methods
"""

from __future__ import annotations

import asyncio
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
from .persistence import CatalogCache, EventStore, QualityLog, StateStore, SurveyScheduleStore
from .probes import HealthProbe
from .prober_client import ProberClient
from .routes import ActiveRoute, GateFile
from .service import ExitService
from .slots import KeySlots
from .survey import SurveyService
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
    survey: SurveyService | None = None


def build(settings: Settings, *, exit_conf: WgConf, wg: WireGuard, probe: HealthProbe,
          catalog: Catalog, keys: KeyStore, prober: ProberClient | None = None,
          standby_wg: WireGuard | None = None, standby_probe: HealthProbe | None = None,
          route: ActiveRoute | None = None,
          forbid_ips: Callable[[], list[str]] = lambda: [],
          clock: Callable[[], float] = time.time, rng: random.Random | None = None,
          pubkey: Callable[[str], Awaitable[str]] = public_key) -> Node:
    s = settings
    store = StateStore(s.state_file, s.peer_env_file, s.countries, s.standby_env_file)
    events = EventBus(EventStore(s.events_file), clock, initial=store.load()[1])
    cache = CatalogCache(s.catalog_file)
    tunnel = Tunnel(wg, probe, HealthTracker(s.strikes, s.warmup), clock, GateFile(s.ipv6_gate_file),
                    quality=s.quality)
    standby = None
    if standby_wg and standby_probe:
        standby = Tunnel(standby_wg, standby_probe, HealthTracker(s.strikes, s.warmup), clock,
                         slot="c", keyed=keys.current("c") is not None, quality=s.quality)
    service = ExitService(s, tunnel, catalog, store, events, exit_conf, standby=standby,
                          route=route or (ActiveRoute(s.active_file) if standby else None),
                          catalog_cache=cache,
                          quality_log=QualityLog(s.quality_log_file, s.quality_log_max_bytes),
                          clock=clock, rng=rng)

    def code_for(country: str) -> str:
        return service.catalog.code_for_country(country)

    slots = KeySlots(service, keys, events, prober, code_for, forbid_ips, pubkey,
                     s.key_expiry_warn_days)
    service.warning_sources.append(slots.warnings)
    prober_lock = asyncio.Lock()   # the prober has one tunnel
    finder = FindService(service, prober, events, code_for, forbid_ips, prober_lock) if prober else None
    survey = SurveyService(service, prober, events, prober_lock, code_for, forbid_ips,
                           QualityLog(s.survey_log_file, s.survey_log_max_bytes), s.quality,
                           SurveyScheduleStore(s.survey_schedule_file), clock) if prober else None
    return Node(s, service, events, slots, finder, CatalogSource(s, cache), survey)
