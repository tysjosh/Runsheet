"""
Unit tests for AuditTimelineService — immutable audit timeline for job events.

Tests append_event and query_timeline against a mocked ElasticsearchService.

Validates: Requirements 12.1, 12.2, 12.3, 12.4
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from notifications.services.audit_timeline_service import AuditTimelineService
from notifications.services.audit_es_mappings import JOB_AUDIT_TIMELINE_INDEX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    return es


def _es_hit(doc: dict) -> dict:
    """Wrap a document in an ES hit envelope."""
    return {"_source": doc}


def _es_response(docs: list[dict], total: int | None = None) -> dict:
    """Build a mock ES search response from a list of documents."""
    return {
        "hits": {
            "hits": [_es_hit(d) for d in docs],
            "total": {"value": total if total is not None else len(docs)},
        }
    }


def _audit_event_doc(
    timeline_event_id: str = "evt-1",
    job_id: str = "JOB_1",
    event_type: str = "status_changed",
    actor_type: str = "dispatcher",
    actor_id: str = "user-1",
    timestamp: str = "2025-01-01T00:00:00+00:00",
    payload: dict | None = None,
    tenant_id: str = "tenant-1",
) -> dict:
    """Return a sample audit timeline event document."""
    return {
        "timeline_event_id": timeline_event_id,
        "job_id": job_id,
        "event_type": event_type,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "timestamp": timestamp,
        "payload": payload or {},
        "tenant_id": tenant_id,
    }


# ---------------------------------------------------------------------------
# append_event
# ---------------------------------------------------------------------------


class TestAppendEvent:
    """Tests for AuditTimelineService.append_event."""

    async def test_appends_event_to_es(self):
        """append_event indexes a document in the job_audit_timeline index."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        result = await service.append_event(
            job_id="JOB_1",
            event_type="status_changed",
            actor_type="dispatcher",
            actor_id="user-1",
            payload={"old_status": "assigned", "new_status": "in_progress"},
            tenant_id="tenant-1",
        )

        # Returns a UUID string
        assert isinstance(result, str)
        assert len(result) == 36  # UUID format

        # Verify ES index_document was called
        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == JOB_AUDIT_TIMELINE_INDEX
        assert call_args[0][1] == result  # doc_id == timeline_event_id

        # Verify document structure
        doc = call_args[0][2]
        assert doc["timeline_event_id"] == result
        assert doc["job_id"] == "JOB_1"
        assert doc["event_type"] == "status_changed"
        assert doc["actor_type"] == "dispatcher"
        assert doc["actor_id"] == "user-1"
        assert doc["payload"] == {"old_status": "assigned", "new_status": "in_progress"}
        assert doc["tenant_id"] == "tenant-1"
        assert "timestamp" in doc

    async def test_generates_unique_event_ids(self):
        """Each call to append_event generates a unique timeline_event_id."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        id1 = await service.append_event(
            job_id="JOB_1",
            event_type="ack",
            actor_type="driver",
            actor_id="driver-1",
            payload={},
            tenant_id="tenant-1",
        )
        id2 = await service.append_event(
            job_id="JOB_1",
            event_type="accept",
            actor_type="driver",
            actor_id="driver-1",
            payload={},
            tenant_id="tenant-1",
        )

        assert id1 != id2

    async def test_sets_current_timestamp(self):
        """append_event uses the current UTC timestamp."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.append_event(
            job_id="JOB_1",
            event_type="notification_sent",
            actor_type="system",
            actor_id="notification-service",
            payload={"notification_id": "notif-1"},
            tenant_id="tenant-1",
        )

        doc = es.index_document.call_args[0][2]
        # Timestamp should be an ISO 8601 string
        assert "T" in doc["timestamp"]
        assert "+" in doc["timestamp"] or "Z" in doc["timestamp"]

    async def test_handles_empty_payload(self):
        """append_event stores empty dict when payload is empty."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.append_event(
            job_id="JOB_1",
            event_type="ack",
            actor_type="driver",
            actor_id="driver-1",
            payload={},
            tenant_id="tenant-1",
        )

        doc = es.index_document.call_args[0][2]
        assert doc["payload"] == {}

    async def test_propagates_es_errors(self):
        """append_event raises when ES index_document fails."""
        es = _make_es_mock()
        es.index_document = AsyncMock(side_effect=Exception("ES connection error"))
        service = AuditTimelineService(es)

        with pytest.raises(Exception, match="ES connection error"):
            await service.append_event(
                job_id="JOB_1",
                event_type="status_changed",
                actor_type="dispatcher",
                actor_id="user-1",
                payload={},
                tenant_id="tenant-1",
            )

    async def test_supports_all_actor_types(self):
        """append_event accepts all valid actor_type values."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        for actor_type in ("driver", "dispatcher", "agent", "system"):
            await service.append_event(
                job_id="JOB_1",
                event_type="test_event",
                actor_type=actor_type,
                actor_id=f"{actor_type}-1",
                payload={},
                tenant_id="tenant-1",
            )

        assert es.index_document.call_count == 4

    async def test_supports_various_event_types(self):
        """append_event accepts various event_type values."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        event_types = [
            "status_changed",
            "assignment",
            "ack",
            "message",
            "exception_reported",
            "pod_submitted",
            "notification_sent",
            "proposal_executed",
        ]

        for event_type in event_types:
            await service.append_event(
                job_id="JOB_1",
                event_type=event_type,
                actor_type="system",
                actor_id="system",
                payload={},
                tenant_id="tenant-1",
            )

        assert es.index_document.call_count == len(event_types)


# ---------------------------------------------------------------------------
# query_timeline
# ---------------------------------------------------------------------------


class TestQueryTimeline:
    """Tests for AuditTimelineService.query_timeline."""

    async def test_returns_events_for_job(self):
        """query_timeline returns events for the specified job_id."""
        docs = [
            _audit_event_doc(timeline_event_id="evt-1", event_type="status_changed"),
            _audit_event_doc(timeline_event_id="evt-2", event_type="ack"),
        ]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(docs))
        service = AuditTimelineService(es)

        result = await service.query_timeline("JOB_1", "tenant-1")

        assert len(result) == 2
        assert result[0]["timeline_event_id"] == "evt-1"
        assert result[1]["timeline_event_id"] == "evt-2"

    async def test_queries_with_job_id_and_tenant_id(self):
        """query_timeline always filters by job_id and tenant_id."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline("JOB_1", "tenant-1")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"job_id": "JOB_1"}} in must
        assert {"term": {"tenant_id": "tenant-1"}} in must

    async def test_sorts_by_timestamp_ascending(self):
        """query_timeline sorts results by timestamp ascending."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline("JOB_1", "tenant-1")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        assert query["sort"] == [{"timestamp": {"order": "asc"}}]

    async def test_filters_by_event_type(self):
        """query_timeline applies event_type filter when provided."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline("JOB_1", "tenant-1", event_type="ack")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"event_type": "ack"}} in must

    async def test_filters_by_actor_type(self):
        """query_timeline applies actor_type filter when provided."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline("JOB_1", "tenant-1", actor_type="driver")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"actor_type": "driver"}} in must

    async def test_filters_by_time_range(self):
        """query_timeline applies start_time and end_time filters."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline(
            "JOB_1",
            "tenant-1",
            start_time="2025-01-01T00:00:00Z",
            end_time="2025-01-31T23:59:59Z",
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]

        time_range_clause = None
        for clause in must:
            if "range" in clause:
                time_range_clause = clause
                break

        assert time_range_clause is not None
        assert time_range_clause["range"]["timestamp"]["gte"] == "2025-01-01T00:00:00Z"
        assert time_range_clause["range"]["timestamp"]["lte"] == "2025-01-31T23:59:59Z"

    async def test_filters_by_start_time_only(self):
        """query_timeline applies only start_time when end_time is None."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline(
            "JOB_1", "tenant-1", start_time="2025-01-01T00:00:00Z"
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]

        time_range_clause = None
        for clause in must:
            if "range" in clause:
                time_range_clause = clause
                break

        assert time_range_clause is not None
        assert "gte" in time_range_clause["range"]["timestamp"]
        assert "lte" not in time_range_clause["range"]["timestamp"]

    async def test_filters_by_end_time_only(self):
        """query_timeline applies only end_time when start_time is None."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline(
            "JOB_1", "tenant-1", end_time="2025-01-31T23:59:59Z"
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]

        time_range_clause = None
        for clause in must:
            if "range" in clause:
                time_range_clause = clause
                break

        assert time_range_clause is not None
        assert "lte" in time_range_clause["range"]["timestamp"]
        assert "gte" not in time_range_clause["range"]["timestamp"]

    async def test_combines_all_filters(self):
        """query_timeline combines event_type, actor_type, and time range filters."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline(
            "JOB_1",
            "tenant-1",
            event_type="status_changed",
            actor_type="dispatcher",
            start_time="2025-01-01T00:00:00Z",
            end_time="2025-01-31T23:59:59Z",
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]

        # Should have 5 clauses: job_id, tenant_id, event_type, actor_type, time range
        assert len(must) == 5
        assert {"term": {"job_id": "JOB_1"}} in must
        assert {"term": {"tenant_id": "tenant-1"}} in must
        assert {"term": {"event_type": "status_changed"}} in must
        assert {"term": {"actor_type": "dispatcher"}} in must

    async def test_returns_empty_when_no_events(self):
        """query_timeline returns empty list when no events found."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        result = await service.query_timeline("JOB_1", "tenant-1")

        assert result == []

    async def test_no_filters_returns_all_events_for_job(self):
        """query_timeline without optional filters returns all events for the job."""
        docs = [
            _audit_event_doc(timeline_event_id="evt-1", event_type="status_changed", actor_type="dispatcher"),
            _audit_event_doc(timeline_event_id="evt-2", event_type="ack", actor_type="driver"),
            _audit_event_doc(timeline_event_id="evt-3", event_type="notification_sent", actor_type="system"),
        ]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(docs))
        service = AuditTimelineService(es)

        result = await service.query_timeline("JOB_1", "tenant-1")

        assert len(result) == 3

    async def test_uses_correct_index(self):
        """query_timeline queries the job_audit_timeline index."""
        es = _make_es_mock()
        service = AuditTimelineService(es)

        await service.query_timeline("JOB_1", "tenant-1")

        call_args = es.search_documents.call_args
        assert call_args[0][0] == JOB_AUDIT_TIMELINE_INDEX
