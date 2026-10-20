"""Health state machine (pure; the clock is passed in). PLAN Phase 3 'Health'."""

from __future__ import annotations

import enum


class Health(enum.StrEnum):
    STARTING = "starting"          # warm-up after start or a server change
    HEALTHY = "healthy"
    SUSPECT = "suspect"            # 1..strikes-1 failed cycles in a row
    FAILING_OVER = "failing_over"
    DEGRADED = "degraded"          # no candidate worked; the kill switch holds


class HealthTracker:
    def __init__(self, strikes: int = 3, warmup: float = 20.0):
        self.strikes_needed, self.warmup = strikes, warmup
        self.state = Health.STARTING
        self.strikes = 0
        self.grace_until = 0.0
        self.since = 0.0

    def _set(self, state: Health, now: float) -> None:
        if state != self.state:
            self.state, self.since = state, now

    def start_grace(self, now: float) -> None:
        """A new session: failures during warm-up don't count."""
        self.strikes = 0
        self.grace_until = now + self.warmup
        self._set(Health.STARTING, now)

    def feed(self, ok: bool, now: float) -> bool:
        """Record one cycle. Returns True when failover should start."""
        if ok:
            self.strikes = 0
            self.grace_until = 0.0  # warm-up ends at the first passing cycle
            self._set(Health.HEALTHY, now)
            return False
        if now < self.grace_until:
            return False
        self.strikes += 1
        if self.strikes >= self.strikes_needed:
            self._set(Health.FAILING_OVER, now)
            return True
        self._set(Health.SUSPECT, now)
        return False

    def failing_over(self, now: float) -> None:
        self._set(Health.FAILING_OVER, now)

    def degraded(self, now: float) -> None:
        self._set(Health.DEGRADED, now)


class Backoff:
    """Retry delay in DEGRADED: 30 s doubling to 5 min."""

    def __init__(self, first: float = 30.0, cap: float = 300.0):
        self.first, self.cap, self.next = first, cap, first

    def reset(self) -> None:
        self.next = self.first

    def step(self) -> float:
        d = self.next
        self.next = min(self.next * 2, self.cap)
        return d
