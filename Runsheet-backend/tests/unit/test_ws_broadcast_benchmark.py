"""
Benchmark test for broadcast latency under 50 concurrent clients.

Verifies p99 broadcast latency does not exceed 100ms with 50 concurrent
mock clients, as required by Req 6.8.

Requirements: 6.8
"""
import asyncio
import statistics
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from websocket.base_ws_manager import BaseWSManager


class BenchmarkWSManager(BaseWSManager):
    """Concrete subclass for benchmarking."""
    pass


def _make_fast_websocket() -> MagicMock:
    """Create a mock WebSocket with near-zero send latency."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


class TestBroadcastBenchmark:
    """Benchmark tests for broadcast latency."""

    @pytest.mark.asyncio
    async def test_broadcast_latency_50_clients_under_100ms(self):
        """Req 6.8: p99 broadcast latency under 50 concurrent clients ≤ 100ms."""
        manager = BenchmarkWSManager("benchmark")

        # Connect 50 clients
        clients = []
        for _ in range(50):
            ws = _make_fast_websocket()
            await manager.connect(ws)
            clients.append(ws)

        assert manager.get_connection_count() == 50

        # Run multiple broadcast rounds and measure latency
        latencies = []
        message = {"type": "benchmark", "data": {"payload": "x" * 100}}

        for _ in range(100):
            start = time.perf_counter()
            count = await manager.broadcast(message)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)
            assert count == 50

        # Calculate p99
        latencies.sort()
        p99_index = int(len(latencies) * 0.99) - 1
        p99_latency = latencies[p99_index]

        # Assert p99 is under 100ms
        assert p99_latency < 100.0, (
            f"p99 broadcast latency {p99_latency:.2f}ms exceeds 100ms threshold. "
            f"Mean: {statistics.mean(latencies):.2f}ms, "
            f"Median: {statistics.median(latencies):.2f}ms"
        )

    @pytest.mark.asyncio
    async def test_broadcast_latency_50_clients_with_backpressure(self):
        """Verify broadcast latency stays reasonable even with some backpressured clients."""
        manager = BenchmarkWSManager("benchmark", max_pending_messages=10)

        clients = []
        for _ in range(50):
            ws = _make_fast_websocket()
            await manager.connect(ws)
            clients.append(ws)

        # Set 10 clients to be over backpressure threshold
        for ws in clients[:10]:
            manager._clients[ws]["pending_count"] = 10

        latencies = []
        message = {"type": "benchmark", "data": {"payload": "test"}}

        for _ in range(50):
            start = time.perf_counter()
            count = await manager.broadcast(message)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)
            # 40 clients should receive (10 are backpressured)
            assert count == 40

        p99_index = int(len(latencies) * 0.99) - 1
        p99_latency = latencies[p99_index]

        assert p99_latency < 100.0, (
            f"p99 broadcast latency with backpressure {p99_latency:.2f}ms exceeds 100ms"
        )
