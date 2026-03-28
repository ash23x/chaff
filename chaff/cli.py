"""CLI entry point for chaff traffic padder."""

import argparse
import asyncio
import logging
import signal
import sys

from .config import ChaffConfig, ScheduleMode, SinkMode
from .proxy import ChaffProxy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chaff",
        description="Stochastic traffic padding proxy -- radar countermeasures "
                    "for your network traffic.",
        epilog="Everyone talks about timing attacks. This is a mitigation.",
    )
    parser.add_argument("--rate", type=float, default=100.0, help="Target pkt/s (default: 100)")
    parser.add_argument("--mode", choices=["poisson", "jittered"], default="poisson", help="Schedule mode")
    parser.add_argument("--sink", choices=["null", "reflector", "paired"], default="null", help="Chaff sink")
    parser.add_argument("--reflector-host", type=str, default=None, help="Reflector VPS host")
    parser.add_argument("--reflector-port", type=int, default=9999, help="Reflector port")
    parser.add_argument("--proxy-port", type=int, default=1080, help="SOCKS5 port")
    parser.add_argument("--pad-size", type=int, default=1500, help="Packet size (bytes)")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable dashboard")
    parser.add_argument("--dashboard-port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = ChaffConfig(
        schedule_mode=ScheduleMode(args.mode),
        target_rate=args.rate,
        pad_size=args.pad_size,
        sink_mode=SinkMode(args.sink),
        reflector_host=args.reflector_host,
        reflector_port=args.reflector_port,
        proxy_port=args.proxy_port,
        dashboard_port=args.dashboard_port,
        dashboard_enabled=not args.no_dashboard,
    )

    bw = config.bandwidth_estimate_mbps()
    print(f"\n  chaff v0.1.0 -- Radar countermeasures for your network traffic")
    print(f"  -------------------------------------------------------------")
    print(f"  Mode:       {config.schedule_mode.value}")
    print(f"  Rate:       {config.target_rate:.0f} pkt/s")
    print(f"  Pad size:   {config.pad_size} bytes")
    print(f"  Sink:       {config.sink_mode.value}")
    print(f"  Bandwidth:  ~{bw:.1f} Mbps")
    print(f"  Proxy:      socks5://{config.proxy_host}:{config.proxy_port}")
    if config.dashboard_enabled:
        print(f"  Dashboard:  http://localhost:{config.dashboard_port}")
    print()

    if config.dashboard_enabled:
        _run_with_dashboard(config)
    else:
        _run_headless(config)


def _run_headless(config: ChaffConfig) -> None:
    proxy = ChaffProxy(config)

    async def run():
        await proxy.start()
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            await proxy.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass

def _run_with_dashboard(config: ChaffConfig) -> None:
    from nicegui import app as nicegui_app
    from .dashboard import ChaffDashboard
    from .stats import WindowStats

    stats = WindowStats()
    dashboard = ChaffDashboard(config, stats)

    def on_packet(packet_type, size):
        stats.record(packet_type, size)
        dashboard.record_packet(packet_type, size)

    proxy = ChaffProxy(
        config,
        connection_callback=dashboard.record_connection,
        blocked_callback=dashboard.record_blocked,
    )
    proxy.queue._stats_callback = on_packet
    proxy.stats = stats
    dashboard.stats = stats

    @nicegui_app.on_startup
    async def startup():
        await proxy.start()

    @nicegui_app.on_shutdown
    async def shutdown():
        await proxy.stop()

    dashboard.build(port=config.dashboard_port)


if __name__ == "__main__":
    main()
