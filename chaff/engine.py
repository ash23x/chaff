"""Core chaff engine — Poisson scheduler, padder, and chaff generator.

The key insight: constant-rate padding is itself a detectable fingerprint.
Real network traffic follows Poisson-distributed inter-arrival times.
We exploit this by scheduling ALL outbound packets (real + chaff) on a
stochastic timeline that is statistically indistinguishable from organic
traffic at any observation window.

    delay = -ln(random()) / λ

Where λ is the target mean rate. This produces exponential inter-arrival
times — the mathematical signature of organic network events.
"""

import asyncio
import logging
import math
import os
import time
from collections import deque
from typing import Optional, Callable, Awaitable

from .config import ChaffConfig, ScheduleMode

logger = logging.getLogger(__name__)


class Scheduler:
    """Stochastic packet scheduler.

    Generates inter-packet delays that match the statistical profile
    of organic network traffic. The Poisson process is memoryless —
    each interval is independent, making the stream resistant to
    windowed statistical analysis.
    """

    def __init__(self, config: ChaffConfig):
        self.config = config
        self._lambda = config.target_rate

    def next_delay(self) -> float:
        """Generate next inter-packet delay in seconds."""
        if self.config.schedule_mode == ScheduleMode.POISSON:
            return self._poisson_delay()
        return self._jittered_delay()

    def _poisson_delay(self) -> float:
        """Exponential inter-arrival: delay = -ln(U) / λ"""
        u = max(1e-10, float.fromhex(os.urandom(8).hex()) / (1 << 64))
        return -math.log(u) / self._lambda

    def _jittered_delay(self) -> float:
        """Constant rate with uniform jitter (simple fallback)."""
        base = self.config.mean_interval()
        jitter_range = base * self.config.jitter_pct
        u = float.fromhex(os.urandom(8).hex()) / (1 << 64)
        return base + (u * 2 - 1) * jitter_range


class Padder:
    """Pads all packets to fixed MTU size with cryptographic randomness.

    After padding, a chaff packet and a real packet are byte-identical
    in structure at the transport layer. An observer sees only
    fixed-size blobs at stochastic intervals.
    """

    def __init__(self, config: ChaffConfig):
        self.pad_size = config.pad_size

    def pad(self, data: bytes) -> bytes:
        """Pad data to fixed size. Returns exactly pad_size bytes."""
        if len(data) >= self.pad_size:
            return data[:self.pad_size]
        padding_needed = self.pad_size - len(data)
        return data + os.urandom(padding_needed)

    def generate_chaff(self) -> bytes:
        """Generate a pure chaff packet — indistinguishable from padded real data."""
        return os.urandom(self.pad_size)


class PacketQueue:
    """Async queue that merges real traffic with chaff on a stochastic timeline.

    Real packets are queued and released at the next scheduled slot.
    Empty slots are filled with chaff. The observer sees a continuous
    stream of identically-sized packets at Poisson-distributed intervals
    regardless of actual traffic patterns.
    """

    def __init__(self, config: ChaffConfig, stats_callback: Optional[Callable] = None):
        self.config = config
        self.scheduler = Scheduler(config)
        self.padder = Padder(config)
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._running = False
        self._stats_callback = stats_callback
        self._real_count = 0
        self._chaff_count = 0
        self._start_time: Optional[float] = None
        self._last_report: float = 0
        self._report_interval: float = 5.0  # stats every 5 seconds
        self._intervals: deque = deque(maxlen=500)  # track recent intervals
        self._last_packet_time: Optional[float] = None

    async def enqueue(self, data: bytes) -> None:
        """Queue real traffic for padded transmission."""
        padded = self.padder.pad(data)
        await self._queue.put(padded)

    async def run(self, send_fn: Callable[[bytes], Awaitable[None]]) -> None:
        """Main scheduling loop. Runs until stopped.

        send_fn: async callable that transmits a packet to the sink/destination.
        """
        self._running = True
        self._start_time = time.monotonic()
        logger.info(
            "Chaff engine started: mode=%s, rate=%.0f pkt/s, "
            "est. bandwidth=%.1f Mbps",
            self.config.schedule_mode.value,
            self.config.target_rate,
            self.config.bandwidth_estimate_mbps()
        )

        while self._running:
            delay = self.scheduler.next_delay()
            await asyncio.sleep(delay)

            now = time.monotonic()
            if self._last_packet_time is not None:
                self._intervals.append(now - self._last_packet_time)
            self._last_packet_time = now

            try:
                packet = self._queue.get_nowait()
                self._real_count += 1
                packet_type = "real"
            except asyncio.QueueEmpty:
                packet = self.padder.generate_chaff()
                self._chaff_count += 1
                packet_type = "chaff"

            try:
                await send_fn(packet)
            except Exception as e:
                logger.warning("Send failed (%s): %s", packet_type, e)

            if self._stats_callback:
                self._stats_callback(packet_type, len(packet))

            # Periodic stats report
            if now - self._last_report >= self._report_interval:
                self._last_report = now
                total = self._real_count + self._chaff_count
                elapsed = now - self._start_time
                chaff_pct = (self._chaff_count / max(1, total)) * 100
                real_pct = 100 - chaff_pct
                rate = total / max(0.1, elapsed)
                bw = (rate * self.config.pad_size * 8) / 1_000_000
                mean_iv = (sum(self._intervals) / len(self._intervals) * 1000) if self._intervals else 0
                logger.info(
                    "STATS | %ds | %d pkts (%.0f%% chaff, %.0f%% real) | "
                    "%.1f pkt/s | %.2f Mbps | mean interval %.1fms",
                    int(elapsed), total, chaff_pct, real_pct,
                    rate, bw, mean_iv
                )

    def stop(self) -> None:
        """Signal the scheduling loop to stop."""
        self._running = False

    @property
    def stats(self) -> dict:
        """Current engine statistics."""
        elapsed = time.monotonic() - (self._start_time or time.monotonic())
        total = self._real_count + self._chaff_count
        return {
            "elapsed_s": round(elapsed, 1),
            "real_packets": self._real_count,
            "chaff_packets": self._chaff_count,
            "total_packets": total,
            "chaff_ratio": round(self._chaff_count / max(1, total), 3),
            "actual_rate": round(total / max(0.1, elapsed), 1),
            "target_rate": self.config.target_rate,
        }
