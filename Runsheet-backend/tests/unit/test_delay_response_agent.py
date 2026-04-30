"""
Unit tests for the Delay Response Agent.

Tests the DelayResponseAgent autonomous agent including monitor_cycle,
_find_available_asset, _job_type_to_asset_type, feature flag handling,
cooldown enforcement, reassignment proposals, and escalation broadcasts.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.autonomous.delay_response_agent import (
    DelayResponseAgent,
    JOB_TYPE_TO_ASSET_TYPE,
    JOBS_CURRENT_INDEX,
    ASSETS_INDEX,
)
from Agents.confirmation_protocol import MutationRequest, MutationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(feature_flags=True):
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


def _make_agent(
    feature_flags=True,
    poll_interval=60,
    cooldown_minutes=15,
):
    """Create a DelayResponseAgent with mocked dependencies."""
    es, al, ws, cp, ffs = _make_deps(feature_flags=feature_flags)
    agent = DelayResponseAgent(
        es_service=es,
        activity_log_service=al,
        ws_manager=ws,
        confirmation_protocol=cp,
        feature_flag_service=ffs,
        poll_interval=poll_interval,
        cooldown_minutes=cooldown_minutes,
    )
    return agent


def _delayed_job(
    job_id="JOB-001",
    tenant_id="default",
    job_type="cargo_transport",
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
        "status": "in_progress",
        "estimated_arrival": estimated_arrival,
        "origin": "Warehouse A",
        "destination": "Port B",
        "asset_assigned": "TRUCK-100",
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
# Tests: __init__
# ---------------------------------------------------------------------------


class TestInit:
    """Tests for agent initialisation."""

    def test_agent_id(self):
        agent = _make_agent()
        assert agent.agent_id == "delay_response_agent"

    def test_default_poll_interval(self):
        es, al, ws, cp, ffs = _make_deps()
        agent = DelayResponseAgent(
            es_service=es,
            activity_log_service=al,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        assert agent.poll_interval == 60

    def test_default_cooldown(self):
        es, al, ws, cp, ffs = _make_deps()
        agent = DelayResponseAgent(
            es_service=es,
            activity_log_service=al,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        assert agent.cooldown_minutes == 15

    def test_custom_poll_interval(self):
        agent = _make_agent(poll_interval=120)
        assert agent.poll_interval == 120

    def test_custom_cooldown(self):
        agent = _make_agent(cooldown_minutes=30)
        assert agent.cooldown_minutes == 30

    def test_stores_es_service(self):
        agent = _make_agent()
        assert agent._es is not None

    def test_feature_flags_optional(self):
        agent = _make_agent(feature_flags=False)
        assert agent._feature_flags is None


# ---------------------------------------------------------------------------
# Tests: _job_type_to_asset_type
# ---------------------------------------------------------------------------


class TestJobTypeToAssetType:
    """Tests for the job type to asset type mapping."""

    def test_cargo_transport(self):
        assert DelayResponseAgent._job_type_to_asset_type("cargo_transport") == "vehicle"

    def test_passenger_transport(self):
        assert DelayResponseAgent._job_type_to_asset_type("passenger_transport") == "vehicle"

    def test_vessel_movement(self):
        assert DelayResponseAgent._job_type_to_asset_type("vessel_movement") == "vessel"

    def test_airport_transfer(self):
        assert DelayResponseAgent._job_type_to_asset_type("airport_transfer") == "vehicle"

    def test_crane_booking(self):
        assert DelayResponseAgent._job_type_to_asset_type("crane_booking") == "equipment"

    def test_unknown_type_defaults_to_vehicle(self):
        assert DelayResponseAgent._job_type_to_asset_type("unknown_type") == "vehicle"

    def test_none_defaults_to_vehicle(self):
        assert DelayResponseAgent._job_type_to_asset_type(None) == "vehicle"


# ---------------------------------------------------------------------------
# Tests: _find_available_asset
# ---------------------------------------------------------------------------


class TestFindAvailableAsset:
    """Tests for the _find_available_asset helper."""

    @pytest.mark.asyncio
    async def test_returns_asset_when_found(self):
        agent = _make_agent()
        asset = _available_asset()
        agent._es.search_documents = AsyncMock(return_value=_es_response([asset]))

        result = await agent._find_available_asset("vehicle", "default")
        assert result is not None
        assert result["asset_id"] == "TRUCK-200"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_asset(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        result = await agent._find_available_asset("vehicle", "default")
        assert result is None

    @pytest.mark.asyncio
    async def test_queries_correct_index(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent._find_available_asset("vessel", "tenant-1")

        call_args = agent._es.search_documents.call_args
        assert call_args[0][0] == ASSETS_INDEX

    @pytest.mark.asyncio
    async def test_filters_by_asset_type_and_tenant(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent._find_available_asset("equipment", "tenant-2")

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]

        tenant_filter = next(f for f in filters if "tenant_id" in f.get("term", {}))
        assert tenant_filter["term"]["tenant_id"] == "tenant-2"

        type_filter = next(f for f in filters if "asset_type" in f.get("term", {}))
        assert type_filter["term"]["asset_type"] == "equipment"

        status_filter = next(f for f in filters if "status" in f.get("term", {}))
        assert status_filter["term"]["status"] == "on_time"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — no delayed jobs
# ---------------------------------------------------------------------------


class TestMonitorCycleNoDelays:
    """Tests for monitor_cycle when no delayed jobs are found."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_delayed_jobs(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        detections, actions = await agent.monitor_cycle()
        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_queries_jobs_current_index(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent.monitor_cycle()

        call_args = agent._es.search_documents.call_args
        assert call_args[0][0] == JOBS_CURRENT_INDEX

    @pytest.mark.asyncio
    async def test_queries_in_progress_status(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent.monitor_cycle()

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        status_filter = next(f for f in filters if "term" in f and "status" in f["term"])
        assert status_filter["term"]["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — with delayed jobs and available asset
# ---------------------------------------------------------------------------


class TestMonitorCycleWithReassignment:
    """Tests for monitor_cycle when delayed jobs are found and assets available."""

    @pytest.mark.asyncio
    async def test_detects_delayed_job(self):
        agent = _make_agent()
        job = _delayed_job()
        asset = _available_asset()

        # First call: jobs query; second call: asset query
        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        detections, actions = await agent.monitor_cycle()
        assert "JOB-001" in detections

    @pytest.mark.asyncio
    async def test_proposes_reassignment(self):
        agent = _make_agent()
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        detections, actions = await agent.monitor_cycle()
        assert len(actions) == 1
        assert actions[0]["action"] == "reassignment"
        assert actions[0]["job_id"] == "JOB-001"

    @pytest.mark.asyncio
    async def test_calls_confirmation_protocol(self):
        agent = _make_agent()
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        agent._confirmation_protocol.process_mutation.assert_called_once()
        call_args = agent._confirmation_protocol.process_mutation.call_args
        request = call_args[0][0]
        assert isinstance(request, MutationRequest)
        assert request.tool_name == "assign_asset_to_job"
        assert request.parameters["job_id"] == "JOB-001"
        assert request.parameters["asset_id"] == "TRUCK-200"
        assert request.agent_id == "delay_response_agent"

    @pytest.mark.asyncio
    async def test_sets_cooldown_after_reassignment(self):
        agent = _make_agent()
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()
        assert agent._is_on_cooldown("JOB-001") is True

    @pytest.mark.asyncio
    async def test_multiple_delayed_jobs(self):
        agent = _make_agent()
        job1 = _delayed_job(job_id="JOB-001")
        job2 = _delayed_job(job_id="JOB-002")
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([job1, job2]),
                _es_response([asset]),  # asset for job1
                _es_response([asset]),  # asset for job2
            ]
        )

        detections, actions = await agent.monitor_cycle()
        assert len(detections) == 2
        assert len(actions) == 2


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — escalation (no asset available)
# ---------------------------------------------------------------------------


class TestMonitorCycleEscalation:
    """Tests for monitor_cycle when no compatible asset is available."""

    @pytest.mark.asyncio
    async def test_escalates_when_no_asset(self):
        agent = _make_agent()
        job = _delayed_job()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([])]
        )

        detections, actions = await agent.monitor_cycle()
        assert len(actions) == 1
        assert actions[0]["action"] == "escalation"
        assert actions[0]["job_id"] == "JOB-001"

    @pytest.mark.asyncio
    async def test_broadcasts_delay_alert(self):
        agent = _make_agent()
        job = _delayed_job()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([])]
        )

        await agent.monitor_cycle()

        agent._ws.broadcast_event.assert_called_once()
        call_args = agent._ws.broadcast_event.call_args
        assert call_args[0][0] == "delay_alert"
        payload = call_args[0][1]
        assert payload["job_id"] == "JOB-001"
        assert payload["reason"] == "no_alternative_available"
        assert "job_details" in payload

    @pytest.mark.asyncio
    async def test_sets_cooldown_after_escalation(self):
        agent = _make_agent()
        job = _delayed_job()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([])]
        )

        await agent.monitor_cycle()
        assert agent._is_on_cooldown("JOB-001") is True

    @pytest.mark.asyncio
    async def test_does_not_call_confirmation_protocol(self):
        agent = _make_agent()
        job = _delayed_job()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([])]
        )

        await agent.monitor_cycle()
        agent._confirmation_protocol.process_mutation.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — cooldown enforcement
# ---------------------------------------------------------------------------


class TestMonitorCycleCooldown:
    """Tests for cooldown enforcement in monitor_cycle."""

    @pytest.mark.asyncio
    async def test_skips_job_on_cooldown(self):
        agent = _make_agent()
        job = _delayed_job()

        # Pre-set cooldown for the job
        agent._set_cooldown("JOB-001")

        agent._es.search_documents = AsyncMock(
            return_value=_es_response([job])
        )

        detections, actions = await agent.monitor_cycle()
        # Job is detected but no action taken
        assert "JOB-001" in detections
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_does_not_query_assets_for_cooldown_job(self):
        agent = _make_agent()
        job = _delayed_job()

        agent._set_cooldown("JOB-001")

        agent._es.search_documents = AsyncMock(
            return_value=_es_response([job])
        )

        await agent.monitor_cycle()
        # Only the initial jobs query should have been made
        assert agent._es.search_documents.call_count == 1

    @pytest.mark.asyncio
    async def test_processes_job_after_cooldown_expires(self):
        agent = _make_agent(cooldown_minutes=15)
        job = _delayed_job()
        asset = _available_asset()

        # Set cooldown in the past (expired)
        agent._cooldown_tracker["JOB-001"] = datetime.now(timezone.utc) - timedelta(
            minutes=20
        )

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        detections, actions = await agent.monitor_cycle()
        assert len(actions) == 1
        assert actions[0]["action"] == "reassignment"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — feature flag enforcement
# ---------------------------------------------------------------------------


class TestMonitorCycleFeatureFlags:
    """Tests for tenant feature flag enforcement."""

    @pytest.mark.asyncio
    async def test_skips_disabled_tenant(self):
        agent = _make_agent(feature_flags=True)
        job = _delayed_job(tenant_id="disabled-tenant")

        agent._feature_flags.is_enabled = AsyncMock(return_value=False)
        agent._es.search_documents = AsyncMock(
            return_value=_es_response([job])
        )

        detections, actions = await agent.monitor_cycle()
        # Job is not added to detections when tenant is disabled
        assert len(detections) == 0
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_processes_enabled_tenant(self):
        agent = _make_agent(feature_flags=True)
        job = _delayed_job(tenant_id="enabled-tenant")
        asset = _available_asset()

        agent._feature_flags.is_enabled = AsyncMock(return_value=True)
        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        detections, actions = await agent.monitor_cycle()
        assert "JOB-001" in detections
        assert len(actions) == 1

    @pytest.mark.asyncio
    async def test_no_feature_flag_service_processes_all(self):
        agent = _make_agent(feature_flags=False)
        job = _delayed_job()
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        detections, actions = await agent.monitor_cycle()
        assert "JOB-001" in detections
        assert len(actions) == 1

    @pytest.mark.asyncio
    async def test_mixed_tenants(self):
        agent = _make_agent(feature_flags=True)
        job_enabled = _delayed_job(job_id="JOB-001", tenant_id="enabled")
        job_disabled = _delayed_job(job_id="JOB-002", tenant_id="disabled")
        asset = _available_asset()

        async def is_enabled(tenant_id):
            return tenant_id == "enabled"

        agent._feature_flags.is_enabled = AsyncMock(side_effect=is_enabled)
        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([job_enabled, job_disabled]),
                _es_response([asset]),  # asset query for enabled job
            ]
        )

        detections, actions = await agent.monitor_cycle()
        assert "JOB-001" in detections
        assert "JOB-002" not in detections
        assert len(actions) == 1


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — tenant_id handling
# ---------------------------------------------------------------------------


class TestMonitorCycleTenantId:
    """Tests for tenant_id handling in monitor_cycle."""

    @pytest.mark.asyncio
    async def test_uses_job_tenant_id(self):
        agent = _make_agent()
        job = _delayed_job(tenant_id="tenant-42")
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        request = agent._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.tenant_id == "tenant-42"

    @pytest.mark.asyncio
    async def test_defaults_to_default_tenant(self):
        agent = _make_agent(feature_flags=False)
        job = _delayed_job()
        del job["tenant_id"]  # Remove tenant_id to test default
        asset = _available_asset()

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([asset])]
        )

        await agent.monitor_cycle()

        request = agent._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.tenant_id == "default"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — job type mapping in context
# ---------------------------------------------------------------------------


class TestMonitorCycleJobTypeMapping:
    """Tests that the correct asset type is queried based on job type."""

    @pytest.mark.asyncio
    async def test_vessel_movement_queries_vessel(self):
        agent = _make_agent(feature_flags=False)
        job = _delayed_job(job_type="vessel_movement")

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([])]
        )

        await agent.monitor_cycle()

        # Second call is the asset query
        asset_query_call = agent._es.search_documents.call_args_list[1]
        query = asset_query_call[0][1]
        filters = query["query"]["bool"]["filter"]
        type_filter = next(f for f in filters if "asset_type" in f.get("term", {}))
        assert type_filter["term"]["asset_type"] == "vessel"

    @pytest.mark.asyncio
    async def test_crane_booking_queries_equipment(self):
        agent = _make_agent(feature_flags=False)
        job = _delayed_job(job_type="crane_booking")

        agent._es.search_documents = AsyncMock(
            side_effect=[_es_response([job]), _es_response([])]
        )

        await agent.monitor_cycle()

        asset_query_call = agent._es.search_documents.call_args_list[1]
        query = asset_query_call[0][1]
        filters = query["query"]["bool"]["filter"]
        type_filter = next(f for f in filters if "asset_type" in f.get("term", {}))
        assert type_filter["term"]["asset_type"] == "equipment"
