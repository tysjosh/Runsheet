"""
Unit tests for the Autonomous Agent Base Class.

Tests the AutonomousAgentBase ABC including start, stop, _run_loop,
_is_on_cooldown, _set_cooldown, monitor_cycle (abstract), and status
property.

Requirements: 3.1, 3.6, 3.7, 4.1, 4.4, 4.6, 5.1, 5.7
"""
import asyncio
import contextlib
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


# ---------------------------------------------------------------------------
# Tests: missing activity log and per-tenant cycle logging (F9)
# ---------------------------------------------------------------------------


class _ScriptedAgent(AutonomousAgentBase):
    """Runs a fixed list of cycles, then stops its own loop."""

    def __init__(self, cycles, **kwargs):
        kwargs.setdefault("agent_id", "scripted_agent")
        kwargs.setdefault("poll_interval_seconds", 0)
        kwargs.setdefault("cooldown_minutes", 0)
        kwargs.setdefault("ws_manager", None)
        kwargs.setdefault("confirmation_protocol", None)
        super().__init__(**kwargs)
        self._cycles = list(cycles)

    async def monitor_cycle(self):
        cycle = self._cycles.pop(0)
        if not self._cycles:
            self._running = False
        return cycle(self) if callable(cycle) else cycle


async def _run(agent):
    """Drive ``_run_loop`` to completion as the sweep leader."""
    agent._running = True
    with patch("persistence.leader_election.is_sweep_leader", return_value=True):
        await agent._run_loop()


def _spy_log():
    log = MagicMock()
    log.log_monitoring_cycle = AsyncMock()
    return log


def _cycle_calls(log):
    """``(tenant_id, detections, actions)`` per logged cycle, None first."""
    return sorted(
        (
            (c.kwargs["tenant_id"], c.args[1], c.args[2])
            for c in log.log_monitoring_cycle.call_args_list
        ),
        key=lambda c: (c[0] is not None, c[0] or ""),
    )


class TestMissingActivityLog:
    """Staging F9: crons built before the activity log raised every cycle."""

    async def test_none_activity_log_warns_once_and_never_errors(self, caplog):
        agent = _ScriptedAgent(
            [(["d1"], []), (["d2"], [])], activity_log_service=None
        )
        with caplog.at_level("WARNING", logger="agent.scripted_agent"):
            await _run(agent)

        messages = [r.getMessage() for r in caplog.records]
        assert not any("Monitor cycle error" in m for m in messages), messages
        warnings = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "no activity log" in r.getMessage()
        ]
        assert len(warnings) == 1

    async def test_set_activity_log_service_late_binds(self):
        agent = _ScriptedAgent([(["d1"], [])], activity_log_service=None)
        log = _spy_log()

        agent.set_activity_log_service(log)
        await _run(agent)

        log.log_monitoring_cycle.assert_awaited_once()


class TestPerTenantCycleLogging:
    """Staging F9: every monitoring_cycle entry carried tenant_id None."""

    async def test_noted_tenants_get_one_entry_each(self):
        def cycle(agent):
            agent._note_tenant_activity("t1", detections=2)
            agent._note_tenant_activity("t2", detections=1, actions=1)
            return (["a", "b", "c"], ["x"])

        log = _spy_log()
        await _run(_ScriptedAgent([cycle], activity_log_service=log))

        assert _cycle_calls(log) == [("t1", 2, 0), ("t2", 1, 1)]

    async def test_noted_tenant_activity_resets_between_cycles(self):
        def first(agent):
            agent._note_tenant_activity("t1", detections=1)
            return (["a"], [])

        def second(agent):
            agent._note_tenant_activity("t2", detections=1)
            return (["b"], [])

        log = _spy_log()
        await _run(_ScriptedAgent([first, second], activity_log_service=log))

        assert _cycle_calls(log) == [("t1", 1, 0), ("t2", 1, 0)]

    async def test_unnoted_results_group_by_item_tenant(self):
        class _Alert:
            def __init__(self, tenant_id):
                self.tenant_id = tenant_id

        detections = [{"tenant_id": "t1"}, _Alert("t2"), "bare-id"]
        actions = [{"tenant_id": "t1"}]
        log = _spy_log()
        await _run(_ScriptedAgent([(detections, actions)], activity_log_service=log))

        assert _cycle_calls(log) == [(None, 1, 0), ("t1", 1, 1), ("t2", 1, 0)]

    async def test_idle_cycle_writes_nothing(self):
        log = _spy_log()
        await _run(_ScriptedAgent([([], [])], activity_log_service=log))

        log.log_monitoring_cycle.assert_not_called()

    async def test_overlay_agent_cycle_with_detections_is_logged(self):
        """Staging 2026-10-07: ``exception_commander`` raised ``TypeError:
        OverlayAgentBase._group_by_tenant() takes 2 positional arguments but 3
        were given`` from ``_log_cycle``, so its monitoring_cycle entry was lost.
        The overlay base's own ``_group_by_tenant(signals)`` shadowed the
        autonomous base's two-argument helper."""
        from Agents.overlay.base_overlay_agent import OverlayAgentBase

        class _Alert:
            def __init__(self, tenant_id):
                self.tenant_id = tenant_id

        class _Overlay(OverlayAgentBase):
            async def evaluate(self, signals):
                return []

            async def monitor_cycle(self):
                self._running = False
                return ([_Alert("t1"), _Alert("t1")], [{"tenant_id": "t1"}])

        log = _spy_log()
        agent = _Overlay(
            agent_id="overlay_stub", signal_bus=MagicMock(), subscriptions=[],
            activity_log_service=log, ws_manager=None, confirmation_protocol=None,
            autonomy_config_service=None, feature_flag_service=None, es_service=None,
            poll_interval=0,
        )
        with patch.object(agent.logger, "exception") as logged_error:
            await _run(agent)

        logged_error.assert_not_called()
        assert _cycle_calls(log) == [("t1", 2, 1)]


class TestFollowerWaitsForLeadership:
    """Staging 2026-10-07: the daily compliance crons never ran.

    A process that wasn't leader on its first loop slept ``poll_interval``
    (24 h) before checking again. Leadership arrives a minute or two after
    boot and staging redeploys more often than daily, so the sweep never ran.
    """

    async def test_follower_cycles_soon_after_taking_over(self):
        from persistence import leader_election

        class _Leader:
            is_leader = False

        leader = _Leader()
        ran = asyncio.Event()

        def cycle(agent):
            ran.set()
            return ([], [])

        agent = _ScriptedAgent([cycle], poll_interval_seconds=86_400,
                               activity_log_service=_spy_log())
        agent._running = True
        original = leader_election.get_sweep_leader()
        leader_election.set_sweep_leader(leader)
        try:
            with patch.object(leader_election, "FOLLOWER_RECHECK_SECONDS", 0.01):
                task = asyncio.create_task(agent._run_loop())
                await asyncio.sleep(0.05)
                assert not ran.is_set(), "a follower ran the cycle"
                leader.is_leader = True  # election moved leadership here
                await asyncio.wait_for(ran.wait(), timeout=1.0)
            task.cancel()
            # The scripted agent stops itself after its last cycle, so the
            # loop may already have returned.
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            leader_election.set_sweep_leader(original)


class _CountingAgent(AutonomousAgentBase):
    """Counts cycles and keeps running until stopped."""

    def __init__(self, agent_id="daily_cron", poll_interval_seconds=86_400):
        super().__init__(
            agent_id=agent_id,
            poll_interval_seconds=poll_interval_seconds,
            cooldown_minutes=0,
            activity_log_service=_spy_log(),
            ws_manager=None,
            confirmation_protocol=None,
        )
        self.runs = 0

    async def monitor_cycle(self):
        self.runs += 1
        return ([], [])


class _SwitchLeader:
    def __init__(self, is_leader=False):
        self.is_leader = is_leader


class TestDailyAgentSchedule:
    """CRON-1: a daily agent runs promptly after it becomes leader, but never
    twice in one interval across leader changes, and keeps its interval."""

    @pytest.fixture(autouse=True)
    def _shared_ledger(self):
        from persistence import leader_election
        from persistence.periodic_runs import InMemoryRunLedger, set_run_ledger

        self.ledger = InMemoryRunLedger()  # stands in for the shared PG store
        set_run_ledger(self.ledger)
        self.clock = [datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)]
        original = leader_election.get_sweep_leader()
        with patch.object(leader_election, "LEADERSHIP_RETRY_SECONDS", 0.01), \
             patch.object(leader_election, "FOLLOWER_RECHECK_SECONDS", 0.005), \
             patch.object(leader_election, "_utcnow", lambda: self.clock[0]):
            yield
        leader_election.set_sweep_leader(original)
        set_run_ledger(None)

    @staticmethod
    async def _stop(*tasks):
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_non_leader_runs_within_the_retry_window_after_takeover(self):
        from persistence import leader_election

        leader = _SwitchLeader(False)
        leader_election.set_sweep_leader(leader)
        agent = _CountingAgent()
        agent._running = True
        task = asyncio.create_task(agent._run_loop())
        await asyncio.sleep(0.05)
        assert agent.runs == 0, "a follower ran the cycle"
        leader.is_leader = True
        await asyncio.sleep(0.05)
        assert agent.runs == 1
        assert "agent:daily_cron" in self.ledger.runs
        await self._stop(task)

    async def test_follower_rechecks_leadership_on_the_short_retry(self):
        """The follower's wait is capped at the retry interval, not 24 h."""
        from persistence import leader_election

        leader_election.set_sweep_leader(_SwitchLeader(False))
        timeouts = []

        async def _wait(timeout):
            timeouts.append(timeout)
            await asyncio.sleep(0.001)
            return False

        agent = _CountingAgent()
        agent._running = True
        with patch.object(leader_election, "wait_for_leadership", _wait):
            task = asyncio.create_task(agent._run_loop())
            await asyncio.sleep(0.02)
            await self._stop(task)
        assert timeouts and max(timeouts) <= leader_election.LEADERSHIP_RETRY_SECONDS

    async def test_new_leader_does_not_rerun_inside_the_interval(self):
        """Old task ran at 12:00 and was replaced at 14:00 (a deploy). The new
        leader waits until 12:00 the next day, then runs once."""
        from persistence import leader_election

        leader_election.set_sweep_leader(_SwitchLeader(True))
        old = _CountingAgent()
        old._running = True
        old_task = asyncio.create_task(old._run_loop())
        await asyncio.sleep(0.03)
        assert old.runs == 1
        await self._stop(old_task)  # old task drains, lock moves

        self.clock[0] += timedelta(hours=2)
        new = _CountingAgent()
        new._running = True
        new_task = asyncio.create_task(new._run_loop())
        await asyncio.sleep(0.05)
        assert new.runs == 0, "the new leader re-ran a daily job after 2 h"

        self.clock[0] += timedelta(hours=22)
        await asyncio.sleep(0.05)
        assert new.runs == 1
        await self._stop(new_task)

    async def test_two_tasks_with_moving_leadership_run_once_per_interval(self):
        from persistence import leader_election

        leader = _SwitchLeader(True)
        leader_election.set_sweep_leader(leader)
        a, b = _CountingAgent(), _CountingAgent()
        a._running = b._running = True
        task_a = asyncio.create_task(a._run_loop())
        await asyncio.sleep(0.03)
        assert a.runs == 1
        # Both tasks now see leadership flip back and forth (a.k.a. two ECS
        # tasks across a failover); neither may run again inside the interval.
        task_b = asyncio.create_task(b._run_loop())
        for _ in range(4):
            leader.is_leader = not leader.is_leader
            await asyncio.sleep(0.02)
        leader.is_leader = True
        await asyncio.sleep(0.03)
        assert a.runs + b.runs == 1
        await self._stop(task_a, task_b)

    async def test_poll_interval_is_respected_after_the_first_run(self):
        from persistence import leader_election

        leader_election.set_sweep_leader(_SwitchLeader(True))
        agent = _CountingAgent(poll_interval_seconds=3600)
        agent._running = True
        task = asyncio.create_task(agent._run_loop())
        await asyncio.sleep(0.03)
        assert agent.runs == 1
        self.clock[0] += timedelta(seconds=3599)
        await asyncio.sleep(0.05)
        assert agent.runs == 1
        self.clock[0] += timedelta(seconds=1)
        await asyncio.sleep(0.05)
        assert agent.runs == 2
        await self._stop(task)

    async def test_without_a_ledger_the_process_keeps_its_own_interval(self):
        """Single-process dev stack: no Postgres, so no ledger."""
        from persistence import leader_election
        from persistence.periodic_runs import set_run_ledger

        set_run_ledger(None)
        leader_election.set_sweep_leader(_SwitchLeader(True))
        agent = _CountingAgent(poll_interval_seconds=3600)
        agent._running = True
        with patch("persistence.database.is_persistence_enabled", return_value=False):
            task = asyncio.create_task(agent._run_loop())
            await asyncio.sleep(0.05)
            assert agent.runs == 1
            self.clock[0] += timedelta(seconds=3600)
            await asyncio.sleep(0.05)
            assert agent.runs == 2
            await self._stop(task)

    async def test_short_interval_agents_do_not_use_the_ledger(self):
        from persistence import leader_election

        leader_election.set_sweep_leader(_SwitchLeader(True))
        agent = _CountingAgent(poll_interval_seconds=0)
        agent._running = True
        task = asyncio.create_task(agent._run_loop())
        await asyncio.sleep(0.02)
        await self._stop(task)
        assert agent.runs > 1
        assert self.ledger.runs == {}
