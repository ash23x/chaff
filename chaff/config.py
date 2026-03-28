"""Configuration for chaff traffic padder."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ScheduleMode(Enum):
    """Traffic scheduling strategy."""
    POISSON = "poisson"      # Exponential inter-arrival (default, recommended)
    JITTERED = "jittered"    # Constant rate with uniform jitter (simple fallback)


class SinkMode(Enum):
    """Where chaff packets go."""
    NULL = "null"            # Fire-and-forget to /dev/null (zero-config)
    REFLECTOR = "reflector"  # VPS echo server bounces back
    PAIRED = "paired"        # Two chaff instances padding each other


@dataclass
class ChaffConfig:
    """Runtime configuration for the chaff engine."""

    # Scheduling
    schedule_mode: ScheduleMode = ScheduleMode.POISSON
    target_rate: float = 100.0       # Mean packets/sec (λ for Poisson)
    jitter_pct: float = 0.3          # ±30% jitter in JITTERED mode

    # Padding
    pad_size: int = 1500             # Fixed packet size (MTU)

    # Chaff sink
    sink_mode: SinkMode = SinkMode.NULL
    reflector_host: Optional[str] = None
    reflector_port: int = 9999
    paired_host: Optional[str] = None
    paired_port: int = 9998

    # Proxy
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 1080           # SOCKS5 default

    # Dashboard
    dashboard_port: int = 8080
    dashboard_enabled: bool = True

    # Limits
    max_bandwidth_mbps: float = 2.0  # Safety cap
    buffer_size: int = 65536         # Socket buffer

    def mean_interval(self) -> float:
        """Mean inter-packet interval in seconds."""
        return 1.0 / self.target_rate

    def bandwidth_estimate_mbps(self) -> float:
        """Estimated bandwidth usage at target rate."""
        return (self.target_rate * self.pad_size * 8) / 1_000_000
