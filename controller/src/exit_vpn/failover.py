"""Candidate selection (pure). PLAN Phase 3 'Failover policy', Phase 5 (regions, profiles,
Secure Core on/off)."""

from __future__ import annotations

import random
from collections.abc import Iterable

from .catalog import Server
from .state import HARD_FEATURES, Prefs

PER_TIER = 5


def eligible(s: Server, prefs: Prefs, profile: Iterable[str] = ()) -> bool:
    """Secure Core is a mode: on means only Secure Core servers, off means none.
    Hard features (P2P) in the profile, and `prefs.require`, must be present."""
    if not s.enabled or s.tor:
        return False
    if s.secure_core != prefs.secure_core:
        return False
    if s.free and not prefs.allow_free:
        return False
    need = set(prefs.require) | (set(profile) & set(HARD_FEATURES))
    return all(getattr(s, f, False) is True for f in need)


def in_region(s: Server, region: dict, secure_core: bool = False) -> bool:
    if s.country.lower() != region["country"].lower():
        return False
    city = region.get("city")
    # Secure Core servers are few per country: a city pin narrows only regular servers
    return not city or secure_core or s.city.lower() == city.lower()


def by_load(servers: Iterable[Server]) -> list[Server]:
    return sorted(servers, key=lambda s: 50 if s.load is None else s.load)


def _load_bucket(s: Server) -> int:
    return 2 if s.load is None else s.load // 20   # unknown load ranks like ~50 %


def _tier(servers: Iterable[Server], rng: random.Random, limit: int,
          profile: Iterable[str] = (), favorites: Iterable[str] = ()) -> list[Server]:
    soft = [f for f in profile if f not in HARD_FEATURES]
    fav = {f.lower() for f in favorites}
    tier = list(servers)
    rng.shuffle(tier)  # spread load within a tier...
    # ...but favourites first, then the full profile, then less loaded servers (stable:
    # random within a bucket)
    tier.sort(key=lambda s: (s.name.lower() not in fav,
                             sum(not getattr(s, f, False) for f in soft), _load_bucket(s)))
    return tier[:limit]


def choose_candidates(servers: list[Server], current: Server | None, prefs: Prefs,
                      blacklisted: set[str], rng: random.Random,
                      scope: str = "failover", per_tier: int = PER_TIER,
                      profile: Iterable[str] = ()) -> list[Server]:
    """Ordered candidates. Scopes:
    failover: same city, then the rest of the country, then the pinned regions
    city / country: the current server's city / country (any city in it)
    any: the pinned regions (current country first if it is not pinned, then in order)
    `profile`: features of a manually picked server to keep (PLAN Phase 5)."""
    if scope not in ("failover", "city", "country", "any"):
        raise ValueError(f"unknown scope {scope!r}")
    profile = list(profile)
    pool = [s for s in servers if eligible(s, prefs, profile) and s.pubkey not in blacklisted
            and (current is None or s.pubkey != current.pubkey)]
    cur_country = current.country.lower() if current else ""
    cur_city = current.city.lower() if current else ""

    def in_country(c: str):
        return [s for s in pool if s.country.lower() == c.lower()]

    tiers: list[list[Server]] = []
    if scope in ("failover", "city") and current:
        tiers.append([s for s in in_country(cur_country) if s.city.lower() == cur_city])
    if scope == "failover" and current:
        tiers.append([s for s in in_country(cur_country) if s.city.lower() != cur_city])
    if scope == "country" and current:
        tiers.append(in_country(cur_country))
    if scope in ("failover", "any"):
        regions = list(prefs.regions)
        if scope == "any" and current and cur_country not in [r["country"].lower() for r in regions]:
            regions.insert(0, {"country": current.country, "city": None})
        for r in regions:
            if scope == "failover" and r["country"].lower() == cur_country:
                continue  # already covered by the city/country tiers
            tiers.append([s for s in pool if in_region(s, r, prefs.secure_core)])

    out: list[Server] = []
    seen: set[tuple[str, str]] = set()
    for t in tiers:
        for s in _tier(t, rng, per_tier, profile, prefs.favorites):
            if (s.pubkey, s.ip) not in seen:
                seen.add((s.pubkey, s.ip))
                out.append(s)
    return out


def find_candidates(servers: list[Server], current: Server | None, prefs: Prefs,
                    blacklisted: set[str], countries: list[str], limit: int,
                    rng: random.Random) -> list[Server]:
    """Candidates for "find a server this URL accepts" (PLAN Phase 4): countries in the
    given order (default: the current country, then the pinned regions' countries); within a
    country, streaming-flagged servers first and cities interleaved for diversity;
    never the active server (the prober must not share it)."""
    order = list(countries) or ([current.country] if current and current.country else []) + [
        c for c in prefs.countries]
    seen_c, out = set(), []
    for c in order:
        if c.lower() in seen_c:
            continue
        seen_c.add(c.lower())
        pool = [s for s in servers if s.country.lower() == c.lower() and eligible(s, prefs)
                and s.pubkey not in blacklisted and (current is None or s.pubkey != current.pubkey)]
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
