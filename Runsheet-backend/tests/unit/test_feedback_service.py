"""
Unit tests for the Feedback Service module.

Tests the FeedbackService class including record_rejection, record_override,
query_similar, get_stats, compute_confidence, and list_feedback methods.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7
"""
import math
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.feedback_service import FeedbackService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    search_response=None,
) -> FeedbackService:
    """Create a FeedbackService with mocked dependencies."""
    es_service = MagicMock()
    es_service.index_document = AsyncMock(return_value={"result": "created"})
    es_service.search_documents = AsyncMock(
        return_value=search_response
        or {
            "hits": {"hits": [], "total": {"value": 0}},
        }
    )

    return FeedbackService(es_service=es_service)


# ---------------------------------------------------------------------------
# Tests: record_rejection
# ---------------------------------------------------------------------------


class TestRecordRejection:
    """Tests for recording rejection feedback signals."""

    async def test_record_rejection_returns_uuid_string(self):
        service = _make_service()
        feedback_id = await service.record_rejection(
            agent_id="delay_response_agent",
            action_type="reassign_rider",
            original_proposal={"shipment_id": "S-1", "new_rider_id": "R-5"},
            rejection_reason="Rider R-5 is on leave",
            user_action={"reassigned_to": "R-8"},
            tenant_id="t1",
            user_id="u1",
        )

        assert isinstance(feedback_id, str)
        assert len(feedback_id) == 36  # UUID format

    async def test_record_rejection_stores_in_correct_index(self):
        service = _make_service()
        await service.record_rejection(
            agent_id="agent1",
            action_type="cancel_job",
            original_proposal={"job_id": "J-1"},
            rejection_reason="Not needed",
            user_action={},
            tenant_id="t1",
            user_id="u1",
        )

        call_args = service._es.index_document.call_args
        assert call_args[0][0] == "agent_feedback"

    async def test_record_rejection_uses_feedback_id_as_doc_id(self):
        service = _make_service()
        feedback_id = await service.record_rejection(
            agent_id="agent1",
            action_type="cancel_job",
            original_proposal={"job_id": "J-1"},
            rejection_reason="Not needed",
            user_action={},
            tenant_id="t1",
            user_id="u1",
        )

        call_args = service._es.index_document.call_args
        assert call_args[0][1] == feedback_id

    async def test_record_rejection_sets_feedback_type_to_rejection(self):
        service = _make_service()
        await service.record_rejection(
            agent_id="agent1",
            action_type="cancel_job",
            original_proposal={"job_id": "J-1"},
            rejection_reason="Not needed",
            user_action={},
            tenant_id="t1",
            user_id="u1",
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["feedback_type"] == "rejection"

    async def test_record_rejection_stores_rejection_reason_in_context(self):
        service = _make_service()
        await service.record_rejection(
            agent_id="agent1",
            action_type="cancel_job",
            original_proposal={"job_id": "J-1"},
            rejection_reason="Job is still needed",
            user_action={},
            tenant_id="t1",
            user_id="u1",
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["context"]["rejection_reason"] == "Job is still needed"

    async def test_record_rejection_preserves_all_fields(self):
        service = _make_service()
        await service.record_rejection(
            agent_id="delay_response_agent",
            action_type="reassign_rider",
            original_proposal={"shipment_id": "S-1", "new_rider_id": "R-5"},
            rejection_reason="Rider R-5 is on leave",
            user_action={"reassigned_to": "R-8"},
            tenant_id="t1",
            user_id="u1",
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["agent_id"] == "delay_response_agent"
        assert doc["action_type"] == "reassign_rider"
        assert doc["original_proposal"] == {"shipment_id": "S-1", "new_rider_id": "R-5"}
        assert doc["user_action"] == {"reassigned_to": "R-8"}
        assert doc["feedback_type"] == "rejection"
        assert doc["tenant_id"] == "t1"
        assert doc["user_id"] == "u1"
        assert "feedback_id" in doc
        assert "timestamp" in doc

    async def test_record_rejection_generates_unique_ids(self):
        service = _make_service()
        id1 = await service.record_rejection(
            "a1", "cancel_job", {"job_id": "J-1"}, "reason1", {}, "t1", "u1"
        )
        id2 = await service.record_rejection(
            "a1", "cancel_job", {"job_id": "J-2"}, "reason2", {}, "t1", "u1"
        )

        assert id1 != id2


# ---------------------------------------------------------------------------
# Tests: record_override
# ---------------------------------------------------------------------------


class TestRecordOverride:
    """Tests for recording override feedback signals."""

    async def test_record_override_returns_uuid_string(self):
        service = _make_service()
        feedback_id = await service.record_override(
            agent_id="scheduling_agent",
            action_type="assign_asset_to_job",
            original_suggestion={"job_id": "J-1", "asset_id": "A-1"},
            user_action={"job_id": "J-1", "asset_id": "A-3"},
            tenant_id="t1",
            user_id="u1",
        )

        assert isinstance(feedback_id, str)
        assert len(feedback_id) == 36

    async def test_record_override_sets_feedback_type_to_override(self):
        service = _make_service()
        await service.record_override(
            agent_id="scheduling_agent",
            action_type="assign_asset_to_job",
            original_suggestion={"job_id": "J-1", "asset_id": "A-1"},
            user_action={"job_id": "J-1", "asset_id": "A-3"},
            tenant_id="t1",
            user_id="u1",
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["feedback_type"] == "override"

    async def test_record_override_stores_original_suggestion_as_proposal(self):
        service = _make_service()
        await service.record_override(
            agent_id="scheduling_agent",
            action_type="assign_asset_to_job",
            original_suggestion={"job_id": "J-1", "asset_id": "A-1"},
            user_action={"job_id": "J-1", "asset_id": "A-3"},
            tenant_id="t1",
            user_id="u1",
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["original_proposal"] == {"job_id": "J-1", "asset_id": "A-1"}

    async def test_record_override_preserves_all_fields(self):
        service = _make_service()
        await service.record_override(
            agent_id="fuel_agent",
            action_type="request_fuel_refill",
            original_suggestion={"station_id": "S-1", "quantity": 500},
            user_action={"station_id": "S-1", "quantity": 300},
            tenant_id="t2",
            user_id="u5",
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["agent_id"] == "fuel_agent"
        assert doc["action_type"] == "request_fuel_refill"
        assert doc["user_action"] == {"station_id": "S-1", "quantity": 300}
        assert doc["tenant_id"] == "t2"
        assert doc["user_id"] == "u5"
        assert doc["context"] == {}
        assert "feedback_id" in doc
        assert "timestamp" in doc

    async def test_record_override_stores_in_correct_index(self):
        service = _make_service()
        await service.record_override(
            "a1", "action", {"key": "val"}, {"key": "val2"}, "t1", "u1"
        )

        call_args = service._es.index_document.call_args
        assert call_args[0][0] == "agent_feedback"


# ---------------------------------------------------------------------------
# Tests: query_similar
# ---------------------------------------------------------------------------


class TestQuerySimilar:
    """Tests for querying similar feedback signals."""

    async def test_query_similar_returns_matching_feedback(self):
        hits = [
            {
                "_source": {
                    "feedback_id": "f-1",
                    "feedback_type": "rejection",
                    "action_type": "reassign_rider",
                    "agent_id": "delay_response_agent",
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)

        results = await service.query_similar(
            action_type="reassign_rider",
            context={"shipment_id": "S-1"},
            tenant_id="t1",
        )

        assert len(results) == 1
        assert results[0]["feedback_id"] == "f-1"

    async def test_query_similar_filters_by_tenant_and_action_type(self):
        service = _make_service()
        await service.query_similar("reassign_rider", {}, "t1")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": "t1"}} in must
        assert {"term": {"action_type": "reassign_rider"}} in must

    async def test_query_similar_sorts_by_timestamp_desc(self):
        service = _make_service()
        await service.query_similar("cancel_job", {}, "t1")

        query = service._es.search_documents.call_args[0][1]
        assert query["sort"] == [{"timestamp": {"order": "desc"}}]

    async def test_query_similar_respects_limit(self):
        service = _make_service()
        await service.query_similar("cancel_job", {}, "t1", limit=5)

        query = service._es.search_documents.call_args[0][1]
        assert query["size"] == 5

    async def test_query_similar_default_limit_is_10(self):
        service = _make_service()
        await service.query_similar("cancel_job", {}, "t1")

        query = service._es.search_documents.call_args[0][1]
        assert query["size"] == 10

    async def test_query_similar_returns_empty_for_no_matches(self):
        service = _make_service()
        results = await service.query_similar("nonexistent", {}, "t1")

        assert results == []

    async def test_query_similar_searches_correct_index(self):
        service = _make_service()
        await service.query_similar("cancel_job", {}, "t1")

        call_args = service._es.search_documents.call_args
        assert call_args[0][0] == "agent_feedback"


# ---------------------------------------------------------------------------
# Tests: get_stats
# ---------------------------------------------------------------------------


class TestGetStats:
    """Tests for aggregated feedback statistics."""

    async def test_get_stats_returns_expected_structure(self):
        response = {
            "hits": {"hits": [], "total": {"value": 10}},
            "aggregations": {
                "feedback_types": {
                    "buckets": [
                        {"key": "rejection", "doc_count": 7},
                        {"key": "override", "doc_count": 3},
                    ]
                },
                "rejections_per_agent": {
                    "agents": {
                        "buckets": [
                            {"key": "delay_response_agent", "doc_count": 4},
                            {"key": "fuel_management_agent", "doc_count": 3},
                        ]
                    }
                },
                "common_action_types": {
                    "buckets": [
                        {"key": "reassign_rider", "doc_count": 5},
                        {"key": "cancel_job", "doc_count": 3},
                    ]
                },
            },
        }
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")

        assert stats["tenant_id"] == "t1"
        assert stats["total_feedback"] == 10
        assert stats["rejection_count"] == 7
        assert stats["override_count"] == 3
        assert stats["rejection_rate"] == 70.0
        assert stats["rejections_per_agent"]["delay_response_agent"] == 4
        assert stats["rejections_per_agent"]["fuel_management_agent"] == 3
        assert stats["common_action_types"]["reassign_rider"] == 5

    async def test_get_stats_handles_zero_feedback(self):
        service = _make_service()

        stats = await service.get_stats("t1")

        assert stats["total_feedback"] == 0
        assert stats["rejection_count"] == 0
        assert stats["override_count"] == 0
        assert stats["rejection_rate"] == 0.0
        assert stats["rejections_per_agent"] == {}
        assert stats["common_action_types"] == {}

    async def test_get_stats_filters_by_tenant_id(self):
        service = _make_service()
        await service.get_stats("t1")

        query = service._es.search_documents.call_args[0][1]
        assert query["query"] == {"term": {"tenant_id": "t1"}}

    async def test_get_stats_uses_size_zero(self):
        service = _make_service()
        await service.get_stats("t1")

        query = service._es.search_documents.call_args[0][1]
        assert query["size"] == 0

    async def test_get_stats_handles_total_as_integer(self):
        """Some ES versions return total as an integer instead of dict."""
        response = {
            "hits": {"hits": [], "total": 5},
            "aggregations": {
                "feedback_types": {"buckets": []},
                "rejections_per_agent": {"agents": {"buckets": []}},
                "common_action_types": {"buckets": []},
            },
        }
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["total_feedback"] == 5

    async def test_get_stats_searches_correct_index(self):
        service = _make_service()
        await service.get_stats("t1")

        call_args = service._es.search_documents.call_args
        assert call_args[0][0] == "agent_feedback"

    async def test_get_stats_rejection_rate_calculation(self):
        response = {
            "hits": {"hits": [], "total": {"value": 4}},
            "aggregations": {
                "feedback_types": {
                    "buckets": [
                        {"key": "rejection", "doc_count": 1},
                        {"key": "override", "doc_count": 3},
                    ]
                },
                "rejections_per_agent": {"agents": {"buckets": []}},
                "common_action_types": {"buckets": []},
            },
        }
        service = _make_service(search_response=response)

        stats = await service.get_stats("t1")
        assert stats["rejection_rate"] == 25.0


# ---------------------------------------------------------------------------
# Tests: compute_confidence
# ---------------------------------------------------------------------------


class TestComputeConfidence:
    """Tests for confidence score computation using exponential decay."""

    def test_zero_rejections_returns_base(self):
        service = _make_service()
        result = service.compute_confidence(base=1.0, rejection_count=0)

        assert result == 1.0

    def test_one_rejection_applies_decay(self):
        service = _make_service()
        result = service.compute_confidence(base=1.0, rejection_count=1)

        expected = 1.0 * math.exp(-0.3 * 1)
        assert result == pytest.approx(expected)

    def test_multiple_rejections_decrease_confidence(self):
        service = _make_service()
        result = service.compute_confidence(base=1.0, rejection_count=5)

        expected = 1.0 * math.exp(-0.3 * 5)
        assert result == pytest.approx(expected)

    def test_confidence_monotonically_decreases(self):
        service = _make_service()
        scores = [
            service.compute_confidence(base=1.0, rejection_count=i)
            for i in range(10)
        ]

        for i in range(1, len(scores)):
            assert scores[i] < scores[i - 1]

    def test_zero_rejections_higher_than_any_positive_count(self):
        service = _make_service()
        zero_score = service.compute_confidence(base=0.8, rejection_count=0)

        for count in range(1, 20):
            score = service.compute_confidence(base=0.8, rejection_count=count)
            assert zero_score > score

    def test_custom_base_score(self):
        service = _make_service()
        result = service.compute_confidence(base=0.5, rejection_count=2)

        expected = 0.5 * math.exp(-0.3 * 2)
        assert result == pytest.approx(expected)

    def test_negative_rejection_count_returns_base(self):
        service = _make_service()
        result = service.compute_confidence(base=0.9, rejection_count=-1)

        assert result == 0.9

    def test_large_rejection_count_approaches_zero(self):
        service = _make_service()
        result = service.compute_confidence(base=1.0, rejection_count=100)

        assert result > 0.0
        assert result < 0.001

    def test_confidence_is_always_non_negative(self):
        service = _make_service()
        for count in range(50):
            result = service.compute_confidence(base=1.0, rejection_count=count)
            assert result >= 0.0


# ---------------------------------------------------------------------------
# Tests: list_feedback
# ---------------------------------------------------------------------------


class TestListFeedback:
    """Tests for listing feedback signals with filtering."""

    async def test_list_feedback_returns_paginated_results(self):
        hits = [
            {"_source": {"feedback_id": "f-1", "feedback_type": "rejection"}},
            {"_source": {"feedback_id": "f-2", "feedback_type": "override"}},
        ]
        response = {"hits": {"hits": hits, "total": {"value": 2}}}
        service = _make_service(search_response=response)

        result = await service.list_feedback("t1")

        assert len(result["items"]) == 2
        assert result["total"] == 2
        assert result["page"] == 1
        assert result["size"] == 20

    async def test_list_feedback_filters_by_tenant_id(self):
        service = _make_service()
        await service.list_feedback("t1")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": "t1"}} in must

    async def test_list_feedback_filters_by_agent_id(self):
        service = _make_service()
        await service.list_feedback("t1", agent_id="delay_response_agent")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"agent_id": "delay_response_agent"}} in must

    async def test_list_feedback_filters_by_action_type(self):
        service = _make_service()
        await service.list_feedback("t1", action_type="reassign_rider")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"action_type": "reassign_rider"}} in must

    async def test_list_feedback_filters_by_time_range(self):
        service = _make_service()
        await service.list_feedback(
            "t1",
            time_range={"gte": "2024-01-01T00:00:00Z", "lte": "2024-12-31T23:59:59Z"},
        )

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        range_clause = next(c for c in must if "range" in c)
        assert range_clause["range"]["timestamp"]["gte"] == "2024-01-01T00:00:00Z"
        assert range_clause["range"]["timestamp"]["lte"] == "2024-12-31T23:59:59Z"

    async def test_list_feedback_no_optional_filters(self):
        service = _make_service()
        await service.list_feedback("t1")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        # Only tenant_id filter
        assert len(must) == 1

    async def test_list_feedback_sorts_by_timestamp_desc(self):
        service = _make_service()
        await service.list_feedback("t1")

        query = service._es.search_documents.call_args[0][1]
        assert query["sort"] == [{"timestamp": {"order": "desc"}}]

    async def test_list_feedback_pagination(self):
        service = _make_service()
        await service.list_feedback("t1", page=3, size=10)

        query = service._es.search_documents.call_args[0][1]
        assert query["from"] == 20  # (3-1) * 10
        assert query["size"] == 10

    async def test_list_feedback_returns_empty_for_no_results(self):
        service = _make_service()
        result = await service.list_feedback("t1")

        assert result["items"] == []
        assert result["total"] == 0

    async def test_list_feedback_handles_total_as_integer(self):
        """Some ES versions return total as an integer instead of dict."""
        response = {"hits": {"hits": [], "total": 3}}
        service = _make_service(search_response=response)

        result = await service.list_feedback("t1")
        assert result["total"] == 3

    async def test_list_feedback_searches_correct_index(self):
        service = _make_service()
        await service.list_feedback("t1")

        call_args = service._es.search_documents.call_args
        assert call_args[0][0] == "agent_feedback"
