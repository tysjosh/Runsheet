"""
Unit tests for the Autonomous Agent Base Class.

Tests the AutonomousAgentBase ABC including start, stop, _run_loop,
_is_on_cooldown, _set_cooldown, monitor_cycle (abstract), and status
property.

Requirements: 3.1, 3.6, 3.7, 4.1, 4.4, 4.6, 5.1, 5.7
"""
import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.autonomous.base_agent import AutonomousAgentBase


# ---------------------------------------------------------------------------
# Concrete test implementation
# ---------------------------------------------------------------------------


class StubAgent(AutonomousAgentBase):
    """Concrete stub for testing the abstract base class."""

    def __init__(self, *args, cycle_return=None, cycle_side_effect=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._cycle_return = cycle_return or ([], [])
        self._cycle_side_effect = cycle_side_effect
        self.cycle_call_count = 0

    async def monitor_cycle(self):
        self.cycle_call_count += 1
        if self._cycle_side_effect:
            raise self._cycle_side_effect
        return self._cycle_return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps():
    """Create mocked dependencies for the agent."""
    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id-1")

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()

    feature_flag_service = MagicMock()

    return activity_log, ws_manager, confirmation_protocol, feature_flag_service


def _make_agent(
    agent_id="test_agent",
    poll_interval=1,
    cooldown_minutes=15,
    cycle_return=None,
    cycle_side_effect=None,
):
    """Create a StubAgent with mocked dependencies."""
    activity_log, ws_manager, cp, ffs = _make_deps()
    agent = StubAgent(
        agent_id=agent_id,
        poll_interval_seconds=poll_interval,
        cooldown_minutes=cooldown_minutes,
        activity_log_service=activity_log,
        ws_manager=ws_manager,
        confirmation_protocol=cp,
        feature_flag_service=ffs,
        cycle_return=cycle_return,
        cycle_side_effect=cycle_side_effect,
    )
    return agent


# ---------------------------------------------------------------------------
# Tests: __init__
# ---------------------------------------------------------------------------


class TestInit:
    """Tests for agent initialisation."""

    def test_stores_agent_id(self):
        agent = _make_agent(agent_id="delay_response_agent")
        assert agent.agent_id == "delay_response_agent"

    def test_stores_poll_interval(self):
        agent = _make_agent(poll_interval=60)
        assert agent.poll_interval == 60

    def test_stores_cooldown_minutes(self):
        agent = _make_agent(cooldown_minutes=30)
        assert agent.cooldown_minutes == 30

    def test_initial_state_is_stopped(self):
        agent = _make_agent()
        assert agent._running is False
        assert agent._task is None

    def test_cooldown_tracker_starts_empty(self):
        agent = _make_agent()
        assert agent._cooldown_tracker == {}

    def test_stores_dependencies(self):
        agent = _make_agent()
        assert agent._activity_log is not None
        assert agent._ws is not None
        assert agent._confirmation_protocol is not None
        assert agent._feature_flags is not None

    def test_feature_flag_service_optional(self):
        activity_log, ws_manager, cp, _ = _make_deps()
        agent = StubAgent(
            agent_id="test",
            poll_interval_seconds=10,
            cooldown_minutes=5,
            activity_log_service=activity_log,
            ws_manager=ws_manager,
            confirmation_protocol=cp,
            feature_flag_service=None,
        )
        assert agent._feature_flags is None

    def test_logger_uses_agent_id(self):
        agent = _make_agent(agent_id="fuel_management_agent")
        assert agent.logger.name == "agent.fuel_management_agent"


# ---------------------------------------------------------------------------
# Tests: start / stop
# ---------------------------------------------------------------------------


class TestStartStop:
    """Tests for agent lifecycle management."""

    async def test_start_sets_running_true(self):
        agent = _make_agent()
        await agent.start()
        assert agent._running is True
        await agent.stop()

    async def test_start_creates_asyncio_task(self):
        agent = _make_agent()
        await agent.start()
        assert agent._task is not None
        assert isinstance(agent._task, asyncio.Task)
        await agent.stop()

    async def test_stop_sets_running_false(self):
        agent = _make_agent()
        await agent.start()
        await agent.stop()
        assert agent._running is False

    async def test_stop_cancels_task(self):
        agent = _make_agent()
        await agent.start()
        task = agent._task
        await agent.stop()
        assert task.cancelled() or task.done()

    async def test_stop_without_start_is_safe(self):
        agent = _make_agent()
        # Should not raise
        await agent.stop()
        assert agent._running is False


# ---------------------------------------------------------------------------
# Tests: _run_loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    """Tests for the polling loop."""

    async def test_run_loop_calls_monitor_cycle(self):
        agent = _make_agent(poll_interval=0)
        await agent.start()
        # Give the loop time to execute at least once
        await asyncio.sleep(0.05)
        await agent.stop()
        assert agent.cycle_call_count >= 1

    async def test_run_loop_logs_monitoring_cycle(self):
        detections = ["d1", "d2"]
        actions = ["a1"]
        agent = _make_agent(poll_interval=0, cycle_return=(detections, actions))
        await agent.start()
        await asyncio.sleep(0.05)
        await agent.stop()

        agent._activity_log.log_monitoring_cycle.assert_called()
        call_args = agent._activity_log.log_monitoring_cycle.call_args
        assert call_args[0][0] == "test_agent"  # agent_id
        assert call_args[0][1] == 2  # detection_count
        assert call_args[0][2] == 1  # action_count
        assert isinstance(call_args[0][3], float)  # duration_ms

    async def test_run_loop_survives_monitor_cycle_exception(self):
        agent = _make_agent(
            poll_interval=0,
            cycle_side_effect=RuntimeError("ES connection lost"),
        )
        await agent.start()
        # Give the loop time to execute multiple cycles despite errors
        await asyncio.sleep(0.05)
        await agent.stop()
        # The loop should have continued running despite the error
        assert agent.cycle_call_count >= 1

    async def test_run_loop_does_not_log_on_exception(self):
        """When monitor_cycle raises, the log call should be skipped."""
        agent = _make_agent(
            poll_interval=0,
            cycle_side_effect=RuntimeError("boom"),
        )
        await agent.start()
        await asyncio.sleep(0.05)
        await agent.stop()
        # log_monitoring_cycle should NOT have been called since monitor_cycle raised
        agent._activity_log.log_monitoring_cycle.assert_not_called()

    async def test_run_loop_stops_when_running_is_false(self):
        agent = _make_agent(poll_interval=0)
        await agent.start()
        await asyncio.sleep(0.05)
        count_before = agent.cycle_call_count
        await agent.stop()
        await asyncio.sleep(0.05)
        # After stop, cycle count should not increase significantly
        assert agent.cycle_call_count <= count_before + 1


# ---------------------------------------------------------------------------
# Tests: _is_on_cooldown / _set_cooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    """Tests for cooldown tracking."""

    def test_not_on_cooldown_when_never_set(self):
        agent = _make_agent(cooldown_minutes=15)
        assert agent._is_on_cooldown("entity-1") is False

    def test_on_cooldown_after_set(self):
        agent = _make_agent(cooldown_minutes=15)
        agent._set_cooldown("entity-1")
        assert agent._is_on_cooldown("entity-1") is True

    def test_not_on_cooldown_after_expiry(self):
        agent = _make_agent(cooldown_minutes=15)
        # Manually set a cooldown time in the past
        agent._cooldown_tracker["entity-1"] = datetime.now(timezone.utc) - timedelta(
            minutes=20
        )
        assert agent._is_on_cooldown("entity-1") is False

    def test_on_cooldown_just_before_expiry(self):
        agent = _make_agent(cooldown_minutes=15)
        # Set cooldown 14 minutes ago (still within 15 min window)
        agent._cooldown_tracker["entity-1"] = datetime.now(timezone.utc) - timedelta(
            minutes=14
        )
        assert agent._is_on_cooldown("entity-1") is True

    def test_cooldown_is_per_entity(self):
        agent = _make_agent(cooldown_minutes=15)
        agent._set_cooldown("entity-1")
        assert agent._is_on_cooldown("entity-1") is True
        assert agent._is_on_cooldown("entity-2") is False

    def test_set_cooldown_updates_existing(self):
        agent = _make_agent(cooldown_minutes=15)
        # Set cooldown in the past
        agent._cooldown_tracker["entity-1"] = datetime.now(timezone.utc) - timedelta(
            minutes=20
        )
        assert agent._is_on_cooldown("entity-1") is False
        # Re-set cooldown
        agent._set_cooldown("entity-1")
        assert agent._is_on_cooldown("entity-1") is True

    def test_cooldown_zero_minutes_always_expired(self):
        agent = _make_agent(cooldown_minutes=0)
        agent._set_cooldown("entity-1")
        # With 0 cooldown, should not be on cooldown
        assert agent._is_on_cooldown("entity-1") is False


# ---------------------------------------------------------------------------
# Tests: status property
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for the status property."""

    def test_status_stopped_initially(self):
        agent = _make_agent()
        assert agent.status == "stopped"

    async def test_status_running_after_start(self):
        agent = _make_agent(poll_interval=10)
        await agent.start()
        # Give the task a moment to start
        await asyncio.sleep(0.01)
        assert agent.status == "running"
        await agent.stop()

    async def test_status_stopped_after_stop(self):
        agent = _make_agent(poll_interval=10)
        await agent.start()
        await agent.stop()
        assert agent.status == "stopped"

    async def test_status_error_when_task_has_exception(self):
        """If the task finishes with an unhandled exception, status is error."""
        agent = _make_agent()
        # Create a task that raises immediately
        async def _failing():
            raise RuntimeError("fatal error")

        agent._running = True
        agent._task = asyncio.create_task(_failing())
        # Wait for the task to finish
        await asyncio.sleep(0.01)
        assert agent.status == "error"

    def test_status_stopped_when_task_is_none(self):
        agent = _make_agent()
        agent._running = False
        agent._task = None
        assert agent.status == "stopped"

    def test_status_stopped_when_not_running_and_no_task(self):
        agent = _make_agent()
        assert agent.status == "stopped"


# ---------------------------------------------------------------------------
# Tests: abstract method enforcement
# ---------------------------------------------------------------------------


class TestAbstractMethod:
    """Tests that monitor_cycle must be implemented."""

    def test_cannot_instantiate_without_monitor_cycle(self):
        with pytest.raises(TypeError):
            # AutonomousAgentBase is abstract and cannot be instantiated
            activity_log, ws_manager, cp, ffs = _make_deps()
            AutonomousAgentBase(
                agent_id="test",
                poll_interval_seconds=10,
                cooldown_minutes=5,
                activity_log_service=activity_log,
                ws_manager=ws_manager,
                confirmation_protocol=cp,
            )
