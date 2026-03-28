"""Tests for chaff engine — scheduler distribution, padder, and stats."""

import math
import statistics
import time
import pytest

from chaff.config import ChaffConfig, ScheduleMode
from chaff.engine import Scheduler, Padder, PacketQueue
from chaff.stats import WindowStats


class TestScheduler:
    """Verify Poisson scheduler produces valid exponential distribution."""

    def test_poisson_mean_converges(self):
        """Mean delay should converge to 1/λ over many samples."""
        config = ChaffConfig(schedule_mode=ScheduleMode.POISSON, target_rate=100)
        sched = Scheduler(config)
        delays = [sched.next_delay() for _ in range(10_000)]
        mean = statistics.mean(delays)
        expected = 1.0 / 100  # 0.01s
        assert abs(mean - expected) < 0.002, (
            f"Mean {mean:.4f} too far from expected {expected:.4f}"
        )

    def test_poisson_all_positive(self):
        """All delays must be positive."""
        config = ChaffConfig(schedule_mode=ScheduleMode.POISSON, target_rate=50)
        sched = Scheduler(config)
        delays = [sched.next_delay() for _ in range(5_000)]
        assert all(d > 0 for d in delays)

    def test_poisson_variance_matches_exponential(self):
        """Variance of exponential = 1/λ². Check within tolerance."""
        config = ChaffConfig(schedule_mode=ScheduleMode.POISSON, target_rate=200)
        sched = Scheduler(config)
        delays = [sched.next_delay() for _ in range(20_000)]
        var = statistics.variance(delays)
        expected_var = 1.0 / (200 ** 2)  # 0.000025
        assert abs(var - expected_var) < expected_var * 0.3, (
            f"Variance {var:.6f} too far from expected {expected_var:.6f}"
        )

    def test_jittered_stays_in_range(self):
        """Jittered mode should stay within ±jitter_pct of base interval."""
        config = ChaffConfig(
            schedule_mode=ScheduleMode.JITTERED,
            target_rate=100,
            jitter_pct=0.3,
        )
        sched = Scheduler(config)
        base = 0.01
        lo = base * (1 - 0.3)
        hi = base * (1 + 0.3)
        delays = [sched.next_delay() for _ in range(5_000)]
        assert all(lo <= d <= hi for d in delays), (
            f"Jittered delays outside [{lo}, {hi}]"
        )


class TestPadder:
    """Verify padding produces fixed-size indistinguishable packets."""

    def test_pad_short_data(self):
        config = ChaffConfig(pad_size=1500)
        padder = Padder(config)
        result = padder.pad(b"hello")
        assert len(result) == 1500
        assert result[:5] == b"hello"

    def test_pad_exact_size(self):
        config = ChaffConfig(pad_size=100)
        padder = Padder(config)
        data = b"x" * 100
        assert padder.pad(data) == data

    def test_pad_oversized_truncates(self):
        config = ChaffConfig(pad_size=10)
        padder = Padder(config)
        result = padder.pad(b"a" * 20)
        assert len(result) == 10

    def test_chaff_is_correct_size(self):
        config = ChaffConfig(pad_size=1500)
        padder = Padder(config)
        chaff = padder.generate_chaff()
        assert len(chaff) == 1500

    def test_chaff_is_random(self):
        """Two chaff packets should not be identical."""
        config = ChaffConfig(pad_size=1500)
        padder = Padder(config)
        a = padder.generate_chaff()
        b = padder.generate_chaff()
        assert a != b


class TestWindowStats:
    """Verify sliding window statistics."""

    def test_empty_stats(self):
        stats = WindowStats()
        assert stats.current_rate == 0.0
        assert stats.chaff_ratio == 0.0
        assert stats.bandwidth_mbps == 0.0

    def test_rate_calculation(self):
        stats = WindowStats(window_size=1.0)
        for _ in range(100):
            stats.record("real", 1500)
        rate = stats.current_rate
        assert rate > 50  # should be high given near-instant recording

    def test_chaff_ratio(self):
        stats = WindowStats()
        for _ in range(7):
            stats.record("chaff", 1500)
        for _ in range(3):
            stats.record("real", 1500)
        assert abs(stats.chaff_ratio - 0.7) < 0.01
