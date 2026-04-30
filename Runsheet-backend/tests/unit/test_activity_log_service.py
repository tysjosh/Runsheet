"""
Unit tests for the Activity Log Service module.

Tests the ActivityLogService class including log, log_mutation,
log_monitoring_cycle, log_tool_invocation, query, and get_stats methods.

Requirements: 1.8, 8.1, 8.2, 8.3, 8.6, 8.7
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.activity_log_service import ActivityLogService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    search_response=None,
    ws_manager=None,
) -> ActivityLogService:
    """Create an ActivityLogService with mocked dependencies."""
    es_service = MagicMock()
    es_service.index_document = AsyncMock(return_value={"result": "created"})
    es_service.search_documents = AsyncMock(
        return_value=search_response
        or {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {},
        }
    )

    if ws_manager is None:
        ws_manager = MagicMock()
        ws_manager.broadcast_activity = AsyncMock()

    return ActivityLogService(es_service=es_service, ws_manager=ws_manager)


def _make_mutation_request(
    tool_name="cancel_job",
    parameters=None,
    tenant_id="t1",
    agent_id="ai_agent",
    user_id="user-1",
    session_id="sess-1",
):
    """Create a mock MutationRequest."""
    req = MagicMock()
    req.tool_name = tool_name
    req.parameters = parameters or {"job_id": "JOB_1", "reason": "delayed"}
    req.tenant_id = tenant_id
    req.agent_id = agent_id
    req.user_id = user_id
    req.session_id = session_id
    return req


def _make_risk_level(value="high"):
    """Create a mock risk level with a .value attribute."""
    mock = MagicMock()
    mock.value = value
    return mock


# ---------------------------------------------------------------------------
# Tests: log
# ---------------------------------------------------------------------------


class TestLog:
    """Tests for the core log method."""

    async def test_log_returns_uuid_string(self):
        service = _make_service()
        log_id = await service.log({"agent_id": "test", "action_type": "query", "outcome": "success"})

        assert isinstance(log_id, str)
        assert len(log_id) == 36  # UUID format

    async def test_log_adds_log_id_and_timestamp(self):
        service = _make_service()
        entry = {"agent_id": "test", "action_type": "query", "outcome": "success"}
        log_id = await service.log(entry)

        call_args = service._es.index_document.call_args
        doc = call_args[0][2]
        assert doc["log_id"] == log_id
        assert "timestamp" in doc

    async def test_log_stores_in_correct_index(self):
        service = _make_service()
        await service.log({"agent_id": "test", "action_type": "query", "outcome": "success"})

        call_args = service._es.index_document.call_args
        assert call_args[0][0] == "agent_activity_log"

    async def test_log_stores_document_with_log_id_as_doc_id(self):
        service = _make_service()
        log_id = await service.log({"agent_id": "test", "action_type": "query", "outcome": "success"})

        call_args = service._es.index_document.call_args
        assert call_args[0][1] == log_id

    async def test_log_preserves_entry_fields(self):
        service = _make_service()
        entry = {
            "agent_id": "fleet_agent",
            "action_type": "mutation",
            "tool_name": "cancel_job",
            "parameters": {"job_id": "JOB_1"},
            "risk_level": "high",
            "outcome": "success",
            "duration_ms": 150.5,
            "tenant_id": "t1",
            "user_id": "user-1",
            "session_id": "sess-1",
            "details": {"confirmation_method": "immediate"},
        }
        await service.log(entry)

        doc = service._es.index_document.call_args[0][2]
        assert doc["agent_id"] == "fleet_agent"
        assert doc["action_type"] == "mutation"
        assert doc["tool_name"] == "cancel_job"
        assert doc["parameters"] == {"job_id": "JOB_1"}
        assert doc["risk_level"] == "high"
        assert doc["outcome"] == "success"
        assert doc["duration_ms"] == 150.5
        assert doc["tenant_id"] == "t1"
        assert doc["user_id"] == "user-1"
        assert doc["session_id"] == "sess-1"
        assert doc["details"]["confirmation_method"] == "immediate"

    async def test_log_broadcasts_via_websocket(self):
        service = _make_service()
        entry = {"agent_id": "test", "action_type": "query", "outcome": "success"}
        await service.log(entry)

        service._ws.broadcast_activity.assert_called_once()
        broadcast_data = service._ws.broadcast_activity.call_args[0][0]
        assert broadcast_data["agent_id"] == "test"
        assert "log_id" in broadcast_data
        assert "timestamp" in broadcast_data

    async def test_log_skips_broadcast_when_no_ws_manager(self):
        service = _make_service()
        service._ws = None
        entry = {"agent_id": "test", "action_type": "query", "outcome": "success"}

        # Should not raise
        log_id = await service.log(entry)
        assert log_id is not None

    async def test_log_handles_ws_broadcast_failure_gracefully(self):
        ws = MagicMock()
        ws.broadcast_activity = AsyncMock(side_effect=Exception("WS connection lost"))
        service = _make_service(ws_manager=ws)
        entry = {"agent_id": "test", "action_type": "query", "outcome": "success"}

        # Should not raise
        log_id = await service.log(entry)
        assert log_id is not None

    async def test_log_generates_unique_ids(self):
        service = _make_service()
        id1 = await service.log({"agent_id": "a", "action_type": "query", "outcome": "success"})
        id2 = await service.log({"agent_id": "b", "action_type": "query", "outcome": "success"})

        assert id1 != id2


# ---------------------------------------------------------------------------
# Tests: log_mutation
# ---------------------------------------------------------------------------


class TestLogMutation:
    """Tests for logging mutations through the Confirmation Protocol."""

    async def test_log_mutation_creates_entry_with_correct_fields(self):
        service = _make_service()
        request = _make_mutation_request()
        risk = _make_risk_level("high")

        log_id = await service.log_mutation(request, risk, "immediate", "Executed successfully")

        assert isinstance(log_id, str)
        doc = service._es.index_document.call_args[0][2]
        assert doc["agent_id"] == "ai_agent"
        assert doc["action_type"] == "mutation"
        assert doc["tool_name"] == "cancel_job"
        assert doc["parameters"] == {"job_id": "JOB_1", "reason": "delayed"}
        assert doc["risk_level"] == "high"
        assert doc["outcome"] == "success"
        assert doc["tenant_id"] == "t1"
        assert doc["user_id"] == "user-1"
        assert doc["session_id"] == "sess-1"
        assert doc["details"]["confirmation_method"] == "immediate"
        assert doc["details"]["result"] == "Executed successfully"

    async def test_log_mutation_outcome_pending_when_no_result(self):
        service = _make_service()
        request = _make_mutation_request()
        risk = _make_risk_level("high")

        await service.log_mutation(request, risk, "approval_queue", None)

        doc = service._es.index_document.call_args[0][2]
        assert doc["outcome"] == "pending_approval"
        assert doc["details"]["confirmation_method"] == "approval_queue"

    async def test_log_mutation_outcome_rejected(self):
        service = _make_service()
        request = _make_mutation_request()
        risk = _make_risk_level("medium")

        await service.log_mutation(request, risk, "rejected", "Validation failed")

        doc = service._es.index_document.call_args[0][2]
        assert doc["outcome"] == "rejected"

    async def test_log_mutation_handles_risk_level_enum(self):
        service = _make_service()
        request = _make_mutation_request()
        risk = _make_risk_level("medium")

        await service.log_mutation(request, risk, "immediate", "OK")

        doc = service._es.index_document.call_args[0][2]
        assert doc["risk_level"] == "medium"

    async def test_log_mutation_handles_risk_level_string(self):
        service = _make_service()
        request = _make_mutation_request()

        await service.log_mutation(request, "low", "immediate", "OK")

        doc = service._es.index_document.call_args[0][2]
        assert doc["risk_level"] == "low"


# ---------------------------------------------------------------------------
# Tests: log_monitoring_cycle
# ---------------------------------------------------------------------------


class TestLogMonitoringCycle:
    """Tests for logging autonomous agent monitoring cycles."""

    async def test_log_monitoring_cycle_creates_entry(self):
        service = _make_service()
        log_id = await service.log_monitoring_cycle(
            agent_id="delay_response_agent",
            detection_count=3,
            action_count=1,
            duration_ms=245.7,
        )

        assert isinstance(log_id, str)
        doc = service._es.index_document.call_args[0][2]
        assert doc["agent_id"] == "delay_response_agent"
        assert doc["action_type"] == "monitoring_cycle"
        assert doc["outcome"] == "success"
        assert doc["duration_ms"] == 245.7
        assert doc["details"]["detection_count"] == 3
        assert doc["details"]["action_count"] == 1

    async def test_log_monitoring_cycle_tool_name_is_none(self):
        service = _make_service()
        await service.log_monitoring_cycle("fuel_agent", 0, 0, 100.0)

        doc = service._es.index_document.call_args[0][2]
        assert doc["tool_name"] is None

    async def test_log_monitoring_cycle_broadcasts_via_ws(self):
        service = _make_service()
        await service.log_monitoring_cycle("sla_guardian", 2, 1, 300.0)

        service._ws.broadcast_activity.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: log_tool_invocation
# ---------------------------------------------------------------------------


class TestLogToolInvocation:
    """Tests for logging tool invocations."""

    async def test_log_tool_invocation_creates_entry(self):
        service = _make_service()
        log_id = await service.log_tool_invocation(
            agent_id="fleet_agent",
            tool_name="search_fleet_data",
            params={"query": "trucks in zone A"},
            outcome="success",
            duration_ms=85.3,
        )

        assert isinstance(log_id, str)
        doc = service._es.index_document.call_args[0][2]
        assert doc["agent_id"] == "fleet_agent"
        assert doc["action_type"] == "tool_invocation"
        assert doc["tool_name"] == "search_fleet_data"
        assert doc["parameters"] == {"query": "trucks in zone A"}
        assert doc["outcome"] == "success"
        assert doc["duration_ms"] == 85.3

    async def test_log_tool_invocation_failure_outcome(self):
        service = _make_service()
        await service.log_tool_invocation(
            agent_id="scheduling_agent",
            tool_name="get_job_details",
            params={"job_id": "JOB_999"},
            outcome="failure",
            duration_ms=50.0,
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["outcome"] == "failure"

    async def test_log_tool_invocation_broadcasts_via_ws(self):
        service = _make_service()
        await service.log_tool_invocation("agent", "tool", {}, "success", 10.0)

        service._ws.broadcast_activity.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: query
# ---------------------------------------------------------------------------


class TestQuery:
    """Tests for querying the activity log."""

    async def test_query_returns_paginated_results(self):
        hits = [
            {"_source": {"log_id": "l-1", "agent_id": "a1", "action_type": "query"}},
            {"_source": {"log_id": "l-2", "agent_id": "a2", "action_type": "mutation"}},
        ]
        response = {"hits": {"hits": hits, "total": {"value": 2}}}
        service = _make_service(search_response=response)

        result = await service.query({})

        assert len(result["items"]) == 2
        assert result["total"] == 2
        assert result["page"] == 1
        assert result["size"] == 50

    async def test_query_filters_by_agent_id(self):
        service = _make_service()
        await service.query({"agent_id": "fleet_agent"})

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"agent_id": "fleet_agent"}} in must

    async def test_query_filters_by_action_type(self):
        service = _make_service()
        await service.query({"action_type": "mutation"})

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"action_type": "mutation"}} in must

    async def test_query_filters_by_tenant_id(self):
        service = _make_service()
        await service.query({"tenant_id": "t1"})

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": "t1"}} in must

    async def test_query_filters_by_outcome(self):
        service = _make_service()
        await service.query({"outcome": "success"})

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"outcome": "success"}} in must

    async def test_query_filters_by_time_range(self):
        service = _make_service()
        await service.query({
            "time_range": {
                "gte": "2024-01-01T00:00:00Z",
                "lte": "2024-01-31T23:59:59Z",
            }
        })

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        range_clause = must[0]
        assert "range" in range_clause
        assert range_clause["range"]["timestamp"]["gte"] == "2024-01-01T00:00:00Z"
        assert range_clause["range"]["timestamp"]["lte"] == "2024-01-31T23:59:59Z"

    async def test_query_combines_multiple_filters(self):
        service = _make_service()
        await service.query({
            "agent_id": "fleet_agent",
            "action_type": "mutation",
            "tenant_id": "t1",
            "outcome": "success",
        })

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert len(must) == 4

    async def test_query_no_filters_uses_match_all(self):
        service = _make_service()
        await service.query({})

        query = service._es.search_documents.call_args[0][1]
        assert "match_all" in query["query"]

    async def test_query_sorts_by_timestamp_desc(self):
        service = _make_service()
        await service.query({})

        query = service._es.search_documents.call_args[0][1]
        assert query["sort"] == [{"timestamp": {"order": "desc"}}]

    async def test_query_pagination(self):
        service = _make_service()
        await service.query({}, page=3, size=10)

        query = service._es.search_documents.call_args[0][1]
        assert query["from"] == 20  # (3-1) * 10
        assert query["size"] == 10

    async def test_query_returns_empty_for_no_results(self):
        service = _make_service()
        result = await service.query({"agent_id": "nonexistent"})

        assert result["items"] == []
        assert result["total"] == 0

    async def test_query_handles_total_as_integer(self):
        """Some ES versions return total as an integer instead of dict."""
        response = {"hits": {"hits": [], "total": 5}}
        service = _make_service(search_response=response)

        result = await service.query({})
        assert result["total"] == 5


# ---------------------------------------------------------------------------
# Tests: get_stats
# ---------------------------------------------------------------------------


class TestGetStats:
    """Tests for aggregated statistics."""

    def _make_stats_response(
        self,
        total=100,
        agent_buckets=None,
        outcome_buckets=None,
        avg_duration=150.5,
    ):
        if agent_buckets is None:
            agent_buckets = [
                {"key": "fleet_agent", "doc_count": 40},
                {"key": "scheduling_agent", "doc_count": 60},
            ]
        if outcome_buckets is None:
            outcome_buckets = [
                {"key": "success", "doc_count": 85},
                {"key": "failure", "doc_count": 10},
                {"key": "pending_approval", "doc_count": 5},
            ]
        return {
            "hits": {"hits": [], "total": {"value": total}},
            "aggregations": {
                "actions_per_agent": {"buckets": agent_buckets},
                "outcomes": {"buckets": outcome_buckets},
                "avg_duration": {"value": avg_duration},
            },
        }

    async def test_get_stats_returns_tenant_id(self):
        response = self._make_stats_response()
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["tenant_id"] == "t1"

    async def test_get_stats_returns_total_actions(self):
        response = self._make_stats_response(total=100)
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["total_actions"] == 100

    async def test_get_stats_returns_actions_per_agent(self):
        response = self._make_stats_response()
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["actions_per_agent"] == {
            "fleet_agent": 40,
            "scheduling_agent": 60,
        }

    async def test_get_stats_computes_success_rate(self):
        response = self._make_stats_response(total=100)
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["success_rate"] == 85.0

    async def test_get_stats_computes_failure_rate(self):
        response = self._make_stats_response(total=100)
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["failure_rate"] == 10.0

    async def test_get_stats_returns_avg_duration(self):
        response = self._make_stats_response(avg_duration=150.5)
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["avg_duration_ms"] == 150.5

    async def test_get_stats_returns_outcome_counts(self):
        response = self._make_stats_response()
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["outcome_counts"]["success"] == 85
        assert stats["outcome_counts"]["failure"] == 10

    async def test_get_stats_handles_zero_total(self):
        response = self._make_stats_response(
            total=0,
            agent_buckets=[],
            outcome_buckets=[],
            avg_duration=None,
        )
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["total_actions"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["failure_rate"] == 0.0
        assert stats["avg_duration_ms"] == 0.0
        assert stats["actions_per_agent"] == {}

    async def test_get_stats_queries_correct_index(self):
        response = self._make_stats_response()
        service = _make_service(search_response=response)

        await service.get_stats("t1")

        call_args = service._es.search_documents.call_args
        assert call_args[0][0] == "agent_activity_log"

    async def test_get_stats_filters_by_tenant(self):
        response = self._make_stats_response()
        service = _make_service(search_response=response)

        await service.get_stats("tenant-abc")

        query = service._es.search_documents.call_args[0][1]
        assert query["query"] == {"term": {"tenant_id": "tenant-abc"}}

    async def test_get_stats_uses_aggregations(self):
        response = self._make_stats_response()
        service = _make_service(search_response=response)

        await service.get_stats("t1")

        query = service._es.search_documents.call_args[0][1]
        assert "aggs" in query
        assert "actions_per_agent" in query["aggs"]
        assert "outcomes" in query["aggs"]
        assert "avg_duration" in query["aggs"]
        assert query["size"] == 0  # No hits needed for aggregation-only query
