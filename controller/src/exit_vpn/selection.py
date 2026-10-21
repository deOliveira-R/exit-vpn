"""Which servers to try, in which order (pure functions of the catalog, the current server,
the preferences and the blacklist). PLAN Phase 3 'Failover policy', Phase 5.

Rules every list follows:
- **Eligible** (`eligible`): enabled, not Tor, in the Secure Core mode (on: only Secure Core
  servers; off: none), free tier only if allowed, and every required feature, plus the hard
  features (P2P) of the profile being kept.
- **Tiers**: candidates come in tiers (same city, same country, each pinned region...);
  inside a tier, favourites first, then servers with the whole profile, then lower load
  (in 20 % buckets, random inside a bucket to spread load), at most `PER_TIER` each.
- Never the current server, never a blacklisted key, no physical server twice.

An explicit pick (`exact`) skips load and the blacklist, but not the Secure Core mode
(failover could never keep it) and not maintenance.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

from .errors import PolicyError
from .prefs import Prefs
from .servers import HARD_FEATURES, Region, Server, by_load

PER_TIER = 5
SCOPES = ("failover", "city", "country", "any")


def eligible(s: Server, prefs: Prefs, profile: Iterable[str] = ()) -> bool:
    if not s.enabled or s.tor:
        return False
    if s.secure_core != prefs.secure_core:
        return False
    if s.free and not prefs.allow_free:
        return False
    need = set(prefs.require) | (set(profile) & set(HARD_FEATURES))
    return all(s.has(f) for f in need)


def _load_bucket(s: Server) -> int:
    return 2 if s.load is None else s.load // 20   # unknown load ranks like ~50 %


def _order_tier(servers: Iterable[Server], rng: random.Random, limit: int,
                profile: Iterable[str], favorites: Iterable[str]) -> list[Server]:
    soft = [f for f in profile if f not in HARD_FEATURES]
    fav = {f.lower() for f in favorites}
    tier = list(servers)
    rng.shuffle(tier)   # stable sort below: random within equal keys
    tier.sort(key=lambda s: (s.name.lower() not in fav, sum(not s.has(f) for f in soft),
                             _load_bucket(s)))
    return tier[:limit]


def _flatten(tiers: list[list[Server]], rng: random.Random, per_tier: int,
             profile: list[str], prefs: Prefs) -> list[Server]:
    out: list[Server] = []
    seen: set[tuple[str, str]] = set()
    for t in tiers:
        for s in _order_tier(t, rng, per_tier, profile, prefs.favorites):
            if s.id not in seen:
                seen.add(s.id)
                out.append(s)
    return out


def _pool(servers: list[Server], current: Server | None, prefs: Prefs, blacklisted: set[str],
          profile: list[str]) -> list[Server]:
    return [s for s in servers if eligible(s, prefs, profile) and s.pubkey not in blacklisted
            and (current is None or s.pubkey != current.pubkey)]


def choose_candidates(servers: list[Server], current: Server | None, prefs: Prefs,
                      blacklisted: set[str], rng: random.Random, scope: str = "failover",
                      per_tier: int = PER_TIER, profile: Iterable[str] = ()) -> list[Server]:
    """Failover and rotation. Scopes:
    failover: same city, then the rest of the country, then the pinned regions
    city / country: the current server's city / country (any city in it)
    any: the pinned regions (the current country first if it is not pinned)"""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    profile = list(profile)
    pool = _pool(servers, current, prefs, blacklisted, profile)
    cur_country = current.country.lower() if current else ""
    cur_city = current.city.lower() if current else ""
    in_country = [s for s in pool if s.country.lower() == cur_country]

    tiers: list[list[Server]] = []
    if scope in ("failover", "city") and current:
        tiers.append([s for s in in_country if s.city.lower() == cur_city])
    if scope == "failover" and current:
        tiers.append([s for s in in_country if s.city.lower() != cur_city])
    if scope == "country" and current:
        tiers.append(in_country)
    if scope in ("failover", "any"):
        regions = list(prefs.regions)
        if scope == "any" and current and cur_country not in [r.country.lower() for r in regions]:
            regions.insert(0, Region(current.country))
        for r in regions:
            if scope == "failover" and r.country.lower() == cur_country:
                continue   # already covered by the city/country tiers
            tiers.append([s for s in pool if r.contains(s, prefs.secure_core)])
    return _flatten(tiers, rng, per_tier, profile, prefs)


def region_candidates(servers: list[Server], current: Server | None, prefs: Prefs,
                      blacklisted: set[str], rng: random.Random, region: Region,
                      per_tier: int = PER_TIER) -> list[Server]:
    """A pick of a country or city: one tier, ordered like every other."""
    pool = _pool(servers, current, prefs, blacklisted, [])
    return _flatten([[s for s in pool if region.contains(s, prefs.secure_core)]], rng,
                    per_tier, [], prefs)


def exact(found: list[Server], prefs: Prefs, name: str, ip: str | None = None) -> list[Server]:
    """The physical servers of an explicit pick (`found`: the catalog's servers with that
    name), least loaded first; raises PolicyError when the pick can't be honoured."""
    found = [s for s in found if ip is None or s.ip == ip]
    if not found:
        raise PolicyError(f"no server {name!r}" + (f" at {ip}" if ip else ""))
    if any(s.secure_core != prefs.secure_core for s in found):
        raise PolicyError("Secure Core is " + ("on: pick a Secure Core server"
                          if prefs.secure_core else "off: turn it on first"))
    usable = [s for s in found if s.enabled]
    if not usable:
        raise PolicyError(f"{name} is in maintenance")
    return by_load(usable)


def mode_switch_candidates(servers: list[Server], current: Server | None, prefs: Prefs,
                           blacklisted: set[str], rng: random.Random) -> list[Server]:
    """Turning Secure Core on or off (`prefs` already in the new mode): the current
    country, then the pinned regions, without a profile (Secure Core servers carry no P2P)."""
    cands = (choose_candidates(servers, current, prefs, blacklisted, rng, "country")
             + choose_candidates(servers, current, prefs, blacklisted, rng, "any"))
    return list({s.id: s for s in cands}.values())


def find_candidates(servers: list[Server], current: Server | None, prefs: Prefs,
                    blacklisted: set[str], countries: list[str], limit: int,
                    rng: random.Random) -> list[Server]:
    """Candidates for "find a server this URL accepts" (PLAN Phase 4): countries in the
    given order (default: the current country, then the pinned regions' countries); within a
    country, streaming-flagged servers first and cities interleaved for diversity; never the
    active server (the prober must not share it)."""
    order = list(countries) or ([current.country] if current and current.country else []) + [
        c for c in prefs.countries]
    seen_c, out = set(), []
    for c in order:
        if c.lower() in seen_c:
            continue
        seen_c.add(c.lower())
        pool = [s for s in _pool(servers, current, prefs, blacklisted, [])
                if s.country.lower() == c.lower()]
        by_city: dict[str, list[Server]] = {}
        for s in pool:
            by_city.setdefault(s.city, []).append(s)
        for lst in by_city.values():
            rng.shuffle(lst)
            lst.sort(key=lambda s: not s.stream)  # stable: streaming first, shuffled within
        cities = list(by_city)
        rng.shuffle(cities)
        while any(by_city.values()) and len(out) < limit:
            for city in cities:
                if by_city[city] and len(out) < limit:
                    out.append(by_city[city].pop(0))
        if len(out) >= limit:
            break
    return out
