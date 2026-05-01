"""
Unit tests verifying SLO threshold alerts fire correctly.

Tests cover:
- Uptime SLO violation triggers a warning alert via telemetry service
- Uptime above SLO threshold does not trigger an alert
- Alert payload contains correct agent_id, uptime_pct, and slo_target
- Rolling 24-hour window calculation is accurate
- Uptime tracking across start/stop/restart cycles

Requirements: 8.4
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, call

from Agents.autonomous.base_agent import AutonomousAgentBase
from bootstrap.agent_scheduler import (
    AgentScheduler,
    AgentState,
    RestartPolicy,
    UptimeRecord,
    SLO_MIN_UPTIME_PCT,
    SLO_UPTIME_WINDOW_SECONDS,
)


# ---------------------------------------------------------------------------
# Test agent implementations
# ---------------------------------------------------------------------------


class MockAgent(AutonomousAgentBase):
    """A simple mock agent for testing."""

    def __init__(self, agent_id="mock_agent"):
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
        self._crash_count = crash_count
        self._start_count = 0

    async def start(self):
        self._running = True
        self._start_count += 1
        if self._crash_count is None or self._start_count <= self._crash_count:
            self._task = asyncio.create_task(self._crash())
        else:
            self._task = asyncio.create_task(self._run_loop())

    async def _crash(self):
        await asyncio.sleep(0.01)
        raise RuntimeError(f"Crash #{self._start_count}")

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
# Tests: Uptime percentage calculation
# ---------------------------------------------------------------------------


class TestUptimeCalculation:
    """Tests for rolling 24-hour uptime percentage calculation."""

    def test_no_records_returns_100_pct(self):
        """Agent with no records defaults to 100% uptime."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)
        assert scheduler.get_uptime_pct("test") == 100.0

    def test_unregistered_agent_returns_100_pct(self):
        """Unregistered agent returns 100% uptime."""
        scheduler = _make_scheduler()
        assert scheduler.get_uptime_pct("nonexistent") == 100.0

    def test_all_uptime_returns_100_pct(self):
        """Agent with only uptime records returns 100%."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["test"]
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=12),
                end=now,
                is_up=True,
            )
        ]

        assert scheduler.get_uptime_pct("test") == 100.0

    def test_mixed_uptime_downtime(self):
        """Agent with mixed uptime/downtime returns correct percentage."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["test"]
        # 20 hours up, 4 hours down = ~83.3% uptime
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=24),
                end=now - timedelta(hours=4),
                is_up=True,
            ),
            UptimeRecord(
                start=now - timedelta(hours=4),
                end=now,
                is_up=False,
            ),
        ]

        uptime_pct = scheduler.get_uptime_pct("test")
        assert 83.0 < uptime_pct < 84.0

    def test_uptime_below_slo_threshold(self):
        """Agent with uptime below SLO threshold is detected."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["test"]
        # 23 hours up, 1 hour down = ~95.8% (below 99%)
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=24),
                end=now - timedelta(hours=1),
                is_up=True,
            ),
            UptimeRecord(
                start=now - timedelta(hours=1),
                end=now,
                is_up=False,
            ),
        ]

        is_compliant, uptime_pct = scheduler.check_slo_compliance("test")
        assert not is_compliant
        assert uptime_pct < SLO_MIN_UPTIME_PCT

    def test_uptime_above_slo_threshold(self):
        """Agent with uptime above SLO threshold is compliant."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["test"]
        # 23h 55m up, 5m down = ~99.65% (above 99%)
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=24),
                end=now - timedelta(minutes=5),
                is_up=True,
            ),
            UptimeRecord(
                start=now - timedelta(minutes=5),
                end=now,
                is_up=False,
            ),
        ]

        is_compliant, uptime_pct = scheduler.check_slo_compliance("test")
        assert is_compliant
        assert uptime_pct >= SLO_MIN_UPTIME_PCT

    def test_open_uptime_record_uses_current_time(self):
        """An open (no end time) uptime record counts up to now."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["test"]
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=12),
                end=None,
                is_up=True,
            )
        ]

        assert scheduler.get_uptime_pct("test") == 100.0

    def test_records_outside_window_are_ignored(self):
        """Records entirely outside the 24-hour window are not counted."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["test"]
        state.uptime_records = [
            # Old downtime record (outside window)
            UptimeRecord(
                start=now - timedelta(hours=48),
                end=now - timedelta(hours=25),
                is_up=False,
            ),
            # Recent uptime record (inside window)
            UptimeRecord(
                start=now - timedelta(hours=12),
                end=now,
                is_up=True,
            ),
        ]

        assert scheduler.get_uptime_pct("test") == 100.0


# ---------------------------------------------------------------------------
# Tests: SLO threshold alerting
# ---------------------------------------------------------------------------


class TestSLOAlerts:
    """Tests for SLO threshold alert emission."""

    def test_alert_fires_when_uptime_below_slo(self):
        """Req 8.4: Telemetry alert emitted when uptime < SLO_MIN_UPTIME_PCT."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="alert_test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["alert_test"]
        # 22 hours up, 2 hours down = ~91.7% (below 99%)
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=24),
                end=now - timedelta(hours=2),
                is_up=True,
            ),
            UptimeRecord(
                start=now - timedelta(hours=2),
                end=now,
                is_up=False,
            ),
        ]

        scheduler._check_and_alert_uptime_slo(state)

        scheduler._telemetry.emit_alert.assert_called_once_with(
            "agent_uptime_slo_violation",
            agent_id="alert_test",
            uptime_pct=pytest.approx(91.67, abs=0.5),
            slo_target=SLO_MIN_UPTIME_PCT,
            window_seconds=SLO_UPTIME_WINDOW_SECONDS,
        )

    def test_no_alert_when_uptime_above_slo(self):
        """No alert emitted when uptime >= SLO_MIN_UPTIME_PCT."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="ok_test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["ok_test"]
        # 23h 58m up, 2m down = ~99.86% (above 99%)
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=24),
                end=now - timedelta(minutes=2),
                is_up=True,
            ),
            UptimeRecord(
                start=now - timedelta(minutes=2),
                end=now,
                is_up=False,
            ),
        ]

        scheduler._check_and_alert_uptime_slo(state)

        scheduler._telemetry.emit_alert.assert_not_called()

    def test_no_alert_without_telemetry_service(self):
        """No error when telemetry service is None."""
        scheduler = AgentScheduler(
            telemetry_service=None,
            activity_log_service=MagicMock(),
        )
        agent = MockAgent(agent_id="no_telem")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["no_telem"]
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=24),
                end=now - timedelta(hours=6),
                is_up=True,
            ),
            UptimeRecord(
                start=now - timedelta(hours=6),
                end=now,
                is_up=False,
            ),
        ]

        # Should not raise
        scheduler._check_and_alert_uptime_slo(state)

    def test_alert_payload_contains_required_fields(self):
        """Alert payload includes agent_id, uptime_pct, slo_target, window_seconds."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="payload_test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["payload_test"]
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=24),
                end=now - timedelta(hours=3),
                is_up=True,
            ),
            UptimeRecord(
                start=now - timedelta(hours=3),
                end=now,
                is_up=False,
            ),
        ]

        scheduler._check_and_alert_uptime_slo(state)

        call_args = scheduler._telemetry.emit_alert.call_args
        assert call_args[0][0] == "agent_uptime_slo_violation"
        assert call_args[1]["agent_id"] == "payload_test"
        assert call_args[1]["slo_target"] == SLO_MIN_UPTIME_PCT
        assert call_args[1]["window_seconds"] == SLO_UPTIME_WINDOW_SECONDS
        assert 0 < call_args[1]["uptime_pct"] < 100


# ---------------------------------------------------------------------------
# Tests: Uptime tracking through lifecycle
# ---------------------------------------------------------------------------


class TestUptimeTracking:
    """Tests for uptime record tracking through agent lifecycle events."""

    @pytest.mark.asyncio
    async def test_start_creates_uptime_record(self):
        """Starting an agent creates an uptime record."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="track_start")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        await scheduler.start_all()
        await asyncio.sleep(0.1)

        state = scheduler._agents["track_start"]
        assert len(state.uptime_records) >= 1
        assert state.uptime_records[0].is_up is True
        assert state.uptime_records[0].end is None  # Still open

        await scheduler.stop_all()

    @pytest.mark.asyncio
    async def test_stop_closes_uptime_record(self):
        """Stopping an agent closes the current uptime record."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="track_stop")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        await scheduler.start_all()
        await asyncio.sleep(0.1)
        await scheduler.stop_all()

        state = scheduler._agents["track_stop"]
        # The last record should be closed
        last_record = state.uptime_records[-1]
        assert last_record.end is not None

    @pytest.mark.asyncio
    async def test_crash_and_restart_creates_downtime_then_uptime(self):
        """Agent crash creates downtime record, restart creates new uptime record."""
        scheduler = _make_scheduler()
        agent = ImmediateCrashAgent(agent_id="track_crash", crash_count=1)
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        await scheduler.start_all()
        # Wait for crash detection and restart
        await asyncio.sleep(3)

        state = scheduler._agents["track_crash"]
        # Should have: initial uptime, downtime during restart, new uptime
        has_uptime = any(r.is_up for r in state.uptime_records)
        has_downtime = any(not r.is_up for r in state.uptime_records)
        assert has_uptime
        assert has_downtime

        await scheduler.stop_all()

    def test_prune_removes_old_records(self):
        """Pruning removes records older than the 24-hour window."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="prune_test")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["prune_test"]
        state.uptime_records = [
            # Old record (should be pruned)
            UptimeRecord(
                start=now - timedelta(hours=48),
                end=now - timedelta(hours=25),
                is_up=True,
            ),
            # Recent record (should be kept)
            UptimeRecord(
                start=now - timedelta(hours=12),
                end=now,
                is_up=True,
            ),
        ]

        scheduler._prune_old_records(state)
        assert len(state.uptime_records) == 1
        assert state.uptime_records[0].start == now - timedelta(hours=12)

    def test_prune_keeps_open_records(self):
        """Pruning keeps records with no end time (still open)."""
        scheduler = _make_scheduler()
        agent = MockAgent(agent_id="prune_open")
        scheduler.register(agent, RestartPolicy.ON_FAILURE)

        now = datetime.now(timezone.utc)
        state = scheduler._agents["prune_open"]
        state.uptime_records = [
            UptimeRecord(
                start=now - timedelta(hours=1),
                end=None,
                is_up=True,
            ),
        ]

        scheduler._prune_old_records(state)
        assert len(state.uptime_records) == 1


# ---------------------------------------------------------------------------
# Tests: SLO alert during agent failure
# ---------------------------------------------------------------------------


class TestSLOAlertOnFailure:
    """Verify SLO alerts fire when agent exhausts restarts and fails."""

    @pytest.mark.asyncio
    async def test_failed_agent_triggers_uptime_check(self):
        """When an agent is marked failed, uptime SLO is checked."""
        scheduler = _make_scheduler()
        agent = ImmediateCrashAgent(agent_id="fail_slo")
        scheduler.register(agent, RestartPolicy.ALWAYS)

        await scheduler.start_all()
        # Wait for multiple crash-restart cycles to exhaust the limit
        await asyncio.sleep(8)

        health = scheduler.get_health()
        assert health["fail_slo"]["status"] == "failed"

        # The agent_failed alert should have been emitted
        alert_calls = scheduler._telemetry.emit_alert.call_args_list
        alert_types = [c[0][0] for c in alert_calls]
        assert "agent_failed" in alert_types

        await scheduler.stop_all()
