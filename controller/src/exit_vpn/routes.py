"""What the controller tells the exit's routing, shared with `route-keeper` (common.sh):

- The IPv6 gate file (`IPV6_GATE`): while it exists, forwarded IPv6 passes the kill switch to
  the active tunnel; otherwise new IPv6 connections are refused at once (open ones carry
  on). The file holds nothing; its presence is the signal.
- The active tunnel (`ActiveRoute`): forwarded traffic leaves by table 100's default route.
  A handover replaces it in one netlink message (`ip route replace`), so packets routed
  before it use the old tunnel and packets after it the new one, with nothing in between.
  The interface is also written to `ACTIVE_FILE`, for route-keeper to restore the route
  after the kernel drops it (a tunnel bouncing). Then the clients' connections through the
  old tunnel are ended (`flows`), so none waits for data that can no longer arrive.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import flows
from .persistence import atomic_write

log = logging.getLogger("exit_vpn")


class GateFile:
    def __init__(self, path: Path):
        self.path = path

    def set(self, open_: bool) -> None:
        try:
            if open_:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.touch()
            else:
                self.path.unlink(missing_ok=True)
        except OSError as e:   # the route stays as it was: closed unless it was open
            log.warning("could not %s the IPv6 gate: %s", "open" if open_ else "close", e)

    def is_set(self) -> bool:
        return self.path.exists()


class RouteError(RuntimeError):
    pass


class ActiveRoute:
    def __init__(self, active_file: Path, table: int = 100):
        self.active_file, self.table = active_file, table

    def current(self) -> str | None:
        """The active tunnel as last written (None: never, so wg0 as init-wg set it up)."""
        try:
            return self.active_file.read_text().strip() or None
        except OSError:
            return None

    async def set(self, ifname: str) -> None:
        """Forwarded traffic leaves via `ifname` from now on. The file first, so route-keeper
        never restores the old route in between; IPv4 is the one-step switch, IPv6 follows
        (its gate is closed during a handover anyway)."""
        atomic_write(self.active_file, ifname + "\n", 0o644)
        await _ip("-4", "route", "replace", "default", "dev", ifname, "table", str(self.table))
        await _ip("-6", "route", "replace", "default", "dev", ifname, "metric", "1",
                  "table", str(self.table))

    async def end_flows(self, old_ifname: str) -> int:
        """End the clients' connections still bound to `old_ifname`; returns how many."""
        return await flows.end_flows(old_ifname)


async def _ip(*args: str) -> None:
    p = await asyncio.create_subprocess_exec("ip", *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode != 0:
        raise RouteError(f"ip {' '.join(args)}: {err.decode().strip()}")
