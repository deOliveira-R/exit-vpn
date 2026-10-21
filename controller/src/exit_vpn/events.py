"""The event log and live feed. Every event goes through `EventBus.publish`: it is logged,
kept (the last 50, saved to `events.json`, so restarts keep the history) and sent to every
`/v1/events` subscriber. Kinds and fields: contracts/README.md (a test fails when the code
can emit a kind without a sample there)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from .persistence import EventStore

log = logging.getLogger("exit_vpn")
KEEP = 50


class EventBus:
    def __init__(self, store: EventStore | None = None, clock: Callable[[], float] = time.time,
                 initial: list[dict] | None = None):
        self.store, self.clock = store, clock
        saved = store.load() if store else None
        self._events: list[dict] = (saved if saved is not None else list(initial or []))[-KEEP:]
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, kind: str, **data) -> dict:
        ev = {"ts": self.clock(), "kind": kind, **data}
        log.info("%s %s", kind, data)
        self._events.append(ev)
        del self._events[:-KEEP]
        if self.store:
            try:
                self.store.save(self._events)
            except OSError as e:   # a full or read-only volume must not stop the exit
                log.warning("could not save events: %s", e)
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(ev)
        return ev

    def recent(self, n: int = KEEP) -> list[dict]:
        return self._events[-n:]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
