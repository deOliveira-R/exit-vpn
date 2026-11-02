"""The quality of one exit tunnel's server, from live measurement only (pure; the clock is
passed in). No server history: a server's past would bias the choice against servers that
improved, so a meter starts empty on every new server or key.

Measured through the tunnel:
- **pings**, every second: a DNS query for a cached name to the server's own resolver
  (10.2.0.1, at the far end of the tunnel): round-trip time, loss, jitter;
- the **health cycles**, every 5 s: how long the HTTPS check took (a small page from a test
  site beyond the server) and the DNS lookup.

Over a sliding `window` this gives a `Summary`, and `assess` a `Quality`:
- *Critical*: a total drop (`drop_pings` lost in a row) or heavy recent loss, or every HTTPS
  check of the recent past failed;
- *Degraded*: loss, queueing (the 90th percentile RTT well above the window's minimum, i.e.
  standing queues on the path), jitter, or slow HTTPS;
- *Healthy* otherwise; *measuring* until there are enough pings to tell.
The thresholds are provisional: they are calibrated from the quality and survey logs.
`QualityTracker` damps the state: a better state must hold for `dwell` seconds before it is
reported (Critical at once), so a flapping server does not flood the event log.
"""

from __future__ import annotations

import enum
import math
from collections import deque
from dataclasses import asdict, dataclass


class Quality(enum.StrEnum):
    MEASURING = "measuring"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


RANK = {Quality.MEASURING: 0, Quality.HEALTHY: 1, Quality.DEGRADED: 2, Quality.CRITICAL: 3}


@dataclass(frozen=True)
class Thresholds:
    window: float = 180.0          # seconds of samples the summary covers
    min_pings: int = 30            # before a verdict
    drop_pings: int = 5            # lost in a row: a total drop
    recent: float = 30.0           # the span for Critical's loss and HTTPS checks
    critical_loss: float = 0.20    # of the recent pings
    degraded_loss: float = 0.02    # of the window's pings,
    degraded_lost: int = 2         # and at least this many (one lost ping is not a pattern)
    degraded_queue_ms: float = 100.0    # p90 RTT above the window's minimum
    degraded_jitter_ms: float = 30.0    # mean difference between consecutive RTTs
    degraded_https_ms: float = 2000.0   # median HTTPS check
    dwell: float = 10.0            # a better state holds this long before it counts


@dataclass(frozen=True)
class Summary:
    pings: int = 0
    lost: int = 0
    loss: float = 0.0              # of the window's pings, 0..1
    recent_loss: float = 0.0
    lost_in_row: int = 0
    rtt_min: float | None = None   # milliseconds
    rtt_p50: float | None = None
    rtt_p90: float | None = None
    jitter: float | None = None
    https_p50: float | None = None
    https_checks: int = 0
    https_failed: int = 0
    recent_https_failed: int = 0
    recent_https_checks: int = 0
    dns_p50: float | None = None

    def view(self) -> dict:
        """Rounded, for status and the log."""
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 3 if k.endswith("loss") else 1)
        return d


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


class QualityMeter:
    def __init__(self, th: Thresholds | None = None):
        self.th = th or Thresholds()
        self.pings: deque[tuple[float, float | None]] = deque()    # (t, rtt ms or None: lost)
        self.cycles: deque[tuple[float, float | None, float | None]] = deque()  # (t, https, dns)
        self.lost_in_row = 0

    def reset(self) -> None:
        """A new server or key: no history."""
        self.pings.clear()
        self.cycles.clear()
        self.lost_in_row = 0

    def ping(self, t: float, rtt_ms: float | None) -> None:
        self.pings.append((t, rtt_ms))
        self.lost_in_row = self.lost_in_row + 1 if rtt_ms is None else 0
        self._trim(t)

    def cycle(self, t: float, https_ms: float | None, dns_ms: float | None) -> None:
        """One health cycle: the fastest passing HTTPS check (None: all failed) and the DNS
        lookup (None: failed)."""
        self.cycles.append((t, https_ms, dns_ms))
        self._trim(t)

    def _trim(self, now: float) -> None:
        for q in (self.pings, self.cycles):
            while q and q[0][0] < now - self.th.window:
                q.popleft()

    def summary(self, now: float) -> Summary:
        self._trim(now)
        rtts = [r for _, r in self.pings if r is not None]
        lost = len(self.pings) - len(rtts)
        recent = [r for t, r in self.pings if t >= now - self.th.recent]
        seq = [r for _, r in self.pings]
        diffs = [abs(b - a) for a, b in zip(seq, seq[1:]) if a is not None and b is not None]
        https = [h for _, h, _ in self.cycles if h is not None]
        recent_cycles = [h for t, h, _ in self.cycles if t >= now - self.th.recent]
        dns = [d for _, _, d in self.cycles if d is not None]
        n = len(self.pings)
        return Summary(
            pings=n, lost=lost, loss=lost / n if n else 0.0,
            recent_loss=sum(r is None for r in recent) / len(recent) if recent else 0.0,
            lost_in_row=self.lost_in_row,
            rtt_min=min(rtts) if rtts else None, rtt_p50=percentile(rtts, 50),
            rtt_p90=percentile(rtts, 90), jitter=sum(diffs) / len(diffs) if diffs else None,
            https_p50=percentile(https, 50), https_checks=len(self.cycles),
            https_failed=len(self.cycles) - len(https),
            recent_https_checks=len(recent_cycles),
            recent_https_failed=sum(h is None for h in recent_cycles),
            dns_p50=percentile(dns, 50))

    def assess(self, now: float) -> tuple[Quality, list[str], Summary]:
        """The state, why, and the summary it was judged on."""
        s, th = self.summary(now), self.th
        critical, degraded = [], []
        if s.lost_in_row >= th.drop_pings:
            critical.append(f"{s.lost_in_row} pings lost in a row")
        if s.pings >= th.drop_pings and s.recent_loss >= th.critical_loss:
            critical.append(f"{s.recent_loss:.0%} loss in {th.recent:.0f} s")
        if s.recent_https_checks >= 2 and s.recent_https_failed == s.recent_https_checks:
            critical.append("every HTTPS check failed")
        if critical:
            return Quality.CRITICAL, critical, s
        if s.pings < th.min_pings:
            return Quality.MEASURING, [], s
        if s.loss >= th.degraded_loss and s.lost >= th.degraded_lost:
            degraded.append(f"{s.loss:.1%} loss")
        if s.rtt_p90 is not None and s.rtt_min is not None \
                and s.rtt_p90 - s.rtt_min >= th.degraded_queue_ms:
            degraded.append(f"RTT p90 {s.rtt_p90:.0f} ms vs min {s.rtt_min:.0f} ms")
        if s.jitter is not None and s.jitter >= th.degraded_jitter_ms:
            degraded.append(f"jitter {s.jitter:.0f} ms")
        if s.https_p50 is not None and s.https_p50 >= th.degraded_https_ms:
            degraded.append(f"HTTPS {s.https_p50:.0f} ms")
        return (Quality.DEGRADED, degraded, s) if degraded else (Quality.HEALTHY, [], s)


class QualityTracker:
    """The reported state: worse states count at once (Critical, Degraded), better ones once
    they held `dwell` seconds."""

    def __init__(self, dwell: float = 10.0):
        self.dwell = dwell
        self.state = Quality.MEASURING
        self.reasons: list[str] = []
        self.since = 0.0
        self._pending: Quality | None = None
        self._pending_since = 0.0

    def reset(self, now: float) -> None:
        self.state, self.reasons, self.since, self._pending = Quality.MEASURING, [], now, None

    def feed(self, q: Quality, reasons: list[str], now: float) -> bool:
        """One assessment. Returns True when the reported state changed."""
        if q == self.state:
            self._pending, self.reasons = None, reasons
            return False
        worse = RANK[q] > RANK[self.state] and q != Quality.MEASURING
        if not worse and self.state != Quality.MEASURING:
            if self._pending != q:
                self._pending, self._pending_since = q, now
            if now - self._pending_since < self.dwell:
                return False
        self.state, self.reasons, self.since, self._pending = q, reasons, now, None
        return True
