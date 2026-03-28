"""chaff dashboard — NiceGUI live monitoring panel.

Real-time visualisation of the Poisson scheduling engine,
packet ratios, connection activity, and traffic analysis.
Full Mr. Robot aesthetic.
"""

import asyncio
import time
from collections import deque
from typing import Optional

from nicegui import ui, app

from .config import ChaffConfig
from .stats import WindowStats


class ChaffDashboard:
    """Live monitoring dashboard for the chaff engine."""

    def __init__(self, config: ChaffConfig, stats: WindowStats):
        self.config = config
        self.stats = stats
        self._connections: deque = deque(maxlen=100)
        self._rate_history: deque = deque(maxlen=120)  # 10 min at 5s intervals
        self._chaff_history: deque = deque(maxlen=120)
        self._real_history: deque = deque(maxlen=120)
        self._time_labels: deque = deque(maxlen=120)
        self._total_real = 0
        self._total_chaff = 0
        self._start_time = time.monotonic()
        self._blocked_domains: deque = deque(maxlen=50)

    def record_packet(self, packet_type: str, size: int) -> None:
        """Called by engine on every packet."""
        if packet_type == "chaff":
            self._total_chaff += 1
        else:
            self._total_real += 1

    def record_connection(self, addr: str, port: int, status: str) -> None:
        """Called by proxy on connection events."""
        ts = time.strftime("%H:%M:%S")
        self._connections.appendleft({
            "time": ts,
            "dest": f"{addr}:{port}",
            "status": status,
        })

    def record_blocked(self, domain: str) -> None:
        """Called when upstream connect fails."""
        ts = time.strftime("%H:%M:%S")
        self._blocked_domains.appendleft({"time": ts, "domain": domain})

    def _snapshot(self) -> None:
        """Take a stats snapshot for time series."""
        elapsed = int(time.monotonic() - self._start_time)
        mins, secs = divmod(elapsed, 60)
        self._time_labels.append(f"{mins}:{secs:02d}")
        self._rate_history.append(round(self.stats.current_rate, 1))
        self._chaff_history.append(self._total_chaff)
        self._real_history.append(self._total_real)

    def build(self, port: int = 8080) -> None:
        """Build and serve the dashboard UI."""

        @ui.page('/')
        def main_page():
            # Dark theme
            ui.dark_mode().enable()
            ui.add_head_html('''
            <style>
                body { background: #0a0a0a !important; }
                .nicegui-content { padding: 16px !important; }
                .stat-card {
                    background: #111 !important;
                    border: 1px solid #1a3a1a !important;
                    border-radius: 8px;
                    padding: 16px;
                }
                .stat-value {
                    font-size: 2.2em;
                    font-weight: 700;
                    font-family: 'JetBrains Mono', 'Fira Code', monospace;
                }
                .conn-log {
                    font-family: 'JetBrains Mono', 'Fira Code', monospace;
                    font-size: 0.75em;
                    color: #4a4;
                    background: #050505;
                    border: 1px solid #1a1a1a;
                    padding: 8px;
                    max-height: 300px;
                    overflow-y: auto;
                }
                .blocked { color: #a44 !important; }
                .chaff-green { color: #0f0 !important; }
                .real-amber { color: #fa0 !important; }
                .header-text {
                    font-family: 'JetBrains Mono', monospace;
                    color: #0f0;
                    letter-spacing: 4px;
                }
            </style>
            <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
            ''')

            # Header
            with ui.row().classes('w-full items-center justify-between mb-4'):
                ui.label('CHAFF').classes('header-text text-3xl')
                ui.label('TRAFFIC PADDING ENGINE').classes('header-text text-sm opacity-60')
                status_label = ui.label('● ACTIVE').classes('text-green-400 font-mono text-lg')

            # Top stat cards row
            with ui.row().classes('w-full gap-4 mb-4'):
                # Packets card
                with ui.card().classes('stat-card flex-1'):
                    ui.label('TOTAL PACKETS').classes('text-xs text-gray-500 font-mono')
                    total_lbl = ui.label('0').classes('stat-value chaff-green')
                    with ui.row().classes('gap-4 mt-1'):
                        chaff_lbl = ui.label('CHAFF: 0').classes('text-xs text-green-600 font-mono')
                        real_lbl = ui.label('REAL: 0').classes('text-xs text-amber-500 font-mono')

                # Rate card
                with ui.card().classes('stat-card flex-1'):
                    ui.label('PACKET RATE').classes('text-xs text-gray-500 font-mono')
                    rate_lbl = ui.label('0.0').classes('stat-value chaff-green')
                    ui.label('pkt/s').classes('text-xs text-gray-600 font-mono')

                # Chaff ratio card
                with ui.card().classes('stat-card flex-1'):
                    ui.label('CHAFF RATIO').classes('text-xs text-gray-500 font-mono')
                    ratio_lbl = ui.label('0%').classes('stat-value chaff-green')
                    ui.label('cover density').classes('text-xs text-gray-600 font-mono')

                # Bandwidth card
                with ui.card().classes('stat-card flex-1'):
                    ui.label('BANDWIDTH').classes('text-xs text-gray-500 font-mono')
                    bw_lbl = ui.label('0.00').classes('stat-value chaff-green')
                    ui.label('Mbps').classes('text-xs text-gray-600 font-mono')

                # Uptime card
                with ui.card().classes('stat-card flex-1'):
                    ui.label('UPTIME').classes('text-xs text-gray-500 font-mono')
                    uptime_lbl = ui.label('00:00').classes('stat-value chaff-green')
                    mode_lbl = ui.label(f'{self.config.schedule_mode.value} | {self.config.target_rate:.0f} target').classes('text-xs text-gray-600 font-mono')

            # Charts row
            with ui.row().classes('w-full gap-4 mb-4'):
                # Rate over time chart
                with ui.card().classes('stat-card flex-1'):
                    ui.label('PACKET RATE (LIVE)').classes('text-xs text-gray-500 font-mono mb-2')
                    rate_chart = ui.echart({
                        'backgroundColor': 'transparent',
                        'grid': {'top': 10, 'right': 20, 'bottom': 30, 'left': 50},
                        'xAxis': {'type': 'category', 'data': [], 'axisLabel': {'color': '#444', 'fontSize': 9}},
                        'yAxis': {'type': 'value', 'axisLabel': {'color': '#444'}, 'splitLine': {'lineStyle': {'color': '#1a1a1a'}}},
                        'series': [{
                            'type': 'line',
                            'data': [],
                            'smooth': True,
                            'lineStyle': {'color': '#0f0', 'width': 2},
                            'areaStyle': {'color': {'type': 'linear', 'x': 0, 'y': 0, 'x2': 0, 'y2': 1,
                                'colorStops': [{'offset': 0, 'color': 'rgba(0,255,0,0.3)'}, {'offset': 1, 'color': 'rgba(0,255,0,0)'}]}},
                            'symbol': 'none',
                        }],
                        'animation': False,
                    }).classes('w-full').style('height: 200px')

                # Inter-arrival histogram
                with ui.card().classes('stat-card flex-1'):
                    ui.label('INTER-ARRIVAL DISTRIBUTION').classes('text-xs text-gray-500 font-mono mb-2')
                    hist_chart = ui.echart({
                        'backgroundColor': 'transparent',
                        'grid': {'top': 10, 'right': 20, 'bottom': 30, 'left': 50},
                        'xAxis': {'type': 'category', 'data': [], 'axisLabel': {'color': '#444', 'fontSize': 9, 'rotate': 45},
                                  'name': 'ms', 'nameTextStyle': {'color': '#444'}},
                        'yAxis': {'type': 'value', 'axisLabel': {'color': '#444'}, 'splitLine': {'lineStyle': {'color': '#1a1a1a'}}},
                        'series': [{
                            'type': 'bar',
                            'data': [],
                            'itemStyle': {'color': {'type': 'linear', 'x': 0, 'y': 0, 'x2': 0, 'y2': 1,
                                'colorStops': [{'offset': 0, 'color': '#0f0'}, {'offset': 1, 'color': '#040'}]}},
                        }],
                        'animation': False,
                    }).classes('w-full').style('height: 200px')

            # Bottom row: connection log + blocked
            with ui.row().classes('w-full gap-4'):
                # Connection log
                with ui.card().classes('stat-card flex-1'):
                    ui.label('CONNECTION LOG').classes('text-xs text-gray-500 font-mono mb-2')
                    conn_log = ui.column().classes('conn-log w-full')
                    conn_log.style('min-height: 200px')

                # Blocked domains
                with ui.card().classes('stat-card') .style('width: 350px'):
                    ui.label('BLOCKED / FAILED').classes('text-xs text-gray-500 font-mono mb-2')
                    blocked_log = ui.column().classes('conn-log w-full')
                    blocked_log.style('min-height: 200px')

            # Config footer
            with ui.row().classes('w-full mt-4 opacity-40'):
                ui.label(
                    f'chaff v0.1.0 | {self.config.schedule_mode.value} | '
                    f'{self.config.target_rate:.0f} pkt/s target | '
                    f'{self.config.pad_size}B pad | '
                    f'{self.config.sink_mode.value} sink | '
                    f'socks5://127.0.0.1:{self.config.proxy_port}'
                ).classes('text-xs font-mono text-gray-600')

            # Update timer
            async def update():
                self._snapshot()
                total = self._total_chaff + self._total_real
                elapsed = time.monotonic() - self._start_time
                mins, secs = divmod(int(elapsed), 60)
                hrs, mins = divmod(mins, 60)

                # Stat cards
                total_lbl.text = f'{total:,}'
                chaff_lbl.text = f'CHAFF: {self._total_chaff:,}'
                real_lbl.text = f'REAL: {self._total_real:,}'
                rate_lbl.text = f'{self.stats.current_rate:.1f}'
                ratio = (self._total_chaff / max(1, total)) * 100
                ratio_lbl.text = f'{ratio:.0f}%'
                bw_lbl.text = f'{self.stats.bandwidth_mbps:.2f}'
                uptime_lbl.text = f'{hrs:02d}:{mins:02d}:{secs:02d}' if hrs else f'{mins:02d}:{secs:02d}'

                # Rate chart
                rate_chart.options['xAxis']['data'] = list(self._time_labels)
                rate_chart.options['series'][0]['data'] = list(self._rate_history)
                rate_chart.update()

                # Histogram
                hist_data = self.stats.histogram(bins=25)
                if hist_data:
                    hist_chart.options['xAxis']['data'] = [f'{d[0]:.0f}' for d in hist_data]
                    hist_chart.options['series'][0]['data'] = [d[1] for d in hist_data]
                    hist_chart.update()

                # Connection log (rebuild last 30)
                conn_log.clear()
                for c in list(self._connections)[:30]:
                    color = 'text-green-500' if c['status'] == 'ok' else 'text-red-400'
                    with conn_log:
                        ui.label(
                            f"[{c['time']}] {c['status'].upper():>5} {c['dest']}"
                        ).classes(f'font-mono text-xs {color}').style('margin: 0; padding: 0; line-height: 1.4')

                # Blocked log
                blocked_log.clear()
                for b in list(self._blocked_domains)[:20]:
                    with blocked_log:
                        ui.label(
                            f"[{b['time']}] {b['domain']}"
                        ).classes('font-mono text-xs blocked').style('margin: 0; padding: 0; line-height: 1.4')

            ui.timer(2.0, update)

        ui.run(port=port, title='chaff', favicon='🔒', reload=False, show=False)
