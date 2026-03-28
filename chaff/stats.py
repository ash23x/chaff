"""Real-time statistics tracking for chaff engine."""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class WindowStats:
    """Sliding window statistics for rate and ratio monitoring."""
    window_size: float = 10.0  # seconds
    _events: deque = field(default_factory=deque)

    def record(self, packet_type: str, size: int) -> None:
        """Record a packet event."""
        now = time.monotonic()
        self._events.append((now, packet_type, size))
        self._prune(now)

    def _prune(self, now: float) -> None:
        """Remove events outside the window."""
        cutoff = now - self.window_size
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    @property
    def current_rate(self) -> float:
        """Packets per second in current window."""
        now = time.monotonic()
        self._prune(now)
        if not self._events:
            return 0.0
        span = now - self._events[0][0]
        return len(self._events) / max(0.01, span)

    @property
    def chaff_ratio(self) -> float:
        """Proportion of chaff in current window."""
        if not self._events:
            return 0.0
        chaff = sum(1 for _, t, _ in self._events if t == "chaff")
        return chaff / len(self._events)

    @property
    def bandwidth_mbps(self) -> float:
        """Current bandwidth usage in Mbps."""
        now = time.monotonic()
        self._prune(now)
        if not self._events:
            return 0.0
        total_bytes = sum(s for _, _, s in self._events)
        span = now - self._events[0][0]
        return (total_bytes * 8) / (max(0.01, span) * 1_000_000)

    def histogram(self, bins: int = 20) -> List[Tuple[float, int]]:
        """Inter-arrival time histogram for visual verification.

        If the engine is working correctly, this should approximate
        an exponential distribution in Poisson mode.
        """
        if len(self._events) < 2:
            return []
        times = [t for t, _, _ in self._events]
        intervals = [times[i+1] - times[i] for i in range(len(times)-1)]
        if not intervals:
            return []
        max_interval = max(intervals)
        bin_width = max_interval / bins
        if bin_width == 0:
            return []
        counts = [0] * bins
        for iv in intervals:
            idx = min(int(iv / bin_width), bins - 1)
            counts[idx] += 1
        return [(round(i * bin_width * 1000, 2), c) for i, c in enumerate(counts)]
