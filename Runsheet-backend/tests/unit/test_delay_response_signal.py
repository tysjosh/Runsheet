"""
Unit tests for delay_response_agent SignalBus publishing modifications.

Tests the _derive_delay_severity static method, RiskSignal publishing
in monitor_cycle, backward compatibility when signal_bus is None,
error resilience when SignalBus publish raises, and RiskSignal field
correctness (confidence, ttl_seconds, context fields).

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call

from Agents.autonomous.delay_response_agent import DelayResponseAgent
from Agents.confirmation_protocol import MutationRequest, MutationResult
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
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock(
        return_value=MutationResult(
            executed=True,
            risk_level="medium",
            result="Successfully executed assign_asset_to_job",
            confirmation_method="immediate",
        )
    )

    feature_flag_service = None
    if feature_flags:
        feature_flag_service = MagicMock()
        feature_flag_service.is_enabled = AsyncMock(return_value=True)

    return es_service, activity_log, ws_manager, confirmation_protocol, feature_flag_service


def _make_agent(signal_bus=None, feature_flags=False):
    """Create a DelayResponseAgent with mocked dependencies and optional signal_bus."""
    es, al, ws, cp, ffs = _make_deps(feature_flags=feature_flags)
    agent = DelayResponseAgent(
        es_service=es,
        activity_log_service=al,
        ws_manager=ws,
        confirmation_protocol=cp,
        feature_flag_service=ffs,
        signal_bus=signal_bus,
    )
    return agent


def _delayed_job(
    job_id="JOB-001",
    tenant_id="default",
    job_type="cargo_transport",
    priority="high",
    asset_assigned="TRUCK-100",
    estimated_arrival=None,
):
    """Create a sample delayed job document."""
    if estimated_arrival is None:
        estimated_arrival = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat()
    return {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "job_type": job_type,
        "priority": priority,
        "status": "in_progress",
        "estimated_arrival": estimated_arrival,
        "origin": "Warehouse A",
        "destination": "Port B",
        "asset_assigned": asset_assigned,
    }


def _available_asset(asset_id="TRUCK-200", asset_type="vehicle"):
    """Create a sample available asset document."""
    return {
        "asset_id": asset_id,
        "asset_type": asset_type,
        "status": "on_time",
        "tenant_id": "default",
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
# Tests: _derive_delay_severity (Req 5.2)
# ---------------------------------------------------------------------------


class TestDeriveDelaySeverity:
    """Tests for the _derive_delay_severity static method."""

    def test_zero_delay_returns_low(self):
        """Delay of 0 minutes returns LOW severity."""
        assert DelayResponseAgent._derive_delay_severity(0) == Severity.LOW

    def test_fifteen_minutes_returns_low(self):
        """Delay of exactly 15 minutes returns LOW severity (boundary: <= 15)."""
        assert DelayResponseAgent._derive_delay_severity(15) == Severity.LOW

    def test_just_over_fifteen_returns_medium(self):
        """Delay of 15.1 minutes returns MEDIUM severity (boundary: > 15)."""
        assert DelayResponseAgent._derive_delay_severity(15.1) == Severity.MEDIUM

    def test_thirty_minutes_returns_medium(self):
        """Delay of exactly 30 minutes returns MEDIUM severity (boundary: <= 30)."""
        assert DelayResponseAgent._derive_delay_severity(30) == Severity.MEDIUM

    def test_just_over_thirty_returns_high(self):
        """Delay of 30.1 minutes returns HIGH severity (boundary: > 30)."""
        assert DelayResponseAgent._derive_delay_severity(30.1) == Severity.HIGH

    def test_sixty_minutes_returns_high(self):
        """Delay of exactly 60 minutes returns HIGH severity (boundary: <= 60)."""
        assert DelayResponseAgent._derive_delay_severity(60) == Severity.HIGH

    def test_just_over_sixty_returns_critical(self):
        """Delay of 60.1 minutes returns CRITICAL severity (boundary: > 60)."""
        assert DelayResponseAgent._derive_delay_severity(60.1) == Severity.CRITICAL


# ---------------------------------------------------------------------------
# Tests: signal_bus constructor parameter (Req 5.7)
# ---------------------------------------------------------------------------


class TestSignalBusConstructor:
    """Tests that signal_bus is accepted and stored."""

    def test_signal_bus_stored(self):
        """Agent stores signal_bus reference when provided."""
        bus = MagicMock()
        agent = _make_agent(signal_bus=bus)
        assert agent._signal_bus is bus

    def test_signal_bus_defaults_to_none(self):
        """Agent defaults signal_bus to None when not provided."""
        agent = _make_agent(signal_bus=None)
        assert agent._signal_bus is None


# ---------------------------------------------------------------------------
# Tests: monitor_cycle publishes RiskSignal (Req 5.1, 5.6)
# ---------------------------------------------------------------------------


class TestMonitorCyclePublishesSignal:
    """Tests that monitor_cycle publishes RiskSignal when signal_bus is provided."""

    @pytest.mark.asyncio
    async def test_publishes_risk_signal_when_bus_provided(self):
        """monitor_cycle calls signal_bus.publish with a RiskSignal."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        bus.publish.assert_called_once()
        published_signal = bus.publish.call_args[0][0]
        assert isinstance(published_signal, RiskSignal)

    @pytest.mark.asyncio
    async def test_publishes_before_mutation_request(self):
        """RiskSignal is published before MutationRequest is created (Req 5.6)."""
        call_order = []

        bus = MagicMock()

        async def track_publish(signal):
            call_order.append("publish")

        bus.publish = AsyncMock(side_effect=track_publish)

        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        original_process = agent._confirmation_protocol.process_mutation

        async def track_mutation(request):
            call_order.append("mutation")
            return MutationResult(
                executed=True,
                risk_level="medium",
                result="ok",
                confirmation_method="immediate",
            )

        agent._confirmation_protocol.process_mutation = AsyncMock(
            side_effect=track_mutation
        )

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        assert call_order.index("publish") < call_order.index("mutation")


# ---------------------------------------------------------------------------
# Tests: monitor_cycle skips publish when signal_bus is None (Req 5.7)
# ---------------------------------------------------------------------------


class TestMonitorCycleNoSignalBus:
    """Tests backward compatibility when signal_bus is None."""

    @pytest.mark.asyncio
    async def test_no_crash_without_signal_bus(self):
        """monitor_cycle completes without error when signal_bus is None."""
        agent = _make_agent(signal_bus=None)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        detections, actions = await agent.monitor_cycle()

        assert "JOB-001" in detections
        assert len(actions) == 1

    @pytest.mark.asyncio
    async def test_still_creates_mutation_request(self):
        """monitor_cycle still processes MutationRequest when signal_bus is None."""
        agent = _make_agent(signal_bus=None)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        agent._confirmation_protocol.process_mutation.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: monitor_cycle continues when SignalBus publish raises (Req 5.8)
# ---------------------------------------------------------------------------


class TestMonitorCycleSignalBusError:
    """Tests that monitor_cycle continues when signal_bus.publish raises."""

    @pytest.mark.asyncio
    async def test_continues_after_publish_exception(self):
        """monitor_cycle continues with MutationRequest when publish raises."""
        bus = MagicMock()
        bus.publish = AsyncMock(side_effect=Exception("SignalBus down"))
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        detections, actions = await agent.monitor_cycle()

        # Should still complete the cycle
        assert "JOB-001" in detections
        assert len(actions) == 1
        assert actions[0]["action"] == "reassignment"

    @pytest.mark.asyncio
    async def test_mutation_still_called_after_publish_error(self):
        """ConfirmationProtocol is still called even when publish fails."""
        bus = MagicMock()
        bus.publish = AsyncMock(side_effect=RuntimeError("bus error"))
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        agent._confirmation_protocol.process_mutation.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: RiskSignal confidence and ttl_seconds (Req 5.3, 5.4)
# ---------------------------------------------------------------------------


class TestRiskSignalConfidenceAndTTL:
    """Tests that published RiskSignal has confidence=0.9 and ttl_seconds=1800."""

    @pytest.mark.asyncio
    async def test_confidence_is_0_9(self):
        """Published RiskSignal has confidence=0.9."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.confidence == 0.9

    @pytest.mark.asyncio
    async def test_ttl_seconds_is_1800(self):
        """Published RiskSignal has ttl_seconds=1800."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.ttl_seconds == 1800


# ---------------------------------------------------------------------------
# Tests: RiskSignal fields (Req 5.1, 5.5)
# ---------------------------------------------------------------------------


class TestRiskSignalFields:
    """Tests that published RiskSignal has correct source_agent, entity fields, and context."""

    @pytest.mark.asyncio
    async def test_source_agent_is_delay_response_agent(self):
        """Published RiskSignal has source_agent='delay_response_agent'."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.source_agent == "delay_response_agent"

    @pytest.mark.asyncio
    async def test_entity_id_is_job_id(self):
        """Published RiskSignal has entity_id set to the job_id."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job(job_id="JOB-DELAY-42")
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.entity_id == "JOB-DELAY-42"

    @pytest.mark.asyncio
    async def test_entity_type_is_job(self):
        """Published RiskSignal has entity_type='job'."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.entity_type == "job"

    @pytest.mark.asyncio
    async def test_context_contains_job_type(self):
        """Published RiskSignal context contains job_type."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job(job_type="cargo_transport")
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert "job_type" in signal.context
        assert signal.context["job_type"] == "cargo_transport"

    @pytest.mark.asyncio
    async def test_context_contains_priority(self):
        """Published RiskSignal context contains priority."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job(priority="urgent")
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert "priority" in signal.context
        assert signal.context["priority"] == "urgent"

    @pytest.mark.asyncio
    async def test_context_contains_asset_assigned(self):
        """Published RiskSignal context contains asset_assigned."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job(asset_assigned="TRUCK-555")
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert "asset_assigned" in signal.context
        assert signal.context["asset_assigned"] == "TRUCK-555"

    @pytest.mark.asyncio
    async def test_context_contains_estimated_arrival(self):
        """Published RiskSignal context contains estimated_arrival."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        est = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        job = _delayed_job(estimated_arrival=est)
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert "estimated_arrival" in signal.context
        assert signal.context["estimated_arrival"] == est

    @pytest.mark.asyncio
    async def test_context_contains_detected_at(self):
        """Published RiskSignal context contains detected_at timestamp."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert "detected_at" in signal.context
        # detected_at should be a valid ISO timestamp
        detected = datetime.fromisoformat(signal.context["detected_at"])
        assert detected.tzinfo is not None

    @pytest.mark.asyncio
    async def test_context_has_all_required_fields(self):
        """Published RiskSignal context contains all five required fields."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        required_fields = {
            "job_type",
            "priority",
            "asset_assigned",
            "estimated_arrival",
            "detected_at",
        }
        assert required_fields.issubset(set(signal.context.keys()))
