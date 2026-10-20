"""Candidate selection (pure). PLAN Phase 3 'Failover policy'."""

from __future__ import annotations

import random
from collections.abc import Iterable

from .catalog import Server
from .state import Prefs

PER_TIER = 5


def eligible(s: Server, prefs: Prefs) -> bool:
    if s.tor:
        return False
    if s.secure_core and not prefs.secure_core:
        return False
    if s.free and not prefs.allow_free:
        return False
    return True


def _tier(servers: Iterable[Server], rng: random.Random, limit: int) -> list[Server]:
    tier = list(servers)
    rng.shuffle(tier)  # spread load within a tier
    return tier[:limit]


def choose_candidates(servers: list[Server], current: Server | None, prefs: Prefs,
                      blacklisted: set[str], rng: random.Random,
                      scope: str = "failover", per_tier: int = PER_TIER) -> list[Server]:
    """Ordered candidates. Scopes:
    failover: same city, then the rest of the country, then the preference countries
    city / country: the current server's city / country (any city in it)
    any: the preference countries (current country first, then in order)"""
    pool = [s for s in servers if eligible(s, prefs) and s.pubkey not in blacklisted
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
        countries = list(prefs.countries)
        if scope == "any" and current and cur_country not in [c.lower() for c in countries]:
            countries.insert(0, current.country)
        for c in countries:
            if scope == "failover" and c.lower() == cur_country:
                continue  # already covered by the city/country tiers
            tiers.append(in_country(c))
    if scope not in ("failover", "city", "country", "any"):
        raise ValueError(f"unknown scope {scope!r}")

    out: list[Server] = []
    seen: set[str] = set()
    for t in tiers:
        for s in _tier(t, rng, per_tier):
            if s.pubkey not in seen:
                seen.add(s.pubkey)
                out.append(s)
    return out


def find_candidates(servers: list[Server], current: Server | None, prefs: Prefs,
                    blacklisted: set[str], countries: list[str], limit: int,
                    rng: random.Random) -> list[Server]:
    """Candidates for "find a server this URL accepts" (PLAN Phase 4): countries in the
    given order (default: the current country, then the preferences); within a
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
