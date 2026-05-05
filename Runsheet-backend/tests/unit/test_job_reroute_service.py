"""
Unit tests for JobRerouteService and REST endpoint.

Tests job rerouting logic including 404 for missing jobs, 400 for invalid
statuses, MutationRequest creation, event appending, WebSocket broadcasting,
and REST route configuration.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.confirmation_protocol import MutationRequest, MutationResult
from errors.codes import ErrorCode
from errors.exceptions import AppException
from scheduling.services.job_reroute_service import JobRerouteService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_es_service(job_doc=None):
    """Create a mocked ES service.

    If job_doc is provided, search_documents returns it as a hit.
    If None, search_documents returns empty hits (job not found).
    """
    es = MagicMock()
    if job_doc is not None:
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job_doc}]}}
        )
    else:
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
    es.update_document = AsyncMock()
    es.index_document = AsyncMock()
    return es


def _make_confirmation_protocol(executed=True):
    """Create a mocked ConfirmationProtocol."""
    cp = MagicMock()
    cp.process_mutation = AsyncMock(
        return_value=MutationResult(
            executed=executed,
            approval_id="approval-123" if not executed else None,
            risk_level="medium",
            result="Successfully executed reroute_job" if executed else None,
            confirmation_method="immediate" if executed else "approval_queue",
        )
    )
    return cp


def _make_service(job_doc=None, executed=True):
    """Create a JobRerouteService with mocked dependencies."""
    es = _make_es_service(job_doc)
    cp = _make_confirmation_protocol(executed)
    svc = JobRerouteService(es_service=es, confirmation_protocol=cp)
    svc._ws_manager = MagicMock()
    svc._ws_manager.broadcast = AsyncMock()
    return svc


def _job_doc(
    job_id="JOB-001",
    tenant_id="default",
    status="assigned",
    destination="Warehouse A",
):
    """Create a sample job document."""
    return {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "status": status,
        "destination": destination,
        "job_type": "cargo_transport",
        "origin": "Depot B",
        "priority": "normal",
        "scheduled_time": "2025-01-15T08:00:00Z",
        "created_at": "2025-01-14T10:00:00Z",
        "updated_at": "2025-01-14T10:00:00Z",
    }


# ---------------------------------------------------------------------------
# Tests: reroute_job returns 404 for missing job (Req 2.6)
# ---------------------------------------------------------------------------


class TestRerouteJobNotFound:
    """Tests that reroute_job raises 404 for missing or wrong-tenant jobs."""

    @pytest.mark.asyncio
    async def test_raises_404_for_missing_job(self):
        """reroute_job raises AppException with 404 when job not found."""
        svc = _make_service(job_doc=None)

        with pytest.raises(AppException) as exc_info:
            await svc.reroute_job(
                job_id="NONEXISTENT",
                new_destination="New Place",
                tenant_id="default",
                reason="test",
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.error_code == ErrorCode.RESOURCE_NOT_FOUND

    @pytest.mark.asyncio
    async def test_404_message_contains_job_id(self):
        """404 error message includes the job_id."""
        svc = _make_service(job_doc=None)

        with pytest.raises(AppException) as exc_info:
            await svc.reroute_job(
                job_id="JOB-MISSING",
                new_destination="New Place",
                tenant_id="default",
                reason="test",
            )

        assert "JOB-MISSING" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_404_does_not_call_confirmation_protocol(self):
        """ConfirmationProtocol is not called when job is not found."""
        svc = _make_service(job_doc=None)

        with pytest.raises(AppException):
            await svc.reroute_job(
                job_id="NONEXISTENT",
                new_destination="New Place",
                tenant_id="default",
                reason="test",
            )

        svc._confirmation_protocol.process_mutation.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: reroute_job returns 400 for invalid status (Req 2.7)
# ---------------------------------------------------------------------------


class TestRerouteJobInvalidStatus:
    """Tests that reroute_job raises 400 for non-reroutable statuses."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["scheduled", "completed", "cancelled", "failed"])
    async def test_raises_400_for_invalid_status(self, status):
        """reroute_job raises AppException with 400 for status '{status}'."""
        doc = _job_doc(status=status)
        svc = _make_service(job_doc=doc)

        with pytest.raises(AppException) as exc_info:
            await svc.reroute_job(
                job_id="JOB-001",
                new_destination="New Place",
                tenant_id="default",
                reason="test",
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR

    @pytest.mark.asyncio
    async def test_400_message_contains_current_status(self):
        """400 error message includes the current job status."""
        doc = _job_doc(status="completed")
        svc = _make_service(job_doc=doc)

        with pytest.raises(AppException) as exc_info:
            await svc.reroute_job(
                job_id="JOB-001",
                new_destination="New Place",
                tenant_id="default",
                reason="test",
            )

        assert "completed" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_400_does_not_call_confirmation_protocol(self):
        """ConfirmationProtocol is not called when status is invalid."""
        doc = _job_doc(status="scheduled")
        svc = _make_service(job_doc=doc)

        with pytest.raises(AppException):
            await svc.reroute_job(
                job_id="JOB-001",
                new_destination="New Place",
                tenant_id="default",
                reason="test",
            )

        svc._confirmation_protocol.process_mutation.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: reroute_job accepts assigned and in_progress jobs (Req 2.1)
# ---------------------------------------------------------------------------


class TestRerouteJobValidStatus:
    """Tests that reroute_job accepts jobs with assigned or in_progress status."""

    @pytest.mark.asyncio
    async def test_accepts_assigned_status(self):
        """reroute_job succeeds for a job with status 'assigned'."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        result = await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="breakdown nearby",
        )

        assert result["mutation_result"]["executed"] is True

    @pytest.mark.asyncio
    async def test_accepts_in_progress_status(self):
        """reroute_job succeeds for a job with status 'in_progress'."""
        doc = _job_doc(status="in_progress")
        svc = _make_service(job_doc=doc)

        result = await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="alternate fuel station",
        )

        assert result["mutation_result"]["executed"] is True


# ---------------------------------------------------------------------------
# Tests: reroute_job creates MutationRequest with correct parameters (Req 2.2)
# ---------------------------------------------------------------------------


class TestRerouteJobMutationRequest:
    """Tests that reroute_job creates a MutationRequest with correct parameters."""

    @pytest.mark.asyncio
    async def test_mutation_request_tool_name(self):
        """MutationRequest has tool_name='reroute_job'."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        svc._confirmation_protocol.process_mutation.assert_called_once()
        request = svc._confirmation_protocol.process_mutation.call_args[0][0]
        assert isinstance(request, MutationRequest)
        assert request.tool_name == "reroute_job"

    @pytest.mark.asyncio
    async def test_mutation_request_contains_job_id(self):
        """MutationRequest parameters contain the job_id."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        request = svc._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.parameters["job_id"] == "JOB-001"

    @pytest.mark.asyncio
    async def test_mutation_request_contains_new_destination(self):
        """MutationRequest parameters contain the new_destination."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="Fuel Station X",
            tenant_id="default",
            reason="test",
        )

        request = svc._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.parameters["new_destination"] == "Fuel Station X"

    @pytest.mark.asyncio
    async def test_mutation_request_contains_tenant_id(self):
        """MutationRequest parameters contain the tenant_id."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="acme-corp",
            reason="test",
        )

        request = svc._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.parameters["tenant_id"] == "acme-corp"

    @pytest.mark.asyncio
    async def test_mutation_request_contains_reason(self):
        """MutationRequest parameters contain the reason."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="breakdown nearby",
        )

        request = svc._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.parameters["reason"] == "breakdown nearby"

    @pytest.mark.asyncio
    async def test_mutation_request_agent_id(self):
        """MutationRequest has agent_id='job_reroute_service'."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        request = svc._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.agent_id == "job_reroute_service"

    @pytest.mark.asyncio
    async def test_mutation_request_includes_destination_location(self):
        """MutationRequest includes new_destination_location when provided."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
            new_destination_location={"lat": 37.77, "lng": -122.42},
        )

        request = svc._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.parameters["new_destination_location"] == {
            "lat": 37.77,
            "lng": -122.42,
        }


# ---------------------------------------------------------------------------
# Tests: reroute_job appends job_rerouted event (Req 2.4)
# ---------------------------------------------------------------------------


class TestRerouteJobEvent:
    """Tests that reroute_job appends a job_rerouted event to job_events."""

    @pytest.mark.asyncio
    async def test_appends_event_to_job_events(self):
        """reroute_job indexes a job_rerouted event document."""
        doc = _job_doc(status="assigned", destination="Old Warehouse")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Warehouse",
            tenant_id="default",
            reason="breakdown",
        )

        svc._es.index_document.assert_called_once()
        call_args = svc._es.index_document.call_args
        index_name = call_args[0][0]
        event_doc = call_args[0][2]

        assert index_name == "job_events"
        assert event_doc["event_type"] == "job_rerouted"
        assert event_doc["job_id"] == "JOB-001"

    @pytest.mark.asyncio
    async def test_event_contains_old_destination(self):
        """Event payload contains the old_destination."""
        doc = _job_doc(status="assigned", destination="Old Warehouse")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Warehouse",
            tenant_id="default",
            reason="breakdown",
        )

        event_doc = svc._es.index_document.call_args[0][2]
        assert event_doc["event_payload"]["old_destination"] == "Old Warehouse"

    @pytest.mark.asyncio
    async def test_event_contains_new_destination(self):
        """Event payload contains the new_destination."""
        doc = _job_doc(status="assigned", destination="Old Warehouse")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Warehouse",
            tenant_id="default",
            reason="breakdown",
        )

        event_doc = svc._es.index_document.call_args[0][2]
        assert event_doc["event_payload"]["new_destination"] == "New Warehouse"

    @pytest.mark.asyncio
    async def test_event_contains_reason(self):
        """Event payload contains the reason."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="alternate fuel station",
        )

        event_doc = svc._es.index_document.call_args[0][2]
        assert event_doc["event_payload"]["reason"] == "alternate fuel station"

    @pytest.mark.asyncio
    async def test_event_contains_actor_id(self):
        """Event payload contains the actor_id."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
            actor_id="user-42",
        )

        event_doc = svc._es.index_document.call_args[0][2]
        assert event_doc["event_payload"]["actor_id"] == "user-42"

    @pytest.mark.asyncio
    async def test_event_contains_tenant_id(self):
        """Event document contains the tenant_id."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="acme-corp",
            reason="test",
        )

        event_doc = svc._es.index_document.call_args[0][2]
        assert event_doc["tenant_id"] == "acme-corp"


# ---------------------------------------------------------------------------
# Tests: reroute_job broadcasts job_rerouted WebSocket event (Req 2.5)
# ---------------------------------------------------------------------------


class TestRerouteJobWebSocket:
    """Tests that reroute_job broadcasts a job_rerouted WebSocket event."""

    @pytest.mark.asyncio
    async def test_broadcasts_job_rerouted_event(self):
        """reroute_job broadcasts a 'job_rerouted' event via WebSocket."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        svc._ws_manager.broadcast.assert_called_once()
        event_type = svc._ws_manager.broadcast.call_args[0][0]
        assert event_type == "job_rerouted"

    @pytest.mark.asyncio
    async def test_ws_event_contains_updated_destination(self):
        """WebSocket event data contains the updated destination."""
        doc = _job_doc(status="assigned", destination="Old Place")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        event_data = svc._ws_manager.broadcast.call_args[0][1]
        assert event_data["destination"] == "New Place"

    @pytest.mark.asyncio
    async def test_ws_event_contains_job_id(self):
        """WebSocket event data contains the job_id."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        event_data = svc._ws_manager.broadcast.call_args[0][1]
        assert event_data["job_id"] == "JOB-001"

    @pytest.mark.asyncio
    async def test_no_ws_broadcast_when_manager_is_none(self):
        """reroute_job does not crash when ws_manager is None."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)
        svc._ws_manager = None

        # Should not raise
        result = await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        assert result["mutation_result"]["executed"] is True

    @pytest.mark.asyncio
    async def test_no_ws_broadcast_when_not_executed(self):
        """WebSocket is not broadcast when ConfirmationProtocol rejects."""
        doc = _job_doc(status="assigned")
        es = _make_es_service(doc)
        cp = _make_confirmation_protocol(executed=False)
        svc = JobRerouteService(es_service=es, confirmation_protocol=cp)
        svc._ws_manager = MagicMock()
        svc._ws_manager.broadcast = AsyncMock()

        result = await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        assert result["mutation_result"]["executed"] is False
        svc._ws_manager.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: REST endpoint configuration and wiring (Req 2.8)
# ---------------------------------------------------------------------------


class TestRESTEndpoint:
    """Tests for the REST endpoint route configuration."""

    def test_router_has_post_reroute_route(self):
        """Router includes a POST route for /jobs/{job_id}/reroute."""
        from scheduling.routes.job_reroute_routes import router

        route_paths = [r.path for r in router.routes]
        assert "/api/v1/scheduling/jobs/{job_id}/reroute" in route_paths

    def test_router_prefix(self):
        """Router has the correct prefix."""
        from scheduling.routes.job_reroute_routes import router

        assert router.prefix == "/api/v1/scheduling"

    def test_configure_wires_service(self):
        """configure_job_reroute_routes wires the service reference."""
        from scheduling.routes import job_reroute_routes
        from scheduling.routes.job_reroute_routes import (
            configure_job_reroute_routes,
            _get_job_reroute_service,
        )

        mock_svc = MagicMock(spec=JobRerouteService)
        configure_job_reroute_routes(job_reroute_service=mock_svc)

        result = _get_job_reroute_service()
        assert result is mock_svc

        # Clean up module state
        job_reroute_routes._job_reroute_service = None

    def test_get_service_raises_when_not_configured(self):
        """_get_job_reroute_service raises RuntimeError when not configured."""
        from scheduling.routes import job_reroute_routes
        from scheduling.routes.job_reroute_routes import _get_job_reroute_service

        # Ensure it's not configured
        job_reroute_routes._job_reroute_service = None

        with pytest.raises(RuntimeError, match="not configured"):
            _get_job_reroute_service()

    def test_router_auth_policy(self):
        """Router declares jwt_required auth policy."""
        from scheduling.routes.job_reroute_routes import ROUTER_AUTH_POLICY

        assert ROUTER_AUTH_POLICY == "jwt_required"

    def test_reroute_route_methods(self):
        """The reroute route accepts POST method."""
        from scheduling.routes.job_reroute_routes import router

        for route in router.routes:
            if hasattr(route, "path") and route.path == "/api/v1/scheduling/jobs/{job_id}/reroute":
                assert "POST" in route.methods
                break
        else:
            pytest.fail("Route /api/v1/scheduling/jobs/{job_id}/reroute not found")


# ---------------------------------------------------------------------------
# Tests: reroute_job updates job document on approval (Req 2.3)
# ---------------------------------------------------------------------------


class TestRerouteJobDocumentUpdate:
    """Tests that reroute_job updates the job document when approved."""

    @pytest.mark.asyncio
    async def test_updates_destination_in_es(self):
        """reroute_job updates the destination field in ES."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Warehouse",
            tenant_id="default",
            reason="test",
        )

        svc._es.update_document.assert_called_once()
        call_args = svc._es.update_document.call_args
        index_name = call_args[0][0]
        doc_id = call_args[0][1]
        update_fields = call_args[0][2]

        assert index_name == "jobs_current"
        assert doc_id == "JOB-001"
        assert update_fields["destination"] == "New Warehouse"

    @pytest.mark.asyncio
    async def test_updates_updated_at_timestamp(self):
        """reroute_job sets updated_at in the ES update."""
        doc = _job_doc(status="assigned")
        svc = _make_service(job_doc=doc)

        await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        update_fields = svc._es.update_document.call_args[0][2]
        assert "updated_at" in update_fields

    @pytest.mark.asyncio
    async def test_no_es_update_when_not_executed(self):
        """ES update is not called when ConfirmationProtocol rejects."""
        doc = _job_doc(status="assigned")
        es = _make_es_service(doc)
        cp = _make_confirmation_protocol(executed=False)
        svc = JobRerouteService(es_service=es, confirmation_protocol=cp)
        svc._ws_manager = MagicMock()
        svc._ws_manager.broadcast = AsyncMock()

        result = await svc.reroute_job(
            job_id="JOB-001",
            new_destination="New Place",
            tenant_id="default",
            reason="test",
        )

        assert result["mutation_result"]["executed"] is False
        es.update_document.assert_not_called()
