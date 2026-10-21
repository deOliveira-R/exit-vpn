"""The IPv6 gate file, shared with the exit's `route-keeper` (common.sh's `IPV6_GATE`): while
it exists, forwarded IPv6 passes the kill switch to wg0; otherwise it is refused at once. The
file holds nothing; its presence is the signal."""

from __future__ import annotations

import logging
from pathlib import Path

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
