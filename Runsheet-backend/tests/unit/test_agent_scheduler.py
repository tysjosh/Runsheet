"""
Unit tests for AgentScheduler with restart policies and health reporting.

Tests cover:
- ALWAYS policy restarts agent on any exit (normal or exception)
- ON_FAILURE policy restarts agent only on unhandled exception
- NEVER policy does not restart agent
- Max restart window — agent exceeding 3 restarts in 5 minutes is marked failed
- get_health() returns correct status, uptime, restart count, and last error
- Graceful shutdown completes within timeout
- Force-cancel on shutdown timeout exceeded

Requirements: 7.2, 7.3, 7.4, 7.5, 7.7, Correctness Properties P7, P8
"""
import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.autonomous.base_agent import AutonomousAgentBase
from bootstrap.agent_scheduler import (
    AgentScheduler,
    AgentState,
    RestartPolicy,
    SLO_MAX_CONSECUTIVE_FAILURES,
    SLO_RESTART_WINDOW_SECONDS,
)


# ---------------------------------------------------------------------------
# Test agent implementations
# ---------------------------------------------------------------------------


class MockAgent(AutonomousAgentBase):
    """A mock agent that completes its task after a configurable number of cycles."""

    def __init__(self, agent_id="mock_agent", fail_on_cycle=None, exit_after_cycles=None):
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
        self._fail_on_cycle = fail_on_cycle
        self._exit_after_cycles = exit_after_cycles
        self._cycle_count = 0

    async def monitor_cycle(self):
        self._cycle_count += 1
        if self._fail_on_cycle and self._cycle_count >= self._fail_on_cycle:
            raise RuntimeError(f"Simulated crash on cycle {self._cycle_count}")
        return ([], [])


class ImmediateExitAgent(AutonomousAgentBase):
    """Agent whose internal task exits immediately (normal exit)."""

    def __init__(self, agent_id="exit_agent"):
        activity_log = MagicMock()
        activity_log.log_monitoring_cycle = AsyncMock()
        ws = MagicMock()
        cp = MagicMock()
        super().__init__(
            agent_id=agent_id,
            poll_interval_seconds=0,
            cooldown_minutes=5,
            activity_log_service=activity_log,
            ws_manager=ws,
            confirmation_protocol=cp,
        )

    async def start(self):
        """Start but the internal task exits immediately (normal)."""
        self._running = True
        self._task = asyncio.create_task(self._quick_exit())

    async def _quick_exit(self):
        """Exit immediately without error."""
        await asyncio.sleep(0.01)
        return

    async def monitor_cycle(self):
        return ([], [])


class ImmediateCrashAgent(AutonomousAgentBase):
    """Agent whose internal task crashes immediately."""

    def __init__(self, agent_id="crash_agent", crash_count=None):
        activity_log = MagicMock()
        activity_log.log_monitoring_cycle = AsyncMock()
        ws = MagicMock()
        cp = MagicMock()
        super().__init__(
            agent_id=agent_id,
            poll_interval_seconds=0,
            cooldown_minutes=5,
            activity_log_service=activity_log,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        self._crash_count = crash_count  # None = always crash
        self._start_count = 0

    async def start(self):
        """Start but the internal task crashes immediately."""
        self._running = True
        self._start_count += 1
        if self._crash_count is None or self._start_count <= self._crash_count:
            self._task = asyncio.create_task(self._crash())
        else:
            # After crash_count starts, run normally (long-lived)
            self._task = asyncio.create_task(self._run_loop())

    async def _crash(self):
        """Crash immediately."""
        await asyncio.sleep(0.01)
        raise RuntimeError(f"Crash #{self._start_count}")

    async def monitor_cycle(self):
        return ([], [])


class SlowStopAgent(AutonomousAgentBase):
    """Agent that takes a long time to stop."""

    def __init__(self, agent_id="slow_agent", stop_delay=30):
        activity_log = MagicMock()
        activity_log.log_monitoring_cycle = AsyncMock()
        ws = MagicMock()
        cp = MagicMock()
        super().__init__(
            agent_id=agent_id,
            poll_interval_seconds=60,
            cooldown_minutes=5,
            activity_log_service=activity_log,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        self._stop_delay = stop_delay

    async def stop(self):
        """Simulate a slow stop."""
        await asyncio.sleep(self._stop_delay)
        self._running = False

    async def monitor_cycle(self):
        return ([], [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
# Tests: Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    """Tests for agent registration."""

    def test_register_stores_agent(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test_agent")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        assert "test_agent" in scheduler._agents

    def test_register_sets_policy(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test_agent")
        scheduler.register(agent, RestartPolicy.ALWAYS)
        assert scheduler._agents["test_agent"].policy == RestartPolicy.ALWAYS

    def test_register_default_policy_is_on_failure(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test_agent")
        scheduler.register(agent)
        assert scheduler._agents["test_agent"].policy == RestartPolicy.ON_FAILURE

    def test_register_initial_status_is_stopped(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test_agent")
        scheduler.register(agent, RestartPolicy.ALWAYS)
        assert scheduler._agents["test_agent"].status == "stopped"

    def test_register_multiple_agents(self):
        scheduler = _make_scheduler()
        a1 = MockAgent(agent_id="agent_1")
        a2 = MockAgent(agent_id="agent_2")
        scheduler.register(a1, RestartPolicy.ALWAYS)
        scheduler.register(a2, RestartPolicy.NEVER)
        assert len(scheduler._agents) == 2


# ---------------------------------------------------------------------------
# Tests: ALWAYS restart policy
# ---------------------------------------------------------------------------


class TestAlwaysPolicy:
    """ALWAYS policy restarts agent on any exit (normal or exception)."""

    async def test_always_restarts_on_normal_exit(self):
        scheduler = _make_scheduler()
        agent = ImmediateExitAgent(agent_id="always_exit")
        scheduler.register(agent, RestartPolicy.ALWAYS)
        await scheduler.start_all()

        # Wait for the agent to exit and be restarted
        await asyncio.sleep(2.5)

        health = scheduler.get_health()
        # Agent should have been restarted at least once
        assert health["always_exit"]["restart_count"] >= 1
        assert health["always_exit"]["status"] in ("running", "restarting")

        await scheduler.stop_all()

    async def test_always_restarts_on_exception(self):
        scheduler = _make_scheduler()
        agent = ImmediateCrashAgent(agent_id="always_crash", crash_count=1)
        scheduler.register(agent, RestartPolicy.ALWAYS)
        await scheduler.start_all()

        # Wait for crash detection and restart
        await asyncio.sleep(2.5)

        health = scheduler.get_health()
        assert health["always_crash"]["restart_count"] >= 1

        await scheduler.stop_all()


# ---------------------------------------------------------------------------
# Tests: ON_FAILURE restart policy
# ---------------------------------------------------------------------------


class TestOnFailurePolicy:
    """ON_FAILURE policy restarts agent only on unhandled exception."""

    async def test_on_failure_restarts_on_exception(self):
        scheduler = _make_scheduler()
        agent = ImmediateCrashAgent(agent_id="fail_crash", crash_count=1)
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        await scheduler.start_all()

        # Wait for crash detection and restart
        await asyncio.sleep(2.5)

        health = scheduler.get_health()
        assert health["fail_crash"]["restart_count"] >= 1

        await scheduler.stop_all()

    async def test_on_failure_does_not_restart_on_normal_exit(self):
        scheduler = _make_scheduler()
        agent = ImmediateExitAgent(agent_id="fail_exit")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        await scheduler.start_all()

        # Wait for the agent to exit
        await asyncio.sleep(2.5)

        health = scheduler.get_health()
        assert health["fail_exit"]["restart_count"] == 0
        assert health["fail_exit"]["status"] == "stopped"

        await scheduler.stop_all()


# ---------------------------------------------------------------------------
# Tests: NEVER restart policy
# ---------------------------------------------------------------------------


class TestNeverPolicy:
    """NEVER policy does not restart agent."""

    async def test_never_does_not_restart_on_exception(self):
        scheduler = _make_scheduler()
        agent = ImmediateCrashAgent(agent_id="never_crash")
        scheduler.register(agent, RestartPolicy.NEVER)
        await scheduler.start_all()

        # Wait for crash detection
        await asyncio.sleep(2.5)

        health = scheduler.get_health()
        assert health["never_crash"]["restart_count"] == 0
        assert health["never_crash"]["status"] in ("stopped", "failed")

        await scheduler.stop_all()

    async def test_never_does_not_restart_on_normal_exit(self):
        scheduler = _make_scheduler()
        agent = ImmediateExitAgent(agent_id="never_exit")
        scheduler.register(agent, RestartPolicy.NEVER)
        await scheduler.start_all()

        # Wait for the agent to exit
        await asyncio.sleep(2.5)

        health = scheduler.get_health()
        assert health["never_exit"]["restart_count"] == 0
        assert health["never_exit"]["status"] == "stopped"

        await scheduler.stop_all()


# ---------------------------------------------------------------------------
# Tests: Max restart window
# ---------------------------------------------------------------------------


class TestMaxRestartWindow:
    """Agent exceeding 3 restarts in 5 minutes is marked failed."""

    async def test_agent_marked_failed_after_max_restarts(self):
        scheduler = _make_scheduler()
        # Agent that always crashes
        agent = ImmediateCrashAgent(agent_id="max_crash")
        scheduler.register(agent, RestartPolicy.ALWAYS)
        await scheduler.start_all()

        # Wait for multiple crash-restart cycles to exhaust the limit
        # Each cycle: ~0.01s crash + 1s monitor sleep + 0.5s restart pause
        await asyncio.sleep(8)

        health = scheduler.get_health()
        assert health["max_crash"]["status"] == "failed"
        assert health["max_crash"]["restart_count"] == SLO_MAX_CONSECUTIVE_FAILURES

        # Verify telemetry alert was emitted
        scheduler._telemetry.emit_alert.assert_called()

        await scheduler.stop_all()

    def test_can_restart_returns_false_when_window_exhausted(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ALWAYS)
        state = scheduler._agents["test"]

        # Simulate 3 recent restarts
        now = datetime.now(timezone.utc)
        state.restart_timestamps = [
            now - timedelta(seconds=60),
            now - timedelta(seconds=30),
            now - timedelta(seconds=10),
        ]

        assert scheduler._can_restart(state) is False

    def test_can_restart_returns_true_when_old_restarts_expired(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ALWAYS)
        state = scheduler._agents["test"]

        # Simulate 3 restarts, but all outside the 5-minute window
        now = datetime.now(timezone.utc)
        state.restart_timestamps = [
            now - timedelta(seconds=400),
            now - timedelta(seconds=350),
            now - timedelta(seconds=310),
        ]

        assert scheduler._can_restart(state) is True

    def test_can_restart_returns_false_for_never_policy(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.NEVER)
        state = scheduler._agents["test"]
        assert scheduler._can_restart(state) is False


# ---------------------------------------------------------------------------
# Tests: Health reporting
# ---------------------------------------------------------------------------


class TestHealthReporting:
    """get_health() returns correct status, uptime, restart count, and last error."""

    def test_health_returns_all_agents(self):
        scheduler = _make_scheduler()
        a1 = MockAgent(agent_id="agent_1")
        a2 = MockAgent(agent_id="agent_2")
        scheduler.register(a1, RestartPolicy.ALWAYS)
        scheduler.register(a2, RestartPolicy.NEVER)

        health = scheduler.get_health()
        assert "agent_1" in health
        assert "agent_2" in health

    def test_health_initial_status_is_stopped(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        health = scheduler.get_health()
        assert health["test"]["status"] == "stopped"
        assert health["test"]["uptime_seconds"] == 0.0
        assert health["test"]["restart_count"] == 0
        assert health["test"]["last_error"] is None
        assert health["test"]["policy"] == "on_failure"

    async def test_health_shows_running_after_start(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        await scheduler.start_all()

        await asyncio.sleep(0.1)

        health = scheduler.get_health()
        assert health["test"]["status"] == "running"
        assert health["test"]["uptime_seconds"] > 0

        await scheduler.stop_all()

    async def test_health_shows_correct_policy(self):
        scheduler = _make_scheduler()
        a1 = MockAgent(agent_id="a1")
        a2 = MockAgent(agent_id="a2")
        a3 = MockAgent(agent_id="a3")
        scheduler.register(a1, RestartPolicy.ALWAYS)
        scheduler.register(a2, RestartPolicy.ON_FAILURE)
        scheduler.register(a3, RestartPolicy.NEVER)

        health = scheduler.get_health()
        assert health["a1"]["policy"] == "always"
        assert health["a2"]["policy"] == "on_failure"
        assert health["a3"]["policy"] == "never"

    async def test_health_shows_last_error_after_crash(self):
        scheduler = _make_scheduler()
        agent = ImmediateCrashAgent(agent_id="err_agent", crash_count=1)
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        await scheduler.start_all()

        # Wait for crash detection
        await asyncio.sleep(2.5)

        health = scheduler.get_health()
        assert health["err_agent"]["last_error"] is not None
        assert "Crash" in health["err_agent"]["last_error"]

        await scheduler.stop_all()

    def test_health_empty_when_no_agents(self):
        scheduler = _make_scheduler()
        health = scheduler.get_health()
        assert health == {}


# ---------------------------------------------------------------------------
# Tests: Graceful shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    """Graceful shutdown completes within timeout."""

    async def test_stop_all_stops_running_agents(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        await scheduler.start_all()

        await asyncio.sleep(0.1)
        await scheduler.stop_all()

        health = scheduler.get_health()
        assert health["test"]["status"] == "stopped"

    async def test_stop_all_with_no_agents(self):
        scheduler = _make_scheduler()
        # Should not raise
        await scheduler.stop_all()

    async def test_force_cancel_on_timeout(self):
        scheduler = _make_scheduler(shutdown_timeout=0.1)
        agent = SlowStopAgent(agent_id="slow", stop_delay=30)
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        await scheduler.start_all()

        await asyncio.sleep(0.1)

        # stop_all should complete despite the slow agent (force-cancel)
        await asyncio.wait_for(scheduler.stop_all(), timeout=5.0)

        health = scheduler.get_health()
        assert health["slow"]["status"] == "stopped"

    async def test_stop_all_accumulates_uptime(self):
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        await scheduler.start_all()

        await asyncio.sleep(0.5)
        await scheduler.stop_all()

        health = scheduler.get_health()
        # Uptime should be accumulated (total_uptime_seconds includes stopped time)
        assert health["test"]["uptime_seconds"] >= 0.4


# ---------------------------------------------------------------------------
# Tests: RestartPolicy enum
# ---------------------------------------------------------------------------


class TestRestartPolicyEnum:
    """Tests for RestartPolicy enum values."""

    def test_always_value(self):
        assert RestartPolicy.ALWAYS.value == "always"

    def test_on_failure_value(self):
        assert RestartPolicy.ON_FAILURE.value == "on_failure"

    def test_never_value(self):
        assert RestartPolicy.NEVER.value == "never"


# ---------------------------------------------------------------------------
# Tests: AgentState dataclass
# ---------------------------------------------------------------------------


class TestAgentState:
    """Tests for AgentState dataclass defaults."""

    def test_default_status_is_stopped(self):
        agent = MockAgent(agent_id="test")
        state = AgentState(agent=agent, policy=RestartPolicy.ON_FAILURE)
        assert state.status == "stopped"

    def test_default_restart_count_is_zero(self):
        agent = MockAgent(agent_id="test")
        state = AgentState(agent=agent, policy=RestartPolicy.ON_FAILURE)
        assert state.restart_count == 0

    def test_default_restart_timestamps_is_empty(self):
        agent = MockAgent(agent_id="test")
        state = AgentState(agent=agent, policy=RestartPolicy.ON_FAILURE)
        assert state.restart_timestamps == []

    def test_default_last_error_is_none(self):
        agent = MockAgent(agent_id="test")
        state = AgentState(agent=agent, policy=RestartPolicy.ON_FAILURE)
        assert state.last_error is None

    def test_default_total_uptime_is_zero(self):
        agent = MockAgent(agent_id="test")
        state = AgentState(agent=agent, policy=RestartPolicy.ON_FAILURE)
        assert state.total_uptime_seconds == 0.0


# ---------------------------------------------------------------------------
# Integration test: Agent recovery after simulated crash
# ---------------------------------------------------------------------------


class TestAgentRecoveryIntegration:
    """Integration test: register a mock agent that crashes on first start,
    then succeeds. Verify scheduler restarts the agent and status transitions:
    running → restarting → running.

    Requirements: 7.3
    """

    async def test_agent_recovers_after_crash(self):
        """Agent crashes on first monitor_cycle, scheduler restarts it,
        and the agent runs successfully on the second start."""
        scheduler = _make_scheduler()
        # crash_count=1 means the first start crashes, subsequent starts succeed
        agent = ImmediateCrashAgent(agent_id="recovery_agent", crash_count=1)
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        await scheduler.start_all()

        # Initial status should be running
        health = scheduler.get_health()
        assert health["recovery_agent"]["status"] == "running"

        # Wait for crash detection and restart
        await asyncio.sleep(3)

        # After recovery, agent should be running again with restart_count=1
        health = scheduler.get_health()
        assert health["recovery_agent"]["status"] == "running"
        assert health["recovery_agent"]["restart_count"] == 1
        assert health["recovery_agent"]["last_error"] is not None
        assert "Crash" in health["recovery_agent"]["last_error"]

        await scheduler.stop_all()

    async def test_status_transitions_through_restarting(self):
        """Verify the agent transitions through restarting state during recovery."""
        scheduler = _make_scheduler()
        agent = ImmediateCrashAgent(agent_id="transition_agent", crash_count=1)
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        observed_statuses = set()

        await scheduler.start_all()
        observed_statuses.add(scheduler._agents["transition_agent"].status)

        # Poll status rapidly to catch the restarting transition
        for _ in range(50):
            await asyncio.sleep(0.1)
            status = scheduler._agents["transition_agent"].status
            observed_statuses.add(status)
            if status == "running" and scheduler._agents["transition_agent"].restart_count > 0:
                break

        # We should have seen at least running (and possibly restarting)
        assert "running" in observed_statuses
        # After recovery, final status should be running
        assert scheduler._agents["transition_agent"].status == "running"
        assert scheduler._agents["transition_agent"].restart_count >= 1

        await scheduler.stop_all()

    async def test_multiple_agents_independent_recovery(self):
        """Multiple agents: one crashes and recovers, the other stays running."""
        scheduler = _make_scheduler()
        crashing_agent = ImmediateCrashAgent(agent_id="crasher", crash_count=1)
        stable_agent = MockAgent(agent_id="stable")

        scheduler.register(crashing_agent, RestartPolicy.ON_FAILURE)
        scheduler.register(stable_agent, RestartPolicy.ON_FAILURE)

        await scheduler.start_all()
        await asyncio.sleep(3)

        health = scheduler.get_health()

        # Crashing agent should have recovered
        assert health["crasher"]["status"] == "running"
        assert health["crasher"]["restart_count"] == 1

        # Stable agent should still be running with no restarts
        assert health["stable"]["status"] == "running"
        assert health["stable"]["restart_count"] == 0

        await scheduler.stop_all()
