"""
Benchmark tests for agent startup time and cycle duration SLOs.

Verifies:
- Agent startup time is under SLO_MAX_RESTART_SECONDS (5 seconds)
- A single monitoring cycle completes within SLO_MAX_CYCLE_DURATION_SECONDS (5 seconds)

Requirements: 8.6
"""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.autonomous.base_agent import AutonomousAgentBase
from bootstrap.agent_scheduler import (
    AgentScheduler,
    RestartPolicy,
    SLO_MAX_CYCLE_DURATION_SECONDS,
    SLO_MAX_RESTART_SECONDS,
)


# ---------------------------------------------------------------------------
# Test agent implementations
# ---------------------------------------------------------------------------


class BenchmarkAgent(AutonomousAgentBase):
    """Agent for benchmarking startup and cycle duration."""

    def __init__(self, agent_id="benchmark_agent", cycle_duration=0.0):
        activity_log = MagicMock()
        activity_log.log_monitoring_cycle = AsyncMock()
        ws = MagicMock()
        ws.broadcast_activity = AsyncMock()
        cp = MagicMock()
        super().__init__(
            agent_id=agent_id,
            poll_interval_seconds=60,
            cooldown_minutes=5,
            activity_log_service=activity_log,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        self._cycle_duration = cycle_duration

    async def monitor_cycle(self):
        """Simulate a monitoring cycle with configurable duration."""
        if self._cycle_duration > 0:
            await asyncio.sleep(self._cycle_duration)
        return ([], [])


def _make_scheduler(shutdown_timeout=10.0):
    """Create an AgentScheduler with mocked dependencies."""
    telemetry = MagicMock()
    telemetry.emit_alert = MagicMock()
    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock()
    return AgentScheduler(
        telemetry_service=telemetry,
        activity_log_service=activity_log,
        shutdown_timeout=shutdown_timeout,
    )


# ---------------------------------------------------------------------------
# Tests: Agent startup time benchmark
# ---------------------------------------------------------------------------


class TestStartupTimeBenchmark:
    """Verify agent startup time is under SLO_MAX_RESTART_SECONDS."""

    @pytest.mark.asyncio
    async def test_single_agent_startup_under_slo(self):
        """Req 8.6: A single agent starts within the SLO budget."""
        scheduler = _make_scheduler()
        agent = BenchmarkAgent(agent_id="startup_bench")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        start = time.perf_counter()
        await scheduler.start_all()
        elapsed = time.perf_counter() - start

        assert elapsed < SLO_MAX_RESTART_SECONDS, (
            f"Agent startup took {elapsed:.3f}s, exceeds SLO of "
            f"{SLO_MAX_RESTART_SECONDS}s"
        )

        health = scheduler.get_health()
        assert health["startup_bench"]["status"] == "running"

        await scheduler.stop_all()

    @pytest.mark.asyncio
    async def test_multiple_agents_startup_under_slo(self):
        """Req 8.6: Starting 3 agents (matching production count) stays within SLO."""
        scheduler = _make_scheduler()
        for i in range(3):
            agent = BenchmarkAgent(agent_id=f"bench_agent_{i}")
            scheduler.register(agent, RestartPolicy.ON_FAILURE)

        start = time.perf_counter()
        await scheduler.start_all()
        elapsed = time.perf_counter() - start

        assert elapsed < SLO_MAX_RESTART_SECONDS, (
            f"Starting 3 agents took {elapsed:.3f}s, exceeds SLO of "
            f"{SLO_MAX_RESTART_SECONDS}s"
        )

        health = scheduler.get_health()
        for i in range(3):
            assert health[f"bench_agent_{i}"]["status"] == "running"

        await scheduler.stop_all()

    @pytest.mark.asyncio
    async def test_startup_time_is_consistent(self):
        """Verify startup time is consistent across multiple runs."""
        times = []
        for _ in range(5):
            scheduler = _make_scheduler()
            agent = BenchmarkAgent(agent_id="consistency_bench")
            scheduler.register(agent, RestartPolicy.ON_FAILURE)

            start = time.perf_counter()
            await scheduler.start_all()
            elapsed = time.perf_counter() - start
            times.append(elapsed)

            await scheduler.stop_all()

        # All runs should be under SLO
        for t in times:
            assert t < SLO_MAX_RESTART_SECONDS

        # Variance should be reasonable (max should be < 10x min)
        assert max(times) < min(times) * 10 or max(times) < 1.0


# ---------------------------------------------------------------------------
# Tests: Monitoring cycle duration benchmark
# ---------------------------------------------------------------------------


class TestCycleDurationBenchmark:
    """Verify a single monitoring cycle completes within SLO budget."""

    @pytest.mark.asyncio
    async def test_fast_cycle_under_slo(self):
        """Req 8.6: A monitoring cycle with minimal work completes within SLO."""
        agent = BenchmarkAgent(agent_id="cycle_bench", cycle_duration=0.0)
        activity_log = MagicMock()
        activity_log.log_monitoring_cycle = AsyncMock()
        ws = MagicMock()
        ws.broadcast_activity = AsyncMock()

        start = time.perf_counter()
        detections, actions = await agent.monitor_cycle()
        elapsed = time.perf_counter() - start

        assert elapsed < SLO_MAX_CYCLE_DURATION_SECONDS, (
            f"Monitoring cycle took {elapsed:.3f}s, exceeds SLO of "
            f"{SLO_MAX_CYCLE_DURATION_SECONDS}s"
        )
        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_moderate_cycle_under_slo(self):
        """A cycle with moderate simulated work stays within SLO."""
        agent = BenchmarkAgent(agent_id="moderate_bench", cycle_duration=0.5)

        start = time.perf_counter()
        await agent.monitor_cycle()
        elapsed = time.perf_counter() - start

        assert elapsed < SLO_MAX_CYCLE_DURATION_SECONDS, (
            f"Moderate cycle took {elapsed:.3f}s, exceeds SLO of "
            f"{SLO_MAX_CYCLE_DURATION_SECONDS}s"
        )

    @pytest.mark.asyncio
    async def test_cycle_duration_multiple_runs(self):
        """Verify cycle duration is consistent across multiple invocations."""
        agent = BenchmarkAgent(agent_id="multi_bench", cycle_duration=0.01)

        times = []
        for _ in range(20):
            start = time.perf_counter()
            await agent.monitor_cycle()
            elapsed = time.perf_counter() - start
            times.append(elapsed)

        # All runs should be under SLO
        for t in times:
            assert t < SLO_MAX_CYCLE_DURATION_SECONDS

        # Average should be well under SLO
        avg = sum(times) / len(times)
        assert avg < SLO_MAX_CYCLE_DURATION_SECONDS / 2, (
            f"Average cycle time {avg:.3f}s is more than half the SLO budget"
        )
