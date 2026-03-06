"""
Unit tests for JobService query methods (task 4.5).

Tests: get_job, list_jobs, get_active_jobs, get_delayed_jobs, get_job_events.
Validates: Requirements 5.1-5.7, 15.2
"""

import math
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scheduling.models import JobStatus, JobType
from scheduling.services.job_service import JobService


TENANT_ID = "tenant_test_001"


def _make_es_service():
    """Create a mock ElasticsearchService."""
    es = MagicMock()
    es.search_documents = AsyncMock()
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    return es


def _make_job_doc(**overrides):
    """Build a minimal job document dict."""
    doc = {
        "job_id": "JOB_1",
        "job_type": "cargo_transport",
        "status": "scheduled",
        "tenant_id": TENANT_ID,
        "asset_assigned": None,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": "2026-03-12T10:00:00+00:00",
        "estimated_arrival": None,
        "started_at": None,
        "completed_at": None,
        "created_at": "2026-03-12T08:00:00+00:00",
        "updated_at": "2026-03-12T08:00:00+00:00",
        "created_by": "user1",
        "priority": "normal",
        "delayed": False,
        "delay_duration_minutes": None,
        "failure_reason": None,
        "notes": None,
        "cargo_manifest": [
            {
                "item_id": "ITEM_abc",
                "description": "Steel pipes",
                "weight_kg": 500.0,
                "container_number": "CNT001",
                "seal_number": None,
                "item_status": "pending",
            }
        ],
    }
    doc.update(overrides)
    return doc


def _make_event_doc(**overrides):
    """Build a minimal job event document dict."""
    doc = {
        "event_id": "evt_001",
        "job_id": "JOB_1",
        "event_type": "job_created",
        "tenant_id": TENANT_ID,
        "actor_id": "user1",
        "event_timestamp": "2026-03-12T08:00:00+00:00",
        "event_payload": {"job": {}},
    }
    doc.update(overrides)
    return doc


def _es_search_response(hits, total=None):
    """Build a mock ES search response."""
    if total is None:
        total = len(hits)
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": total},
        }
    }


# ------------------------------------------------------------------ #
# get_job
# ------------------------------------------------------------------ #


class TestGetJob:
    """Tests for JobService.get_job() — Requirement 5.3."""

    @pytest.mark.asyncio
    async def test_returns_job_with_events(self):
        es = _make_es_service()
        job_doc = _make_job_doc()
        event_doc = _make_event_doc()

        # First call: _get_job_doc (jobs_current search)
        # Second call: get_job_events (job_events search)
        es.search_documents.side_effect = [
            _es_search_response([job_doc]),
            _es_search_response([event_doc]),
        ]

        svc = JobService(es, redis_url=None)
        result = await svc.get_job("JOB_1", TENANT_ID)

        assert result["job"].job_id == "JOB_1"
        assert len(result["events"]) == 1
        assert result["events"][0].event_type == "job_created"

    @pytest.mark.asyncio
    async def test_not_found_raises_404(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        with pytest.raises(Exception) as exc_info:
            await svc.get_job("JOB_MISSING", TENANT_ID)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_tenant_filter_applied(self):
        es = _make_es_service()
        job_doc = _make_job_doc()
        es.search_documents.side_effect = [
            _es_search_response([job_doc]),
            _es_search_response([]),
        ]

        svc = JobService(es, redis_url=None)
        await svc.get_job("JOB_1", TENANT_ID)

        # Verify tenant_id is in the query for both calls
        for call in es.search_documents.call_args_list:
            query_body = call[0][1]  # second positional arg
            bool_must = query_body["query"]["bool"]["must"]
            tenant_terms = [
                c for c in bool_must if "term" in c and "tenant_id" in c["term"]
            ]
            assert len(tenant_terms) == 1
            assert tenant_terms[0]["term"]["tenant_id"] == TENANT_ID


# ------------------------------------------------------------------ #
# list_jobs
# ------------------------------------------------------------------ #


class TestListJobs:
    """Tests for JobService.list_jobs() — Requirements 5.1, 5.2, 5.6, 5.7."""

    @pytest.mark.asyncio
    async def test_basic_pagination_envelope(self):
        es = _make_es_service()
        jobs = [_make_job_doc(job_id=f"JOB_{i}") for i in range(3)]
        es.search_documents.return_value = _es_search_response(jobs, total=25)

        svc = JobService(es, redis_url=None)
        result = await svc.list_jobs(TENANT_ID, page=1, size=3)

        assert len(result["data"]) == 3
        assert result["pagination"]["page"] == 1
        assert result["pagination"]["size"] == 3
        assert result["pagination"]["total"] == 25
        assert result["pagination"]["total_pages"] == math.ceil(25 / 3)

    @pytest.mark.asyncio
    async def test_filter_by_job_type(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.list_jobs(TENANT_ID, job_type="cargo_transport")

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]
        type_terms = [c for c in must if "term" in c and "job_type" in c["term"]]
        assert len(type_terms) == 1
        assert type_terms[0]["term"]["job_type"] == "cargo_transport"

    @pytest.mark.asyncio
    async def test_filter_by_status(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.list_jobs(TENANT_ID, status="assigned")

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]
        status_terms = [c for c in must if "term" in c and "status" in c["term"]]
        assert len(status_terms) == 1

    @pytest.mark.asyncio
    async def test_filter_by_date_range(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.list_jobs(
            TENANT_ID,
            start_date="2026-03-01T00:00:00Z",
            end_date="2026-03-31T23:59:59Z",
        )

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]
        range_clauses = [c for c in must if "range" in c]
        assert len(range_clauses) == 1
        assert "gte" in range_clauses[0]["range"]["scheduled_time"]
        assert "lte" in range_clauses[0]["range"]["scheduled_time"]

    @pytest.mark.asyncio
    async def test_combined_filters(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.list_jobs(
            TENANT_ID,
            job_type="vessel_movement",
            status="in_progress",
            asset_assigned="VESSEL_01",
        )

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]
        # tenant + job_type + status + asset_assigned = 4 clauses
        assert len(must) == 4

    @pytest.mark.asyncio
    async def test_invalid_job_type_returns_400(self):
        es = _make_es_service()
        svc = JobService(es, redis_url=None)

        with pytest.raises(Exception) as exc_info:
            await svc.list_jobs(TENANT_ID, job_type="invalid_type")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_status_returns_400(self):
        es = _make_es_service()
        svc = JobService(es, redis_url=None)

        with pytest.raises(Exception) as exc_info:
            await svc.list_jobs(TENANT_ID, status="nonexistent")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_sort_order_returns_400(self):
        es = _make_es_service()
        svc = JobService(es, redis_url=None)

        with pytest.raises(Exception) as exc_info:
            await svc.list_jobs(TENANT_ID, sort_order="sideways")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_total_pages_uses_ceil(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([], total=7)

        svc = JobService(es, redis_url=None)
        result = await svc.list_jobs(TENANT_ID, page=1, size=3)

        assert result["pagination"]["total_pages"] == 3  # ceil(7/3) = 3

    @pytest.mark.asyncio
    async def test_tenant_always_in_query(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.list_jobs(TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]
        tenant_terms = [
            c for c in must if "term" in c and "tenant_id" in c["term"]
        ]
        assert len(tenant_terms) == 1


# ------------------------------------------------------------------ #
# get_active_jobs
# ------------------------------------------------------------------ #


class TestGetActiveJobs:
    """Tests for JobService.get_active_jobs() — Requirement 5.4."""

    @pytest.mark.asyncio
    async def test_queries_active_statuses(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.get_active_jobs(TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]
        terms_clause = [c for c in must if "terms" in c]
        assert len(terms_clause) == 1
        statuses = terms_clause[0]["terms"]["status"]
        assert set(statuses) == {"scheduled", "assigned", "in_progress"}

    @pytest.mark.asyncio
    async def test_sorted_by_scheduled_time_asc(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.get_active_jobs(TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        assert query_body["sort"] == [{"scheduled_time": {"order": "asc"}}]

    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(self):
        es = _make_es_service()
        jobs = [
            _make_job_doc(job_id="JOB_1", status="scheduled"),
            _make_job_doc(job_id="JOB_2", status="in_progress"),
        ]
        es.search_documents.return_value = _es_search_response(jobs)

        svc = JobService(es, redis_url=None)
        result = await svc.get_active_jobs(TENANT_ID)

        assert len(result) == 2
        assert result[0]["job_id"] == "JOB_1"

    @pytest.mark.asyncio
    async def test_tenant_filter_applied(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.get_active_jobs(TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]
        tenant_terms = [
            c for c in must if "term" in c and "tenant_id" in c["term"]
        ]
        assert len(tenant_terms) == 1


# ------------------------------------------------------------------ #
# get_delayed_jobs
# ------------------------------------------------------------------ #


class TestGetDelayedJobs:
    """Tests for JobService.get_delayed_jobs() — Requirement 5.5."""

    @pytest.mark.asyncio
    async def test_queries_in_progress_and_delayed(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.get_delayed_jobs(TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]

        status_terms = [c for c in must if "term" in c and "status" in c["term"]]
        assert len(status_terms) == 1
        assert status_terms[0]["term"]["status"] == "in_progress"

        delayed_terms = [c for c in must if "term" in c and "delayed" in c["term"]]
        assert len(delayed_terms) == 1
        assert delayed_terms[0]["term"]["delayed"] is True

    @pytest.mark.asyncio
    async def test_returns_delayed_jobs(self):
        es = _make_es_service()
        delayed_job = _make_job_doc(
            job_id="JOB_LATE",
            status="in_progress",
            delayed=True,
            delay_duration_minutes=45,
        )
        es.search_documents.return_value = _es_search_response([delayed_job])

        svc = JobService(es, redis_url=None)
        result = await svc.get_delayed_jobs(TENANT_ID)

        assert len(result) == 1
        assert result[0]["delayed"] is True

    @pytest.mark.asyncio
    async def test_tenant_filter_applied(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.get_delayed_jobs(TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]
        tenant_terms = [
            c for c in must if "term" in c and "tenant_id" in c["term"]
        ]
        assert len(tenant_terms) == 1


# ------------------------------------------------------------------ #
# get_job_events
# ------------------------------------------------------------------ #


class TestGetJobEvents:
    """Tests for JobService.get_job_events() — Requirement 15.2."""

    @pytest.mark.asyncio
    async def test_returns_events_sorted_by_timestamp(self):
        es = _make_es_service()
        events = [
            _make_event_doc(event_id="e1", event_timestamp="2026-03-12T08:00:00+00:00"),
            _make_event_doc(event_id="e2", event_timestamp="2026-03-12T09:00:00+00:00"),
        ]
        es.search_documents.return_value = _es_search_response(events)

        svc = JobService(es, redis_url=None)
        result = await svc.get_job_events("JOB_1", TENANT_ID)

        assert len(result) == 2
        assert result[0].event_id == "e1"
        assert result[1].event_id == "e2"

        # Verify sort order in query
        query_body = es.search_documents.call_args[0][1]
        assert query_body["sort"] == [{"event_timestamp": {"order": "asc"}}]

    @pytest.mark.asyncio
    async def test_queries_job_events_index(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.get_job_events("JOB_1", TENANT_ID)

        index_arg = es.search_documents.call_args[0][0]
        assert index_arg == "job_events"

    @pytest.mark.asyncio
    async def test_filters_by_job_id_and_tenant(self):
        es = _make_es_service()
        es.search_documents.return_value = _es_search_response([])

        svc = JobService(es, redis_url=None)
        await svc.get_job_events("JOB_42", TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        must = query_body["query"]["bool"]["must"]

        job_terms = [c for c in must if "term" in c and "job_id" in c["term"]]
        assert len(job_terms) == 1
        assert job_terms[0]["term"]["job_id"] == "JOB_42"

        tenant_terms = [c for c in must if "term" in c and "tenant_id" in c["term"]]
        assert len(tenant_terms) == 1

    @pytest.mark.asyncio
    async def test_returns_job_event_models(self):
        es = _make_es_service()
        event = _make_event_doc(event_type="status_changed")
        es.search_documents.return_value = _es_search_response([event])

        svc = JobService(es, redis_url=None)
        result = await svc.get_job_events("JOB_1", TENANT_ID)

        assert len(result) == 1
        assert result[0].event_type == "status_changed"
        assert result[0].tenant_id == TENANT_ID
