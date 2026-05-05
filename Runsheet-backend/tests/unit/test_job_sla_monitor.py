"""
Unit tests for JobSLAMonitor autonomous agent.

Tests constructor defaults, RiskSignal publishing, WebSocket broadcasting,
cooldown handling, tenant feature flag filtering, severity derivation for
breach vs approaching, and RiskSignal context fields.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.autonomous.job_sla_monitor import JobSLAMonitor
from Agents.overlay.data_contracts import RiskSignal, Severity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(feature_flags=False):
    """Create mocked dependencies for the agent."""
    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id-1")

    ws_manager = MagicMock()
    ws_manager.broadcast_event = AsyncMock()

    confirmation_protocol = MagicMock()

    feature_flag_service = None
    if feature_flags:
        feature_flag_service = MagicMock()
        feature_flag_service.is_enabled = AsyncMock(return_value=True)

    return es_service, activity_log, ws_manager, confirmation_protocol, feature_flag_service


def _make_agent(signal_bus=None, feature_flags=False, **kwargs):
    """Create a JobSLAMonitor with mocked dependencies and optional signal_bus."""
    es, al, ws, cp, ffs = _make_deps(feature_flags=feature_flags)
    agent = JobSLAMonitor(
        es_service=es,
        activity_log_service=al,
        ws_manager=ws,
        confirmation_protocol=cp,
        feature_flag_service=ffs,
        signal_bus=signal_bus,
        **kwargs,
    )
    return agent


def _job_doc(
    job_id="JOB-001",
    tenant_id="default",
    job_type="cargo_transport",
    priority="high",
    status="in_progress",
    estimated_arrival=None,
    asset_assigned="TRUCK-100",
):
    """Create a sample job document.

    If *estimated_arrival* is not provided, defaults to 15 minutes from now
    (within the default 30-minute warning threshold).
    """
    if estimated_arrival is None:
        estimated_arrival = (
            datetime.now(timezone.utc) + timedelta(minutes=15)
        ).isoformat()
    return {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "job_type": job_type,
        "priority": priority,
        "status": status,
        "estimated_arrival": estimated_arrival,
        "asset_assigned": asset_assigned,
    }


def _es_response(docs):
    """Wrap documents in an ES search response structure."""
    return {
        "hits": {
            "hits": [{"_source": doc} for doc in docs],
            "total": {"value": len(docs)},
        }
    }


# ---------------------------------------------------------------------------
# Tests: Constructor defaults (Req 4.1)
# ---------------------------------------------------------------------------


class TestConstructorDefaults:
    """Tests that JobSLAMonitor has correct default configuration."""

    def test_agent_id(self):
        """Agent ID is 'job_sla_monitor'."""
        agent = _make_agent()
        assert agent.agent_id == "job_sla_monitor"

    def test_default_poll_interval(self):
        """Default poll interval is 90 seconds."""
        agent = _make_agent()
        assert agent.poll_interval == 90

    def test_default_cooldown_minutes(self):
        """Default cooldown is 15 minutes."""
        agent = _make_agent()
        assert agent.cooldown_minutes == 15

    def test_default_sla_warning_threshold_minutes(self):
        """Default SLA warning threshold is 30 minutes."""
        agent = _make_agent()
        assert agent._sla_warning_threshold_minutes == 30

    def test_signal_bus_stored(self):
        """Signal bus reference is stored when provided."""
        bus = MagicMock()
        agent = _make_agent(signal_bus=bus)
        assert agent._signal_bus is bus

    def test_signal_bus_defaults_to_none(self):
        """Signal bus defaults to None when not provided."""
        agent = _make_agent()
        assert agent._signal_bus is None

    def test_custom_poll_interval(self):
        """Custom poll interval is respected."""
        agent = _make_agent(poll_interval=60)
        assert agent.poll_interval == 60

    def test_custom_cooldown_minutes(self):
        """Custom cooldown minutes is respected."""
        agent = _make_agent(cooldown_minutes=5)
        assert agent.cooldown_minutes == 5

    def test_custom_sla_warning_threshold(self):
        """Custom SLA warning threshold is respected."""
        agent = _make_agent(sla_warning_threshold_minutes=45)
        assert agent._sla_warning_threshold_minutes == 45


# ---------------------------------------------------------------------------
# Tests: monitor_cycle publishes RiskSignal (Req 4.3)
# ---------------------------------------------------------------------------


class TestMonitorCycleRiskSignal:
    """Tests that monitor_cycle publishes RiskSignal with correct fields."""

    @pytest.mark.asyncio
    async def test_publishes_risk_signal(self):
        """monitor_cycle publishes a RiskSignal when signal_bus is provided."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        bus.publish.assert_called_once()
        signal = bus.publish.call_args[0][0]
        assert isinstance(signal, RiskSignal)

    @pytest.mark.asyncio
    async def test_risk_signal_source_agent(self):
        """Published RiskSignal has source_agent='job_sla_monitor'."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.source_agent == "job_sla_monitor"

    @pytest.mark.asyncio
    async def test_risk_signal_entity_type_is_job(self):
        """Published RiskSignal has entity_type='job'."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.entity_type == "job"

    @pytest.mark.asyncio
    async def test_risk_signal_confidence(self):
        """Published RiskSignal has confidence=0.85."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.confidence == 0.85

    @pytest.mark.asyncio
    async def test_risk_signal_ttl_seconds(self):
        """Published RiskSignal has ttl_seconds=1800."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.ttl_seconds == 1800

    @pytest.mark.asyncio
    async def test_risk_signal_entity_id_is_job_id(self):
        """Published RiskSignal has entity_id set to the job_id."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc(job_id="JOB-SIGNAL-01")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.entity_id == "JOB-SIGNAL-01"

    @pytest.mark.asyncio
    async def test_no_signal_when_bus_is_none(self):
        """monitor_cycle does not crash when signal_bus is None."""
        agent = _make_agent(signal_bus=None)
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        detections, actions = await agent.monitor_cycle()

        assert "JOB-001" in detections
        assert len(actions) == 1


# ---------------------------------------------------------------------------
# Tests: monitor_cycle broadcasts WebSocket event (Req 4.4)
# ---------------------------------------------------------------------------


class TestMonitorCycleWebSocket:
    """Tests that monitor_cycle broadcasts job_sla_warning WebSocket event."""

    @pytest.mark.asyncio
    async def test_broadcasts_job_sla_warning_event(self):
        """monitor_cycle broadcasts a 'job_sla_warning' event."""
        agent = _make_agent()
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        agent._ws.broadcast_event.assert_called_once()
        event_name = agent._ws.broadcast_event.call_args[0][0]
        assert event_name == "job_sla_warning"

    @pytest.mark.asyncio
    async def test_ws_event_contains_job_id(self):
        """WebSocket event contains job_id."""
        agent = _make_agent()
        job = _job_doc(job_id="JOB-WS-01")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert event_data["job_id"] == "JOB-WS-01"

    @pytest.mark.asyncio
    async def test_ws_event_contains_estimated_arrival(self):
        """WebSocket event contains estimated_arrival."""
        arrival = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        agent = _make_agent()
        job = _job_doc(estimated_arrival=arrival)
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert event_data["estimated_arrival"] == arrival

    @pytest.mark.asyncio
    async def test_ws_event_contains_time_remaining_minutes(self):
        """WebSocket event contains time_remaining_minutes."""
        agent = _make_agent()
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert "time_remaining_minutes" in event_data
        assert isinstance(event_data["time_remaining_minutes"], float)

    @pytest.mark.asyncio
    async def test_ws_event_contains_asset_assigned(self):
        """WebSocket event contains asset_assigned."""
        agent = _make_agent()
        job = _job_doc(asset_assigned="TRUCK-200")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert event_data["asset_assigned"] == "TRUCK-200"

    @pytest.mark.asyncio
    async def test_ws_event_contains_tenant_id(self):
        """WebSocket event contains tenant_id."""
        agent = _make_agent()
        job = _job_doc(tenant_id="tenant-xyz")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert event_data["tenant_id"] == "tenant-xyz"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle skips jobs without estimated_arrival (Req 4.8)
# ---------------------------------------------------------------------------


class TestMonitorCycleSkipsNoEstimatedArrival:
    """Tests that monitor_cycle skips jobs without estimated_arrival."""

    @pytest.mark.asyncio
    async def test_skips_job_without_estimated_arrival(self):
        """Jobs with estimated_arrival=None are skipped."""
        agent = _make_agent()
        job = _job_doc()
        job["estimated_arrival"] = None
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        detections, actions = await agent.monitor_cycle()

        assert len(actions) == 0
        agent._ws.broadcast_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_job_with_estimated_arrival(self):
        """Jobs with a valid estimated_arrival are processed."""
        agent = _make_agent()
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        detections, actions = await agent.monitor_cycle()

        assert len(actions) == 1
        agent._ws.broadcast_event.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: monitor_cycle skips disabled tenants (Req 4.7)
# ---------------------------------------------------------------------------


class TestMonitorCycleDisabledTenants:
    """Tests that monitor_cycle skips jobs belonging to disabled tenants."""

    @pytest.mark.asyncio
    async def test_skips_disabled_tenant(self):
        """Jobs from disabled tenants are not processed."""
        agent = _make_agent(feature_flags=True)
        agent._feature_flags.is_enabled = AsyncMock(return_value=False)
        job = _job_doc(tenant_id="disabled-tenant")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        detections, actions = await agent.monitor_cycle()

        assert len(actions) == 0
        agent._ws.broadcast_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_enabled_tenant(self):
        """Jobs from enabled tenants are processed normally."""
        agent = _make_agent(feature_flags=True)
        agent._feature_flags.is_enabled = AsyncMock(return_value=True)
        job = _job_doc(tenant_id="enabled-tenant")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        detections, actions = await agent.monitor_cycle()

        assert len(actions) == 1
        agent._ws.broadcast_event.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: monitor_cycle respects per-job cooldown (Req 4.6)
# ---------------------------------------------------------------------------


class TestMonitorCycleCooldown:
    """Tests that monitor_cycle respects per-job cooldown."""

    @pytest.mark.asyncio
    async def test_skips_job_on_cooldown(self):
        """Second cycle for the same job is skipped due to cooldown."""
        agent = _make_agent()
        job = _job_doc(job_id="JOB-COOL")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        # First cycle — should process
        await agent.monitor_cycle()
        assert agent._ws.broadcast_event.call_count == 1

        # Second cycle — same job should be skipped
        agent._ws.broadcast_event.reset_mock()
        await agent.monitor_cycle()
        assert agent._ws.broadcast_event.call_count == 0

    @pytest.mark.asyncio
    async def test_cooldown_job_still_detected(self):
        """Job on cooldown is still in detections but no action is taken."""
        agent = _make_agent()
        job = _job_doc(job_id="JOB-COOL")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        # First cycle
        await agent.monitor_cycle()

        # Second cycle
        detections, actions = await agent.monitor_cycle()
        assert "JOB-COOL" in detections
        assert len(actions) == 0


# ---------------------------------------------------------------------------
# Tests: Severity derivation — breach vs approaching (Req 4.9)
# ---------------------------------------------------------------------------


class TestSeverityDerivation:
    """Tests severity is critical on breach and high when approaching."""

    def test_critical_when_breach_occurred(self):
        """Severity is CRITICAL when time_remaining_minutes <= 0 (breach)."""
        assert JobSLAMonitor._derive_severity(-10.0) == Severity.CRITICAL

    def test_critical_at_zero(self):
        """Severity is CRITICAL when time_remaining_minutes == 0 (exact breach)."""
        assert JobSLAMonitor._derive_severity(0.0) == Severity.CRITICAL

    def test_high_when_approaching(self):
        """Severity is HIGH when time_remaining_minutes > 0 (approaching)."""
        assert JobSLAMonitor._derive_severity(15.0) == Severity.HIGH

    def test_high_just_above_zero(self):
        """Severity is HIGH when time_remaining_minutes is just above 0."""
        assert JobSLAMonitor._derive_severity(0.1) == Severity.HIGH

    @pytest.mark.asyncio
    async def test_breach_produces_critical_signal(self):
        """monitor_cycle publishes CRITICAL severity when breach has occurred."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        # Set estimated_arrival 10 minutes in the past → breach
        past_arrival = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat()
        job = _job_doc(estimated_arrival=past_arrival)
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.severity == Severity.CRITICAL

    @pytest.mark.asyncio
    async def test_approaching_produces_high_signal(self):
        """monitor_cycle publishes HIGH severity when approaching breach."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        # Set estimated_arrival 15 minutes in the future → approaching
        future_arrival = (
            datetime.now(timezone.utc) + timedelta(minutes=15)
        ).isoformat()
        job = _job_doc(estimated_arrival=future_arrival)
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.severity == Severity.HIGH


# ---------------------------------------------------------------------------
# Tests: RiskSignal context fields (Req 4.5)
# ---------------------------------------------------------------------------


class TestRiskSignalContext:
    """Tests that RiskSignal context contains required fields."""

    @pytest.mark.asyncio
    async def test_context_contains_job_type(self):
        """RiskSignal context contains job_type."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc(job_type="cargo_transport")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.context["job_type"] == "cargo_transport"

    @pytest.mark.asyncio
    async def test_context_contains_priority(self):
        """RiskSignal context contains priority."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc(priority="high")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.context["priority"] == "high"

    @pytest.mark.asyncio
    async def test_context_contains_asset_assigned(self):
        """RiskSignal context contains asset_assigned."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc(asset_assigned="TRUCK-100")
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.context["asset_assigned"] == "TRUCK-100"

    @pytest.mark.asyncio
    async def test_context_contains_estimated_arrival(self):
        """RiskSignal context contains estimated_arrival."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        arrival = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc(estimated_arrival=arrival)
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.context["estimated_arrival"] == arrival

    @pytest.mark.asyncio
    async def test_context_contains_time_remaining_minutes(self):
        """RiskSignal context contains time_remaining_minutes."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _job_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([job]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert "time_remaining_minutes" in signal.context
        assert isinstance(signal.context["time_remaining_minutes"], float)
