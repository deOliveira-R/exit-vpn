"""Settings from the environment (compose passes them from .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# gluetun-servers, pinned: the catalog supplies WireGuard endpoints and public keys, so a
# tampered catalog could steer exit traffic to a server we don't trust. Update by
# bumping the commit deliberately (PLAN D12).
DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/qdm12/gluetun-servers/"
    "0c7381faba8b0ef5d59caec11eae7ef629f6b4c9/pkg/servers/protonvpn.json"
)


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    vpn_conf: Path
    wg_if: str = "wg0"
    state_dir: Path = Path("/var/lib/exit-vpn")
    ts_socket: Path = Path("/run/tailscale/tailscaled.sock")
    provider: str = "proton"
    catalog_url: str = DEFAULT_CATALOG_URL
    # Every catalog hostname must end with this (sanity check against a bad catalog)
    hostname_suffix: str = ".protonvpn.net"
    wg_port: int = 51820
    countries: list[str] = field(default_factory=lambda: ["United States", "Switzerland"])
    owners: list[str] = field(default_factory=list)
    api_port: int = 8080
    health_interval: float = 5.0
    strikes: int = 3
    # A fresh session drops TCP for 4-14 s (PLAN Phase 0 results)
    warmup: float = 20.0
    # A new key may also need its registration to reach the server
    key_warmup: float = 45.0
    key_dirs: dict[str, Path] = field(
        default_factory=lambda: {"a": Path("/keys/a"), "b": Path("/keys/b")})
    probe_conf: Path | None = None       # slot b's bootstrap config (the prober's)
    prober_url: str = ""                 # the prober container (empty: no /v1/find)
    version: str = "dev"                 # the image's release (node-vX.Y.Z)
    key_expiry_warn_days: float = 14.0
    pushed_catalog_max_age: float = 7 * 86400
    handshake_max_age: float = 180.0
    blacklist_ttl: float = 30 * 60
    probe_timeout: float = 3.0
    dns_timeout: float = 2.0
    probe_urls: list[str] = field(
        default_factory=lambda: [
            "https://www.cloudflare.com/cdn-cgi/trace",
            "https://www.google.com/generate_204",
        ]
    )
    dns_probe_name: str = "www.cloudflare.com"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def catalog_file(self) -> Path:
        return self.state_dir / "catalog.json"

    @property
    def peer_env_file(self) -> Path:
        return self.state_dir / "peer.env"

    @property
    def events_file(self) -> Path:
        return self.state_dir / "events.json"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        kw: dict = {"vpn_conf": Path(e["EXIT_VPN_CONF"])}
        simple = {
            "WG_IF": ("wg_if", str),
            "STATE_DIR": ("state_dir", Path),
            "TS_SOCKET": ("ts_socket", Path),
            "VPN_PROVIDER": ("provider", str),
            "CATALOG_URL": ("catalog_url", str),
            "CATALOG_HOSTNAME_SUFFIX": ("hostname_suffix", str),
            "API_PORT": ("api_port", int),
            "HEALTH_INTERVAL": ("health_interval", float),
            "HEALTH_STRIKES": ("strikes", int),
            "WARMUP_SECONDS": ("warmup", float),
            "KEY_WARMUP_SECONDS": ("key_warmup", float),
            "BLACKLIST_SECONDS": ("blacklist_ttl", float),
        }
        for var, (name, conv) in simple.items():
            if e.get(var):
                kw[name] = conv(e[var])
        if e.get("EXIT_VPN_VERSION"):
            kw["version"] = e["EXIT_VPN_VERSION"]
        if e.get("PROBER_URL"):
            kw["prober_url"] = e["PROBER_URL"]
        if e.get("PROBE_VPN_CONF"):
            kw["probe_conf"] = Path(e["PROBE_VPN_CONF"])
        if e.get("KEY_DIR_A") or e.get("KEY_DIR_B"):
            kw["key_dirs"] = {"a": Path(e.get("KEY_DIR_A", "/keys/a")),
                              "b": Path(e.get("KEY_DIR_B", "/keys/b"))}
        if e.get("EXIT_COUNTRIES"):
            kw["countries"] = _list(e["EXIT_COUNTRIES"])
        if e.get("EXIT_OWNERS"):
            kw["owners"] = _list(e["EXIT_OWNERS"])
        return cls(**kw)
