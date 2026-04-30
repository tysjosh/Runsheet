"""
Unit tests for the Approval Queue Service module.

Tests the ApprovalQueueService class including create, approve, reject,
expire_stale, list_pending, impact summary generation, optimistic
concurrency control, and WebSocket broadcasting.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.approval_queue_service import ApprovalQueueService
from Agents.confirmation_protocol import MutationRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    tool_name: str = "cancel_job",
    parameters: dict = None,
    tenant_id: str = "t1",
    agent_id: str = "ai_agent",
) -> MutationRequest:
    """Create a MutationRequest with sensible defaults."""
    return MutationRequest(
        tool_name=tool_name,
        parameters=parameters or {"job_id": "JOB_1", "reason": "delayed"},
        tenant_id=tenant_id,
        agent_id=agent_id,
    )


def _make_risk_level(value: str = "high"):
    """Create a mock risk level with a .value attribute."""
    mock = MagicMock()
    mock.value = value
    return mock


def _make_service(
    search_hits=None,
    get_response=None,
    ws_manager=None,
    activity_log=None,
    confirmation_protocol=None,
) -> ApprovalQueueService:
    """Create an ApprovalQueueService with mocked dependencies."""
    es_service = MagicMock()
    es_service.index_document = AsyncMock(return_value={"result": "created"})
    es_service.update_document = AsyncMock(return_value={"result": "updated"})
    es_service.get_document = AsyncMock(return_value=get_response or {})

    # Default search response
    if search_hits is None:
        search_hits = []
    es_service.search_documents = AsyncMock(
        return_value={
            "hits": {
                "hits": search_hits,
                "total": {"value": len(search_hits)},
            }
        }
    )

    # Raw client for optimistic concurrency
    es_service.client = MagicMock()
    if get_response:
        es_service.client.get = MagicMock(
            return_value={
                "_source": get_response,
                "_seq_no": 1,
                "_primary_term": 1,
            }
        )
    else:
        es_service.client.get = MagicMock(
            return_value={
                "_source": {},
                "_seq_no": 1,
                "_primary_term": 1,
            }
        )
    es_service.client.update = MagicMock(return_value={"result": "updated"})

    if ws_manager is None:
        ws_manager = MagicMock()
        ws_manager.broadcast_approval_event = AsyncMock()

    if activity_log is None:
        activity_log = MagicMock()
        activity_log.log = AsyncMock(return_value="log-123")

    return ApprovalQueueService(
        es_service=es_service,
        ws_manager=ws_manager,
        activity_log_service=activity_log,
        confirmation_protocol=confirmation_protocol,
    )


# ---------------------------------------------------------------------------
# Tests: create
# ---------------------------------------------------------------------------


class TestCreate:
    """Tests for creating pending approval entries."""

    async def test_create_returns_uuid_string(self):
        service = _make_service()
        request = _make_request()
        action_id = await service.create(request, _make_risk_level("high"))

        assert isinstance(action_id, str)
        assert len(action_id) == 36  # UUID format

    async def test_create_stores_document_in_es(self):
        service = _make_service()
        request = _make_request(tool_name="cancel_job", tenant_id="t1")
        action_id = await service.create(request, _make_risk_level("high"))

        service._es.index_document.assert_called_once()
        call_args = service._es.index_document.call_args
        assert call_args[0][0] == "agent_approval_queue"
        assert call_args[0][1] == action_id

        doc = call_args[0][2]
        assert doc["action_id"] == action_id
        assert doc["tool_name"] == "cancel_job"
        assert doc["status"] == "pending"
        assert doc["risk_level"] == "high"
        assert doc["tenant_id"] == "t1"
        assert doc["proposed_by"] == "ai_agent"
        assert doc["reviewed_by"] is None
        assert doc["reviewed_at"] is None

    async def test_create_sets_expiry_time(self):
        service = _make_service()
        request = _make_request()
        await service.create(request, _make_risk_level("high"), expiry_minutes=30)

        doc = service._es.index_document.call_args[0][2]
        proposed_at = datetime.fromisoformat(doc["proposed_at"])
        expiry_time = datetime.fromisoformat(doc["expiry_time"])
        delta = expiry_time - proposed_at
        # Should be approximately 30 minutes
        assert 29 <= delta.total_seconds() / 60 <= 31

    async def test_create_default_expiry_is_60_minutes(self):
        service = _make_service()
        request = _make_request()
        await service.create(request, _make_risk_level("high"))

        doc = service._es.index_document.call_args[0][2]
        proposed_at = datetime.fromisoformat(doc["proposed_at"])
        expiry_time = datetime.fromisoformat(doc["expiry_time"])
        delta = expiry_time - proposed_at
        assert 59 <= delta.total_seconds() / 60 <= 61

    async def test_create_broadcasts_approval_created(self):
        service = _make_service()
        request = _make_request()
        await service.create(request, _make_risk_level("high"))

        service._ws.broadcast_approval_event.assert_called_once()
        call_args = service._ws.broadcast_approval_event.call_args
        assert call_args[0][0] == "approval_created"
        assert call_args[0][1]["status"] == "pending"

    async def test_create_generates_impact_summary(self):
        service = _make_service()
        request = _make_request(
            tool_name="cancel_job",
            parameters={"job_id": "JOB_42", "reason": "weather"},
        )
        await service.create(request, _make_risk_level("high"))

        doc = service._es.index_document.call_args[0][2]
        assert "JOB_42" in doc["impact_summary"]

    async def test_create_stores_parameters(self):
        params = {"job_id": "JOB_1", "reason": "test"}
        service = _make_service()
        request = _make_request(parameters=params)
        await service.create(request, _make_risk_level("high"))

        doc = service._es.index_document.call_args[0][2]
        assert doc["parameters"] == params

    async def test_create_handles_risk_level_enum(self):
        """Risk level with .value attribute should be stored as string."""
        service = _make_service()
        request = _make_request()
        risk = _make_risk_level("medium")
        await service.create(request, risk)

        doc = service._es.index_document.call_args[0][2]
        assert doc["risk_level"] == "medium"


# ---------------------------------------------------------------------------
# Tests: approve
# ---------------------------------------------------------------------------


class TestApprove:
    """Tests for approving pending actions."""

    def _pending_entry(self, action_id="action-1", tool_name="cancel_job"):
        return {
            "action_id": action_id,
            "action_type": "mutation",
            "tool_name": tool_name,
            "parameters": {"job_id": "JOB_1"},
            "risk_level": "high",
            "proposed_by": "ai_agent",
            "proposed_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "impact_summary": "Cancel job JOB_1",
            "tenant_id": "t1",
        }

    async def test_approve_updates_status_to_approved(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry)
        result = await service.approve("action-1", "reviewer-1")

        assert result["status"] == "approved"
        assert result["reviewed_by"] == "reviewer-1"
        assert result["reviewed_at"] is not None

    async def test_approve_uses_optimistic_concurrency(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry)
        await service.approve("action-1", "reviewer-1")

        # Should use client.update with if_seq_no and if_primary_term
        service._es.client.update.assert_called()
        call_kwargs = service._es.client.update.call_args[1]
        assert call_kwargs["if_seq_no"] == 1
        assert call_kwargs["if_primary_term"] == 1

    async def test_approve_broadcasts_approval_approved(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry)
        await service.approve("action-1", "reviewer-1")

        # At least one broadcast for "approval_approved"
        calls = service._ws.broadcast_approval_event.call_args_list
        event_types = [c[0][0] for c in calls]
        assert "approval_approved" in event_types

    async def test_approve_rejects_non_pending_entry(self):
        entry = self._pending_entry()
        entry["status"] = "rejected"
        service = _make_service(get_response=entry)

        with pytest.raises(ValueError, match="expected 'pending'"):
            await service.approve("action-1", "reviewer-1")

    async def test_approve_rejects_expired_entry(self):
        entry = self._pending_entry()
        entry["status"] = "expired"
        service = _make_service(get_response=entry)

        with pytest.raises(ValueError, match="expected 'pending'"):
            await service.approve("action-1", "reviewer-1")

    async def test_approve_executes_mutation_when_protocol_wired(self):
        entry = self._pending_entry()
        protocol = MagicMock()
        protocol._execute_mutation = AsyncMock(return_value="Executed successfully")
        service = _make_service(
            get_response=entry, confirmation_protocol=protocol
        )
        result = await service.approve("action-1", "reviewer-1")

        protocol._execute_mutation.assert_called_once()
        assert result["status"] == "executed"
        assert result["execution_result"]["success"] is True

    async def test_approve_stores_execution_failure(self):
        entry = self._pending_entry()
        protocol = MagicMock()
        protocol._execute_mutation = AsyncMock(
            side_effect=Exception("ES write failed")
        )
        service = _make_service(
            get_response=entry, confirmation_protocol=protocol
        )
        result = await service.approve("action-1", "reviewer-1")

        assert result["status"] == "executed"
        assert result["execution_result"]["success"] is False
        assert "ES write failed" in result["execution_result"]["error"]

    async def test_approve_without_protocol_does_not_execute(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry, confirmation_protocol=None)
        result = await service.approve("action-1", "reviewer-1")

        assert result["status"] == "approved"
        assert "execution_result" not in result


# ---------------------------------------------------------------------------
# Tests: reject
# ---------------------------------------------------------------------------


class TestReject:
    """Tests for rejecting pending actions."""

    def _pending_entry(self, action_id="action-1"):
        return {
            "action_id": action_id,
            "action_type": "mutation",
            "tool_name": "reassign_rider",
            "parameters": {"shipment_id": "S-1", "new_rider_id": "R-2"},
            "risk_level": "high",
            "proposed_by": "ai_agent",
            "proposed_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "impact_summary": "Reassign shipment S-1 to rider R-2",
            "tenant_id": "t1",
        }

    async def test_reject_updates_status_to_rejected(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry)
        result = await service.reject("action-1", "reviewer-1", reason="Not needed")

        assert result["status"] == "rejected"
        assert result["reviewed_by"] == "reviewer-1"
        assert result["reviewed_at"] is not None
        assert result["rejection_reason"] == "Not needed"

    async def test_reject_broadcasts_approval_rejected(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry)
        await service.reject("action-1", "reviewer-1", reason="Wrong rider")

        calls = service._ws.broadcast_approval_event.call_args_list
        event_types = [c[0][0] for c in calls]
        assert "approval_rejected" in event_types

    async def test_reject_logs_to_activity_log(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry)
        await service.reject("action-1", "reviewer-1", reason="Bad idea")

        service._activity_log.log.assert_called_once()
        log_entry = service._activity_log.log.call_args[0][0]
        assert log_entry["action_type"] == "approval_rejected"
        assert log_entry["outcome"] == "rejected"
        assert log_entry["user_id"] == "reviewer-1"
        assert log_entry["details"]["reason"] == "Bad idea"

    async def test_reject_rejects_non_pending_entry(self):
        entry = self._pending_entry()
        entry["status"] = "approved"
        service = _make_service(get_response=entry)

        with pytest.raises(ValueError, match="expected 'pending'"):
            await service.reject("action-1", "reviewer-1")

    async def test_reject_with_empty_reason(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry)
        result = await service.reject("action-1", "reviewer-1")

        assert result["rejection_reason"] == ""

    async def test_reject_uses_optimistic_concurrency(self):
        entry = self._pending_entry()
        service = _make_service(get_response=entry)
        await service.reject("action-1", "reviewer-1")

        call_kwargs = service._es.client.update.call_args[1]
        assert call_kwargs["if_seq_no"] == 1
        assert call_kwargs["if_primary_term"] == 1


# ---------------------------------------------------------------------------
# Tests: expire_stale
# ---------------------------------------------------------------------------


class TestExpireStale:
    """Tests for expiring stale approval entries."""

    def _expired_hit(self, action_id="action-1", tenant_id="t1"):
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        return {
            "_source": {
                "action_id": action_id,
                "tool_name": "cancel_job",
                "parameters": {"job_id": "JOB_1"},
                "risk_level": "high",
                "proposed_by": "ai_agent",
                "proposed_at": past,
                "status": "pending",
                "expiry_time": past,
                "tenant_id": tenant_id,
            }
        }

    async def test_expire_stale_returns_count(self):
        hits = [self._expired_hit("a-1"), self._expired_hit("a-2")]
        service = _make_service(search_hits=hits)
        count = await service.expire_stale()

        assert count == 2

    async def test_expire_stale_updates_status_to_expired(self):
        hits = [self._expired_hit("a-1")]
        service = _make_service(search_hits=hits)
        await service.expire_stale()

        service._es.update_document.assert_called_once()
        call_args = service._es.update_document.call_args
        assert call_args[0][0] == "agent_approval_queue"
        assert call_args[0][1] == "a-1"
        assert call_args[0][2]["status"] == "expired"

    async def test_expire_stale_broadcasts_approval_expired(self):
        hits = [self._expired_hit("a-1")]
        service = _make_service(search_hits=hits)
        await service.expire_stale()

        calls = service._ws.broadcast_approval_event.call_args_list
        event_types = [c[0][0] for c in calls]
        assert "approval_expired" in event_types

    async def test_expire_stale_logs_to_activity_log(self):
        hits = [self._expired_hit("a-1")]
        service = _make_service(search_hits=hits)
        await service.expire_stale()

        service._activity_log.log.assert_called_once()
        log_entry = service._activity_log.log.call_args[0][0]
        assert log_entry["action_type"] == "approval_expired"
        assert log_entry["outcome"] == "expired"

    async def test_expire_stale_returns_zero_when_none_expired(self):
        service = _make_service(search_hits=[])
        count = await service.expire_stale()

        assert count == 0

    async def test_expire_stale_queries_pending_with_past_expiry(self):
        service = _make_service(search_hits=[])
        await service.expire_stale()

        service._es.search_documents.assert_called_once()
        call_args = service._es.search_documents.call_args
        query = call_args[0][1]
        must_clauses = query["query"]["bool"]["must"]
        assert {"term": {"status": "pending"}} in must_clauses
        # Second clause should be a range on expiry_time
        range_clause = must_clauses[1]
        assert "range" in range_clause
        assert "expiry_time" in range_clause["range"]

    async def test_expire_stale_continues_on_individual_failure(self):
        """If one entry fails to expire, others should still be processed."""
        hits = [self._expired_hit("a-1"), self._expired_hit("a-2")]
        service = _make_service(search_hits=hits)
        # First call fails, second succeeds
        service._es.update_document = AsyncMock(
            side_effect=[Exception("ES error"), {"result": "updated"}]
        )
        count = await service.expire_stale()

        assert count == 1


# ---------------------------------------------------------------------------
# Tests: list_pending
# ---------------------------------------------------------------------------


class TestListPending:
    """Tests for listing pending approvals."""

    def _pending_hit(self, action_id, proposed_at):
        return {
            "_source": {
                "action_id": action_id,
                "tool_name": "cancel_job",
                "status": "pending",
                "proposed_at": proposed_at,
                "tenant_id": "t1",
            }
        }

    async def test_list_pending_returns_items(self):
        now = datetime.now(timezone.utc)
        hits = [
            self._pending_hit("a-1", now.isoformat()),
            self._pending_hit("a-2", (now - timedelta(minutes=5)).isoformat()),
        ]
        service = _make_service(search_hits=hits)
        result = await service.list_pending("t1")

        assert len(result["items"]) == 2
        assert result["total"] == 2
        assert result["page"] == 1
        assert result["size"] == 20

    async def test_list_pending_filters_by_tenant(self):
        service = _make_service(search_hits=[])
        await service.list_pending("tenant-abc")

        query = service._es.search_documents.call_args[0][1]
        must_clauses = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": "tenant-abc"}} in must_clauses

    async def test_list_pending_filters_by_status_pending(self):
        service = _make_service(search_hits=[])
        await service.list_pending("t1")

        query = service._es.search_documents.call_args[0][1]
        must_clauses = query["query"]["bool"]["must"]
        assert {"term": {"status": "pending"}} in must_clauses

    async def test_list_pending_sorts_by_proposed_at_desc(self):
        service = _make_service(search_hits=[])
        await service.list_pending("t1")

        query = service._es.search_documents.call_args[0][1]
        assert query["sort"] == [{"proposed_at": {"order": "desc"}}]

    async def test_list_pending_pagination(self):
        service = _make_service(search_hits=[])
        await service.list_pending("t1", page=3, size=10)

        query = service._es.search_documents.call_args[0][1]
        assert query["from"] == 20  # (3-1) * 10
        assert query["size"] == 10

    async def test_list_pending_returns_empty_for_no_results(self):
        service = _make_service(search_hits=[])
        result = await service.list_pending("t1")

        assert result["items"] == []
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Tests: _generate_impact_summary
# ---------------------------------------------------------------------------


class TestGenerateImpactSummary:
    """Tests for the impact summary generation."""

    def test_cancel_job_summary(self):
        service = _make_service()
        request = _make_request(
            tool_name="cancel_job",
            parameters={"job_id": "JOB_42", "reason": "weather delay"},
        )
        summary = service._generate_impact_summary(request)

        assert "JOB_42" in summary
        assert "Cancel" in summary

    def test_reassign_rider_summary(self):
        service = _make_service()
        request = _make_request(
            tool_name="reassign_rider",
            parameters={"shipment_id": "S-100", "new_rider_id": "R-5"},
        )
        summary = service._generate_impact_summary(request)

        assert "S-100" in summary
        assert "R-5" in summary

    def test_assign_asset_summary(self):
        service = _make_service()
        request = _make_request(
            tool_name="assign_asset_to_job",
            parameters={"asset_id": "A-1", "job_id": "JOB_1"},
        )
        summary = service._generate_impact_summary(request)

        assert "A-1" in summary
        assert "JOB_1" in summary

    def test_request_fuel_refill_summary(self):
        service = _make_service()
        request = _make_request(
            tool_name="request_fuel_refill",
            parameters={"station_id": "S-12", "quantity_liters": 500},
        )
        summary = service._generate_impact_summary(request)

        assert "S-12" in summary
        assert "500" in summary

    def test_unknown_tool_fallback_summary(self):
        service = _make_service()
        request = _make_request(
            tool_name="some_unknown_tool",
            parameters={"key": "value"},
        )
        summary = service._generate_impact_summary(request)

        assert "some_unknown_tool" in summary


# ---------------------------------------------------------------------------
# Tests: WebSocket broadcasting
# ---------------------------------------------------------------------------


class TestWebSocketBroadcasting:
    """Tests for WebSocket event broadcasting."""

    async def test_broadcast_silently_handles_ws_error(self):
        ws = MagicMock()
        ws.broadcast_approval_event = AsyncMock(
            side_effect=Exception("WS connection lost")
        )
        service = _make_service(ws_manager=ws)
        request = _make_request()

        # Should not raise
        await service.create(request, _make_risk_level("high"))

    async def test_broadcast_skipped_when_no_ws_manager(self):
        service = _make_service()
        service._ws = None
        request = _make_request()

        # Should not raise
        action_id = await service.create(request, _make_risk_level("high"))
        assert action_id is not None


# ---------------------------------------------------------------------------
# Tests: Concurrency control
# ---------------------------------------------------------------------------


class TestConcurrencyControl:
    """Tests for ES optimistic concurrency on approve/reject."""

    async def test_version_conflict_raises_runtime_error(self):
        entry = {
            "action_id": "action-1",
            "status": "pending",
            "tool_name": "cancel_job",
            "parameters": {},
            "proposed_by": "ai_agent",
            "tenant_id": "t1",
        }
        service = _make_service(get_response=entry)
        service._es.client.update = MagicMock(
            side_effect=Exception("version_conflict_engine_exception")
        )

        with pytest.raises(RuntimeError, match="Concurrent modification"):
            await service.approve("action-1", "reviewer-1")

    async def test_non_conflict_error_propagates(self):
        entry = {
            "action_id": "action-1",
            "status": "pending",
            "tool_name": "cancel_job",
            "parameters": {},
            "proposed_by": "ai_agent",
            "tenant_id": "t1",
        }
        service = _make_service(get_response=entry)
        service._es.client.update = MagicMock(
            side_effect=Exception("connection_timeout")
        )

        with pytest.raises(Exception, match="connection_timeout"):
            await service.approve("action-1", "reviewer-1")
