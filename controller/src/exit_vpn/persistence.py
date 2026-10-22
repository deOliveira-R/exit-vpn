"""Files the controller keeps in its state volume (`/var/lib/exit-vpn`), all written
atomically (temp file, fsync, rename; 0600):

- `state.json`: preferences, the active and standby servers, the blacklist (`StateStore`).
- `peer.env`: the active peer, read by `init-wg` on the next start, so the tunnel comes up
  on the last good server before Python runs (on wg0, which starts as the active tunnel).
  `standby.env`: likewise the standby's peer (on wg1). Shell contract: `PEER_NAME`,
  `PEER_PUBKEY`, `PEER_ENDPOINT`, single-quoted (see `common.sh`).
- `events.json`: the recent event log (`EventStore`).
- `catalog.json`: the last good catalog (`CatalogCache`), so restarts and upstream outages
  keep a working server list.
Key files live in the key volumes (`keys.KeyStore`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .catalog import MIN_SERVERS, Catalog, CatalogError
from .prefs import Prefs
from .servers import Server
from .state import TunnelServer, Blacklist, State

log = logging.getLogger("exit_vpn")


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


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


class StateStore:
    def __init__(self, path: Path, peer_env: Path, default_countries: list[str],
                 standby_env: Path | None = None):
        self.path, self.peer_env, self.default_countries = path, peer_env, default_countries
        self.standby_env = standby_env

    def load(self) -> tuple[State, list[dict]]:
        """The saved state, and the events an older version kept inside state.json."""
        try:
            d = json.loads(self.path.read_text())
        except FileNotFoundError:
            return State(Prefs.default(self.default_countries)), []
        prefs = Prefs.from_dict(d.get("prefs", {}), self.default_countries)
        active = TunnelServer.from_dict(d["current"]) if d.get("current") else None
        standby = TunnelServer.from_dict(d["standby"]) if d.get("standby") else None
        return (State(prefs, active, Blacklist(d.get("blacklist", {})), standby),
                d.get("events", []))

    def save(self, state: State) -> None:
        atomic_write(self.path, json.dumps({
            "prefs": state.prefs.to_dict(),
            "current": state.active.to_dict() if state.active else None,
            "standby": state.standby.to_dict() if state.standby else None,
            "blacklist": state.blacklist.to_dict()}, indent=1))
        if state.active:
            atomic_write(self.peer_env, _peer_env(state.active))
        if state.standby and self.standby_env:
            atomic_write(self.standby_env, _peer_env(state.standby))


def _peer_env(a: TunnelServer) -> str:
    return "".join(f"{k}={_shell_quote(v)}\n"
                   for k, v in (("PEER_NAME", a.name), ("PEER_PUBKEY", a.pubkey),
                                ("PEER_ENDPOINT", a.endpoint)))


class EventStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict] | None:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def save(self, events: list[dict]) -> None:
        atomic_write(self.path, json.dumps(events))


class CatalogCache:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Catalog:
        d = json.loads(self.path.read_text())
        fields = Server.__dataclass_fields__
        servers = [Server(**{k: v for k, v in s.items() if k in fields}) for s in d["servers"]]
        if len(servers) < MIN_SERVERS:
            raise CatalogError(f"cached catalog has only {len(servers)} servers")
        return Catalog(servers, d.get("source", ""), d.get("fetched_at", 0.0))

    def save(self, catalog: Catalog) -> None:
        atomic_write(self.path, json.dumps({
            "source": catalog.source, "fetched_at": catalog.fetched_at,
            "servers": [s.to_dict() for s in catalog.servers]}))
