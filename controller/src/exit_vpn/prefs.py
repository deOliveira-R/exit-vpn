"""The owner's preferences (pure, immutable): where to exit and what every server must
offer. A change is a new `Prefs` from `updated()`, which validates everything against the
catalog first, so a refused change leaves nothing half-applied."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from .catalog import Catalog
from .errors import PolicyError
from .servers import FEATURES, Region


@dataclass(frozen=True)
class Prefs:
    regions: tuple[Region, ...]   # pinned regions in priority order (failover, rotate any)
    auto_failover: bool = True
    secure_core: bool = False     # a mode: on, only Secure Core servers; off, none
    allow_free: bool = False      # free-tier servers are crowded
    require: tuple[str, ...] = ()       # features every candidate must have
    favorites: tuple[str, ...] = field(default=())  # logical names, tried first in a tier

    @property
    def countries(self) -> list[str]:
        """The regions' countries, in order, without repeats."""
        out: list[str] = []
        for r in self.regions:
            if r.country.lower() not in [c.lower() for c in out]:
                out.append(r.country)
        return out

    def updated(self, catalog: Catalog, *, regions: Iterable[Region] | None = None,
                auto_failover: bool | None = None, secure_core: bool | None = None,
                allow_free: bool | None = None, require: Iterable[str] | None = None,
                favorites: Iterable[str] | None = None) -> Prefs:
        """A copy with the given changes, in the catalog's spelling; raises PolicyError
        (and changes nothing) for unknown places, servers or features."""
        changes: dict = {}
        if regions is not None:
            canon: list[Region] = []
            for r in regions:
                c = catalog.canonical_region(r.country, r.city)
                if c not in canon:
                    canon.append(c)
            if not canon:
                raise PolicyError("empty list")
            changes["regions"] = tuple(canon)
        if require is not None:
            bad = [f for f in require if f not in FEATURES]
            if bad:
                raise PolicyError(f"unknown features: {bad}")
            changes["require"] = tuple(dict.fromkeys(require))
        if favorites is not None:
            changes["favorites"] = tuple(dict.fromkeys(catalog.canonical_name(n) for n in favorites))
        for name, value in (("auto_failover", auto_failover), ("secure_core", secure_core),
                            ("allow_free", allow_free)):
            if value is not None:
                changes[name] = value
        return replace(self, **changes)

    # Wire format (the API's `prefs` object) and state.json -------------------------------
    def view(self) -> dict:
        return {**self.to_dict(), "countries": self.countries}

    def to_dict(self) -> dict:
        return {"regions": [r.to_dict() for r in self.regions], "auto_failover": self.auto_failover,
                "secure_core": self.secure_core, "allow_free": self.allow_free,
                "require": list(self.require), "favorites": list(self.favorites)}

    @classmethod
    def from_dict(cls, d: dict, default_countries: list[str]) -> Prefs:
        """Saved prefs; state from before pinned regions (countries only) is migrated."""
        regions = d.get("regions")
        if regions is None:
            regions = [{"country": c} for c in (d.get("countries") or default_countries)]
        return cls(regions=tuple(Region.from_dict(r) for r in regions),
                   auto_failover=d.get("auto_failover", True),
                   secure_core=d.get("secure_core", False),
                   allow_free=d.get("allow_free", False),
                   require=tuple(d.get("require", ())), favorites=tuple(d.get("favorites", ())))

    @classmethod
    def default(cls, countries: list[str]) -> Prefs:
        return cls(regions=tuple(Region(c) for c in countries))
