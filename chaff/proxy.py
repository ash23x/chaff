"""Async SOCKS5 proxy with chaff engine integration.

Sits between local applications and the upstream connection (VPN, Tor, etc).
All outbound traffic passes through the Poisson scheduler, padded to MTU,
with empty slots filled by indistinguishable chaff packets.

    App → chaff SOCKS5 → [padder + scheduler] → VPN/Tor → Internet

The proxy itself is transparent — applications configure it as a standard
SOCKS5 proxy and are unaware of the padding layer.
"""

import asyncio
import logging
import struct
from typing import Optional

from .config import ChaffConfig, SinkMode
from .engine import PacketQueue
from .stats import WindowStats

logger = logging.getLogger(__name__)

SOCKS5_VERSION = 0x05
SOCKS5_AUTH_NONE = 0x00
SOCKS5_CMD_CONNECT = 0x01
SOCKS5_ATYP_IPV4 = 0x01
SOCKS5_ATYP_DOMAIN = 0x03
SOCKS5_ATYP_IPV6 = 0x04


class ChaffSink:
    """Handles chaff packet disposal based on configured sink mode."""

    def __init__(self, config: ChaffConfig):
        self.config = config
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        """Establish connection to sink (if needed)."""
        if self.config.sink_mode == SinkMode.REFLECTOR:
            if not self.config.reflector_host:
                raise ValueError("Reflector mode requires reflector_host")
            reader, self._writer = await asyncio.open_connection(
                self.config.reflector_host, self.config.reflector_port
            )
            logger.info("Connected to reflector %s:%d",
                       self.config.reflector_host, self.config.reflector_port)

    async def send(self, data: bytes) -> None:
        """Send a packet to the configured sink."""
        if self.config.sink_mode == SinkMode.NULL:
            return  # /dev/null — packet vanishes
        if self._writer and not self._writer.is_closing():
            self._writer.write(data)
            await self._writer.drain()

    async def close(self) -> None:
        """Clean up sink connection."""
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()


class Socks5Handler:
    """Handles individual SOCKS5 client connections."""

    def __init__(self, packet_queue: PacketQueue):
        self.queue = packet_queue
        self._connection_callback = None
        self._blocked_callback = None

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        """Process a single SOCKS5 connection."""
        addr = writer.get_extra_info('peername')
        logger.debug("New connection from %s", addr)

        try:
            # SOCKS5 greeting
            header = await reader.readexactly(2)
            version, n_methods = struct.unpack('!BB', header)
            if version != SOCKS5_VERSION:
                writer.close()
                return
            await reader.readexactly(n_methods)  # consume method list

            # Reply: no auth required
            writer.write(struct.pack('!BB', SOCKS5_VERSION, SOCKS5_AUTH_NONE))
            await writer.drain()

            # Connection request
            req = await reader.readexactly(4)
            ver, cmd, _, atyp = struct.unpack('!BBBB', req)

            if cmd != SOCKS5_CMD_CONNECT:
                self._send_reply(writer, 0x07)  # command not supported
                return

            # Parse destination address
            dst_addr, dst_port = await self._parse_address(reader, atyp)
            if not dst_addr:
                self._send_reply(writer, 0x08)  # address type not supported
                return

            # Connect to upstream
            try:
                remote_reader, remote_writer = await asyncio.wait_for(
                    asyncio.open_connection(dst_addr, dst_port), timeout=10
                )
            except (OSError, asyncio.TimeoutError) as e:
                logger.warning("Upstream connect failed: %s:%d — %s",
                             dst_addr, dst_port, e)
                if self._blocked_callback:
                    self._blocked_callback(dst_addr)
                self._send_reply(writer, 0x05)  # connection refused
                return

            # Success reply
            if self._connection_callback:
                self._connection_callback(dst_addr, dst_port, "ok")
            self._send_reply(writer, 0x00)

            # Bidirectional relay through the chaff engine
            await asyncio.gather(
                self._relay_outbound(reader, remote_writer),
                self._relay_inbound(remote_reader, writer),
                return_exceptions=True
            )

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def _relay_outbound(self, reader: asyncio.StreamReader,
                              remote_writer: asyncio.StreamWriter) -> None:
        """Client → chaff engine → upstream."""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                # Real traffic goes through the queue (gets padded + scheduled)
                await self.queue.enqueue(data)
                # Also forward the actual data upstream
                remote_writer.write(data)
                await remote_writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            remote_writer.close()

    async def _relay_inbound(self, remote_reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        """Upstream → client (passthrough, no padding on inbound)."""
        try:
            while True:
                data = await remote_reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def _parse_address(self, reader: asyncio.StreamReader,
                             atyp: int) -> tuple:
        """Parse SOCKS5 destination address."""
        if atyp == SOCKS5_ATYP_IPV4:
            raw = await reader.readexactly(4)
            addr = '.'.join(str(b) for b in raw)
        elif atyp == SOCKS5_ATYP_DOMAIN:
            length = (await reader.readexactly(1))[0]
            addr = (await reader.readexactly(length)).decode()
        elif atyp == SOCKS5_ATYP_IPV6:
            raw = await reader.readexactly(16)
            addr = ':'.join(f'{raw[i]:02x}{raw[i+1]:02x}'
                          for i in range(0, 16, 2))
        else:
            return None, None

        port_data = await reader.readexactly(2)
        port = struct.unpack('!H', port_data)[0]
        return addr, port

    def _send_reply(self, writer: asyncio.StreamWriter,
                    status: int) -> None:
        """Send SOCKS5 reply."""
        reply = struct.pack('!BBBBIH', SOCKS5_VERSION, status, 0x00,
                           SOCKS5_ATYP_IPV4, 0, 0)
        writer.write(reply)


class ChaffProxy:
    """Main proxy server — ties SOCKS5 handler, chaff engine, and sink together."""

    def __init__(self, config: ChaffConfig, connection_callback=None, blocked_callback=None):
        self.config = config
        self.stats = WindowStats()
        self.sink = ChaffSink(config)
        self.queue = PacketQueue(config, stats_callback=self.stats.record)
        self.handler = Socks5Handler(self.queue)
        self.handler._connection_callback = connection_callback
        self.handler._blocked_callback = blocked_callback
        self._server: Optional[asyncio.Server] = None

    async def start(self) -> None:
        """Start the proxy server and chaff engine."""
        await self.sink.connect()

        self._server = await asyncio.start_server(
            self.handler.handle,
            self.config.proxy_host,
            self.config.proxy_port,
        )
        logger.info("SOCKS5 proxy listening on %s:%d",
                    self.config.proxy_host, self.config.proxy_port)

        # Start the chaff engine in the background
        asyncio.create_task(self.queue.run(self.sink.send))

    async def stop(self) -> None:
        """Graceful shutdown."""
        self.queue.stop()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        await self.sink.close()
        logger.info("Chaff proxy stopped. Final stats: %s", self.queue.stats)
