"""
Unit tests for cross-domain agent integration foundation changes.

Tests the RerouteJobRequest model validation, ConfirmationProtocol
reroute_job and truck_fuel_alert handlers, and job_priorities ES
index mapping registration in setup_overlay_indices.

Requirements: 1.3, 2.1, 2.2, 3.7
"""
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from pydantic import ValidationError

from scheduling.models import RerouteJobRequest, GeoPoint
from Agents.confirmation_protocol import (
    ConfirmationProtocol,
    MutationRequest,
)
from Agents.overlay.overlay_es_mappings import (
    JOB_PRIORITIES_INDEX,
    JOB_PRIORITIES_MAPPING,
    setup_overlay_indices,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    tool_name: str = "reroute_job",
    parameters: dict = None,
    tenant_id: str = "t1",
    agent_id: str = "ai_agent",
) -> MutationRequest:
    """Create a MutationRequest with sensible defaults."""
    return MutationRequest(
        tool_name=tool_name,
        parameters=parameters or {},
        tenant_id=tenant_id,
        agent_id=agent_id,
    )


def _make_protocol_with_es() -> ConfirmationProtocol:
    """Create a ConfirmationProtocol with a mocked ES service for _execute_mutation tests."""
    es_service = MagicMock()
    es_service.update_document = AsyncMock()
    es_service.index_document = AsyncMock()

    protocol = ConfirmationProtocol(
        risk_registry=MagicMock(),
        approval_queue_service=MagicMock(),
        autonomy_config_service=MagicMock(),
        activity_log_service=MagicMock(),
        business_validator=MagicMock(),
        es_service=es_service,
    )
    return protocol


# ---------------------------------------------------------------------------
# Tests: RerouteJobRequest model validation (Req 2.1)
# ---------------------------------------------------------------------------


class TestRerouteJobRequest:
    """Tests for the RerouteJobRequest Pydantic model."""

    def test_valid_input_all_fields(self):
        """RerouteJobRequest accepts valid input with all fields."""
        req = RerouteJobRequest(
            new_destination="Warehouse B",
            new_destination_location=GeoPoint(lat=37.7749, lng=-122.4194),
            reason="Customer requested change",
        )
        assert req.new_destination == "Warehouse B"
        assert req.new_destination_location.lat == 37.7749
        assert req.new_destination_location.lng == -122.4194
        assert req.reason == "Customer requested change"

    def test_valid_input_without_location(self):
        """RerouteJobRequest accepts None for new_destination_location."""
        req = RerouteJobRequest(
            new_destination="Depot C",
            reason="Route blocked",
        )
        assert req.new_destination == "Depot C"
        assert req.new_destination_location is None
        assert req.reason == "Route blocked"

    def test_missing_new_destination_raises(self):
        """RerouteJobRequest raises ValidationError when new_destination is missing."""
        with pytest.raises(ValidationError):
            RerouteJobRequest(reason="Some reason")

    def test_missing_reason_raises(self):
        """RerouteJobRequest raises ValidationError when reason is missing."""
        with pytest.raises(ValidationError):
            RerouteJobRequest(new_destination="Warehouse B")

    def test_missing_both_required_fields_raises(self):
        """RerouteJobRequest raises ValidationError when both required fields are missing."""
        with pytest.raises(ValidationError):
            RerouteJobRequest()

    def test_explicit_none_for_location_accepted(self):
        """RerouteJobRequest accepts explicit None for new_destination_location."""
        req = RerouteJobRequest(
            new_destination="Site D",
            new_destination_location=None,
            reason="Emergency diversion",
        )
        assert req.new_destination_location is None


# ---------------------------------------------------------------------------
# Tests: ConfirmationProtocol reroute_job handler (Req 2.2)
# ---------------------------------------------------------------------------


class TestConfirmationProtocolRerouteJob:
    """Tests for the reroute_job branch in _execute_mutation."""

    async def test_reroute_job_calls_update_document(self):
        """reroute_job handler calls update_document on jobs_current."""
        protocol = _make_protocol_with_es()
        request = _make_request(
            tool_name="reroute_job",
            parameters={
                "job_id": "JOB_001",
                "new_destination": "Warehouse X",
            },
            tenant_id="tenant-abc",
        )

        result = await protocol._execute_mutation(request)

        protocol._es.update_document.assert_called_once()
        call_args = protocol._es.update_document.call_args
        assert call_args[0][0] == "jobs_current"
        assert call_args[0][1] == "JOB_001"

    async def test_reroute_job_updates_correct_fields(self):
        """reroute_job handler updates destination, updated_at, and tenant_id."""
        protocol = _make_protocol_with_es()
        request = _make_request(
            tool_name="reroute_job",
            parameters={
                "job_id": "JOB_002",
                "new_destination": "Depot Y",
            },
            tenant_id="tenant-xyz",
        )

        await protocol._execute_mutation(request)

        call_args = protocol._es.update_document.call_args
        update_body = call_args[0][2]
        assert update_body["destination"] == "Depot Y"
        assert update_body["tenant_id"] == "tenant-xyz"
        assert "updated_at" in update_body

    async def test_reroute_job_returns_success_string(self):
        """reroute_job handler returns a success message."""
        protocol = _make_protocol_with_es()
        request = _make_request(
            tool_name="reroute_job",
            parameters={
                "job_id": "JOB_003",
                "new_destination": "Site Z",
            },
            tenant_id="t1",
        )

        result = await protocol._execute_mutation(request)

        assert "reroute_job" in result
        assert "t1" in result

    async def test_reroute_job_es_failure_returns_error(self):
        """reroute_job handler returns error message when ES update fails."""
        protocol = _make_protocol_with_es()
        protocol._es.update_document = AsyncMock(
            side_effect=Exception("ES connection lost")
        )
        request = _make_request(
            tool_name="reroute_job",
            parameters={
                "job_id": "JOB_004",
                "new_destination": "Fallback",
            },
        )

        result = await protocol._execute_mutation(request)

        assert "Failed" in result
        assert "ES connection lost" in result


# ---------------------------------------------------------------------------
# Tests: ConfirmationProtocol truck_fuel_alert handler (Req 1.3)
# ---------------------------------------------------------------------------


class TestConfirmationProtocolTruckFuelAlert:
    """Tests for the truck_fuel_alert branch in _execute_mutation."""

    async def test_truck_fuel_alert_calls_index_document(self):
        """truck_fuel_alert handler calls index_document on agent_activity_log."""
        protocol = _make_protocol_with_es()
        request = _make_request(
            tool_name="truck_fuel_alert",
            parameters={
                "truck_id": "TRUCK_001",
                "fuel_level_pct": 8.5,
                "tenant_id": "tenant-fuel",
            },
            tenant_id="tenant-fuel",
        )

        await protocol._execute_mutation(request)

        protocol._es.index_document.assert_called_once()
        call_args = protocol._es.index_document.call_args
        assert call_args[0][0] == "agent_activity_log"

    async def test_truck_fuel_alert_document_has_correct_fields(self):
        """truck_fuel_alert handler indexes document with required fields."""
        protocol = _make_protocol_with_es()
        params = {
            "truck_id": "TRUCK_002",
            "fuel_level_pct": 5.0,
        }
        request = _make_request(
            tool_name="truck_fuel_alert",
            parameters=params,
            tenant_id="tenant-fuel",
        )

        await protocol._execute_mutation(request)

        call_args = protocol._es.index_document.call_args
        doc = call_args[0][2]
        assert doc["agent_id"] == "truck_fuel_monitor"
        assert doc["action_type"] == "truck_fuel_alert"
        assert doc["parameters"] == params
        assert doc["tenant_id"] == "tenant-fuel"
        assert "timestamp" in doc
        assert "log_id" in doc

    async def test_truck_fuel_alert_id_starts_with_fuel_alert(self):
        """truck_fuel_alert handler generates an alert_id starting with FUEL_ALERT_."""
        protocol = _make_protocol_with_es()
        request = _make_request(
            tool_name="truck_fuel_alert",
            parameters={"truck_id": "TRUCK_003"},
            tenant_id="t1",
        )

        await protocol._execute_mutation(request)

        call_args = protocol._es.index_document.call_args
        doc_id = call_args[0][1]
        assert doc_id.startswith("FUEL_ALERT_")

    async def test_truck_fuel_alert_returns_success_string(self):
        """truck_fuel_alert handler returns a success message."""
        protocol = _make_protocol_with_es()
        request = _make_request(
            tool_name="truck_fuel_alert",
            parameters={"truck_id": "TRUCK_004"},
            tenant_id="t1",
        )

        result = await protocol._execute_mutation(request)

        assert "truck_fuel_alert" in result
        assert "t1" in result

    async def test_truck_fuel_alert_es_failure_returns_error(self):
        """truck_fuel_alert handler returns error message when ES index fails."""
        protocol = _make_protocol_with_es()
        protocol._es.index_document = AsyncMock(
            side_effect=Exception("ES write failed")
        )
        request = _make_request(
            tool_name="truck_fuel_alert",
            parameters={"truck_id": "TRUCK_005"},
        )

        result = await protocol._execute_mutation(request)

        assert "Failed" in result
        assert "ES write failed" in result


# ---------------------------------------------------------------------------
# Tests: job_priorities ES index mapping (Req 3.7)
# ---------------------------------------------------------------------------


class TestJobPrioritiesMapping:
    """Tests for the JOB_PRIORITIES_INDEX and JOB_PRIORITIES_MAPPING."""

    def test_index_name_is_job_priorities(self):
        assert JOB_PRIORITIES_INDEX == "job_priorities"

    def test_mapping_is_strict(self):
        assert JOB_PRIORITIES_MAPPING["mappings"]["dynamic"] == "strict"

    def test_top_level_keyword_fields(self):
        props = JOB_PRIORITIES_MAPPING["mappings"]["properties"]
        for field in ["priority_list_id", "tenant_id"]:
            assert props[field]["type"] == "keyword", f"{field} should be keyword"

    def test_timestamp_field(self):
        props = JOB_PRIORITIES_MAPPING["mappings"]["properties"]
        assert props["timestamp"]["type"] == "date"

    def test_scoring_weights_is_flattened(self):
        # ``scoring_weights`` is a small key→float map. Under the index's
        # dynamic: strict root, an ``object`` field would inherit strict and
        # reject the weight keys on write, so it's mapped as ``flattened``
        # (whole map stored as one field, keys not individually mapped).
        props = JOB_PRIORITIES_MAPPING["mappings"]["properties"]
        assert props["scoring_weights"]["type"] == "flattened"

    def test_priorities_is_nested(self):
        props = JOB_PRIORITIES_MAPPING["mappings"]["properties"]
        assert props["priorities"]["type"] == "nested"

    def test_nested_priority_fields(self):
        nested_props = JOB_PRIORITIES_MAPPING["mappings"]["properties"]["priorities"]["properties"]
        assert nested_props["job_id"]["type"] == "keyword"
        assert nested_props["job_type"]["type"] == "keyword"
        assert nested_props["priority_score"]["type"] == "float"
        assert nested_props["priority_bucket"]["type"] == "keyword"
        assert nested_props["sla_urgency"]["type"] == "float"
        assert nested_props["cargo_priority_score"]["type"] == "float"
        assert nested_props["customer_tier_score"]["type"] == "float"
        assert nested_props["reasons"]["type"] == "keyword"

    def test_shard_settings(self):
        settings = JOB_PRIORITIES_MAPPING["settings"]
        assert settings["number_of_shards"] == 1
        assert settings["number_of_replicas"] == 1


# ---------------------------------------------------------------------------
# Tests: setup_overlay_indices includes job_priorities (Req 3.7)
# ---------------------------------------------------------------------------


class TestSetupOverlayIndicesJobPriorities:
    """Tests that setup_overlay_indices creates the job_priorities index."""

    def _make_es_service(self, existing_indices=None):
        """Create a mock ElasticsearchService with a mock client."""
        existing = existing_indices or set()
        es_service = MagicMock()
        client = MagicMock()
        client.indices.exists.side_effect = lambda index: index in existing
        es_service.client = client
        type(es_service).is_serverless = PropertyMock(return_value=False)
        return es_service

    def _patch_es_module(self):
        """Stub the ES service module to avoid real connections."""
        fake_module = MagicMock()
        fake_module.ElasticsearchService = MagicMock()
        fake_module.ElasticsearchService.strip_serverless_incompatible_settings = (
            lambda mapping: mapping
        )
        return patch.dict(sys.modules, {"services.elasticsearch_service": fake_module})

    def test_job_priorities_index_created_when_missing(self):
        """setup_overlay_indices creates job_priorities when it doesn't exist."""
        es_service = self._make_es_service()
        with self._patch_es_module():
            setup_overlay_indices(es_service)

        create_calls = es_service.client.indices.create.call_args_list
        created_indices = {c.kwargs["index"] for c in create_calls}
        assert JOB_PRIORITIES_INDEX in created_indices

    def test_job_priorities_index_skipped_when_exists(self):
        """setup_overlay_indices skips job_priorities when it already exists."""
        es_service = self._make_es_service(existing_indices={JOB_PRIORITIES_INDEX})
        with self._patch_es_module():
            setup_overlay_indices(es_service)

        create_calls = es_service.client.indices.create.call_args_list
        created_indices = {c.kwargs.get("index") for c in create_calls}
        assert JOB_PRIORITIES_INDEX not in created_indices

    def test_job_priorities_uses_correct_mapping(self):
        """setup_overlay_indices passes JOB_PRIORITIES_MAPPING for job_priorities."""
        es_service = self._make_es_service()
        with self._patch_es_module():
            setup_overlay_indices(es_service)

        create_calls = es_service.client.indices.create.call_args_list
        for call_obj in create_calls:
            if call_obj.kwargs.get("index") == JOB_PRIORITIES_INDEX:
                assert call_obj.kwargs["body"] == JOB_PRIORITIES_MAPPING
                break
        else:
            pytest.fail("job_priorities index was not created")
