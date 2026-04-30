"""
Unit tests for the Memory Service module.

Tests the MemoryService class including store_pattern, store_preference,
query_relevant, decay_stale, delete, and list_memories methods.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.memory_service import MemoryService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    search_response=None,
    get_response=None,
) -> MemoryService:
    """Create a MemoryService with mocked dependencies."""
    es_service = MagicMock()
    es_service.index_document = AsyncMock(return_value={"result": "created"})
    es_service.search_documents = AsyncMock(
        return_value=search_response
        or {
            "hits": {"hits": [], "total": {"value": 0}},
        }
    )
    es_service.update_document = AsyncMock(return_value={"result": "updated"})
    es_service.delete_document = AsyncMock(return_value={"result": "deleted"})
    es_service.get_document = AsyncMock(return_value=get_response)

    return MemoryService(es_service=es_service)


# ---------------------------------------------------------------------------
# Tests: store_pattern
# ---------------------------------------------------------------------------


class TestStorePattern:
    """Tests for storing operational patterns."""

    async def test_store_pattern_returns_uuid_string(self):
        service = _make_service()
        memory_id = await service.store_pattern(
            agent_id="fleet_agent",
            tenant_id="t1",
            content="Truck T-1042 is frequently delayed on Route 7",
            confidence=0.85,
            tags=["truck", "delay", "route-7"],
        )

        assert isinstance(memory_id, str)
        assert len(memory_id) == 36  # UUID format

    async def test_store_pattern_stores_in_correct_index(self):
        service = _make_service()
        await service.store_pattern(
            agent_id="fleet_agent",
            tenant_id="t1",
            content="Pattern content",
            confidence=0.9,
            tags=["test"],
        )

        call_args = service._es.index_document.call_args
        assert call_args[0][0] == "agent_memory"

    async def test_store_pattern_uses_memory_id_as_doc_id(self):
        service = _make_service()
        memory_id = await service.store_pattern(
            agent_id="fleet_agent",
            tenant_id="t1",
            content="Pattern content",
            confidence=0.9,
            tags=["test"],
        )

        call_args = service._es.index_document.call_args
        assert call_args[0][1] == memory_id

    async def test_store_pattern_sets_memory_type_to_pattern(self):
        service = _make_service()
        await service.store_pattern(
            agent_id="fleet_agent",
            tenant_id="t1",
            content="Pattern content",
            confidence=0.9,
            tags=["test"],
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["memory_type"] == "pattern"

    async def test_store_pattern_preserves_all_fields(self):
        service = _make_service()
        await service.store_pattern(
            agent_id="fleet_agent",
            tenant_id="t1",
            content="Truck T-1042 is frequently delayed on Route 7",
            confidence=0.85,
            tags=["truck", "delay"],
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["agent_id"] == "fleet_agent"
        assert doc["tenant_id"] == "t1"
        assert doc["content"] == "Truck T-1042 is frequently delayed on Route 7"
        assert doc["confidence_score"] == 0.85
        assert doc["tags"] == ["truck", "delay"]
        assert doc["access_count"] == 0
        assert "memory_id" in doc
        assert "created_at" in doc
        assert "last_accessed" in doc

    async def test_store_pattern_generates_unique_ids(self):
        service = _make_service()
        id1 = await service.store_pattern("a1", "t1", "pattern 1", 0.5, [])
        id2 = await service.store_pattern("a1", "t1", "pattern 2", 0.6, [])

        assert id1 != id2


# ---------------------------------------------------------------------------
# Tests: store_preference
# ---------------------------------------------------------------------------


class TestStorePreference:
    """Tests for storing user preferences."""

    async def test_store_preference_returns_uuid_string(self):
        service = _make_service()
        memory_id = await service.store_preference(
            agent_id="ai_agent",
            tenant_id="t1",
            content="Always notify me before reassignments",
            tags=["notification", "reassignment"],
        )

        assert isinstance(memory_id, str)
        assert len(memory_id) == 36

    async def test_store_preference_sets_memory_type_to_preference(self):
        service = _make_service()
        await service.store_preference(
            agent_id="ai_agent",
            tenant_id="t1",
            content="Prefer trucks over vans for cargo jobs",
            tags=["vehicle-preference"],
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["memory_type"] == "preference"

    async def test_store_preference_sets_confidence_to_1(self):
        service = _make_service()
        await service.store_preference(
            agent_id="ai_agent",
            tenant_id="t1",
            content="Prefer trucks over vans",
            tags=["vehicle"],
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["confidence_score"] == 1.0

    async def test_store_preference_preserves_all_fields(self):
        service = _make_service()
        await service.store_preference(
            agent_id="ai_agent",
            tenant_id="t1",
            content="Always notify me before reassignments",
            tags=["notification", "reassignment"],
        )

        doc = service._es.index_document.call_args[0][2]
        assert doc["agent_id"] == "ai_agent"
        assert doc["tenant_id"] == "t1"
        assert doc["content"] == "Always notify me before reassignments"
        assert doc["tags"] == ["notification", "reassignment"]
        assert doc["access_count"] == 0
        assert "memory_id" in doc
        assert "created_at" in doc
        assert "last_accessed" in doc

    async def test_store_preference_stores_in_correct_index(self):
        service = _make_service()
        await service.store_preference("a1", "t1", "pref", ["tag"])

        call_args = service._es.index_document.call_args
        assert call_args[0][0] == "agent_memory"


# ---------------------------------------------------------------------------
# Tests: query_relevant
# ---------------------------------------------------------------------------


class TestQueryRelevant:
    """Tests for querying relevant memories."""

    async def test_query_relevant_returns_matching_memories(self):
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "memory_type": "pattern",
                    "content": "Truck T-1042 delayed on Route 7",
                    "confidence_score": 0.85,
                    "access_count": 3,
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)

        memories = await service.query_relevant("t1", "delayed truck route")

        assert len(memories) == 1
        assert memories[0]["memory_id"] == "m-1"
        assert memories[0]["content"] == "Truck T-1042 delayed on Route 7"

    async def test_query_relevant_filters_by_tenant_id(self):
        service = _make_service()
        await service.query_relevant("t1", "some context")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": "t1"}} in must

    async def test_query_relevant_uses_content_match(self):
        service = _make_service()
        await service.query_relevant("t1", "delayed truck")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"match": {"content": "delayed truck"}} in must

    async def test_query_relevant_respects_limit(self):
        service = _make_service()
        await service.query_relevant("t1", "context", limit=10)

        query = service._es.search_documents.call_args[0][1]
        assert query["size"] == 10

    async def test_query_relevant_default_limit_is_5(self):
        service = _make_service()
        await service.query_relevant("t1", "context")

        query = service._es.search_documents.call_args[0][1]
        assert query["size"] == 5

    async def test_query_relevant_updates_access_metadata(self):
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "memory_type": "pattern",
                    "content": "Pattern",
                    "access_count": 5,
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)

        await service.query_relevant("t1", "context")

        service._es.update_document.assert_called_once()
        call_args = service._es.update_document.call_args
        assert call_args[0][0] == "agent_memory"
        assert call_args[0][1] == "m-1"
        update_body = call_args[0][2]
        assert update_body["doc"]["access_count"] == 6
        assert "last_accessed" in update_body["doc"]

    async def test_query_relevant_returns_empty_for_no_matches(self):
        service = _make_service()
        memories = await service.query_relevant("t1", "nonexistent context")

        assert memories == []

    async def test_query_relevant_handles_update_failure_gracefully(self):
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "memory_type": "pattern",
                    "content": "Pattern",
                    "access_count": 0,
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)
        service._es.update_document = AsyncMock(
            side_effect=Exception("ES update failed")
        )

        # Should not raise — returns memories even if access update fails
        memories = await service.query_relevant("t1", "context")
        assert len(memories) == 1

    async def test_query_relevant_searches_correct_index(self):
        service = _make_service()
        await service.query_relevant("t1", "context")

        call_args = service._es.search_documents.call_args
        assert call_args[0][0] == "agent_memory"


# ---------------------------------------------------------------------------
# Tests: decay_stale
# ---------------------------------------------------------------------------


class TestDecayStale:
    """Tests for relevance decay of stale memories."""

    async def test_decay_stale_reduces_confidence_by_50_percent(self):
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "confidence_score": 0.8,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)

        affected = await service.decay_stale()

        assert affected == 1
        call_args = service._es.update_document.call_args
        assert call_args[0][0] == "agent_memory"
        assert call_args[0][1] == "m-1"
        update_body = call_args[0][2]
        assert update_body["doc"]["confidence_score"] == 0.4  # 0.8 * 0.5

    async def test_decay_stale_purges_below_threshold(self):
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "confidence_score": 0.15,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)

        affected = await service.decay_stale()

        assert affected == 1
        # 0.15 * 0.5 = 0.075 < 0.1 → purge
        service._es.delete_document.assert_called_once_with("agent_memory", "m-1")
        service._es.update_document.assert_not_called()

    async def test_decay_stale_purges_at_exact_threshold(self):
        """Confidence of 0.2 * 0.5 = 0.1 — NOT below threshold, should decay."""
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "confidence_score": 0.2,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)

        await service.decay_stale()

        # 0.2 * 0.5 = 0.1 — exactly at threshold, NOT below → decay
        service._es.update_document.assert_called_once()
        service._es.delete_document.assert_not_called()

    async def test_decay_stale_purges_when_result_below_threshold(self):
        """Confidence of 0.18 * 0.5 = 0.09 — below 0.1, should purge."""
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "confidence_score": 0.18,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)

        await service.decay_stale()

        service._es.delete_document.assert_called_once_with("agent_memory", "m-1")

    async def test_decay_stale_handles_multiple_memories(self):
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "confidence_score": 0.8,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
            {
                "_source": {
                    "memory_id": "m-2",
                    "confidence_score": 0.1,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 2}}}
        service = _make_service(search_response=response)

        affected = await service.decay_stale()

        assert affected == 2
        # m-1: 0.8 * 0.5 = 0.4 → decay
        # m-2: 0.1 * 0.5 = 0.05 < 0.1 → purge
        service._es.update_document.assert_called_once()
        service._es.delete_document.assert_called_once_with("agent_memory", "m-2")

    async def test_decay_stale_returns_zero_when_no_stale_memories(self):
        service = _make_service()
        affected = await service.decay_stale()

        assert affected == 0

    async def test_decay_stale_queries_by_last_accessed_range(self):
        service = _make_service()
        await service.decay_stale()

        query = service._es.search_documents.call_args[0][1]
        assert "range" in query["query"]
        assert "last_accessed" in query["query"]["range"]
        assert "lte" in query["query"]["range"]["last_accessed"]

    async def test_decay_stale_handles_update_failure_gracefully(self):
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "confidence_score": 0.8,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
            {
                "_source": {
                    "memory_id": "m-2",
                    "confidence_score": 0.6,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 2}}}
        service = _make_service(search_response=response)
        # First update fails, second succeeds
        service._es.update_document = AsyncMock(
            side_effect=[Exception("ES error"), {"result": "updated"}]
        )

        affected = await service.decay_stale()

        # Only the second one counted as affected
        assert affected == 1

    async def test_decay_stale_handles_delete_failure_gracefully(self):
        hits = [
            {
                "_source": {
                    "memory_id": "m-1",
                    "confidence_score": 0.05,
                    "last_accessed": "2023-01-01T00:00:00+00:00",
                }
            },
        ]
        response = {"hits": {"hits": hits, "total": {"value": 1}}}
        service = _make_service(search_response=response)
        service._es.delete_document = AsyncMock(side_effect=Exception("ES error"))

        affected = await service.decay_stale()

        # Failed to purge, so not counted
        assert affected == 0

    async def test_decay_stale_searches_correct_index(self):
        service = _make_service()
        await service.decay_stale()

        call_args = service._es.search_documents.call_args
        assert call_args[0][0] == "agent_memory"


# ---------------------------------------------------------------------------
# Tests: delete
# ---------------------------------------------------------------------------


class TestDelete:
    """Tests for deleting memories."""

    async def test_delete_returns_true_on_success(self):
        get_response = {
            "_source": {
                "memory_id": "m-1",
                "tenant_id": "t1",
                "content": "Some memory",
            }
        }
        service = _make_service(get_response=get_response)

        result = await service.delete("m-1", "t1")

        assert result is True
        service._es.delete_document.assert_called_once_with("agent_memory", "m-1")

    async def test_delete_returns_false_when_not_found(self):
        service = _make_service(get_response=None)

        result = await service.delete("nonexistent", "t1")

        assert result is False
        service._es.delete_document.assert_not_called()

    async def test_delete_returns_false_on_tenant_mismatch(self):
        get_response = {
            "_source": {
                "memory_id": "m-1",
                "tenant_id": "t2",
                "content": "Some memory",
            }
        }
        service = _make_service(get_response=get_response)

        result = await service.delete("m-1", "t1")

        assert result is False
        service._es.delete_document.assert_not_called()

    async def test_delete_returns_false_on_es_error(self):
        get_response = {
            "_source": {
                "memory_id": "m-1",
                "tenant_id": "t1",
            }
        }
        service = _make_service(get_response=get_response)
        service._es.delete_document = AsyncMock(side_effect=Exception("ES error"))

        result = await service.delete("m-1", "t1")

        assert result is False

    async def test_delete_verifies_tenant_before_deleting(self):
        get_response = {
            "_source": {
                "memory_id": "m-1",
                "tenant_id": "t1",
            }
        }
        service = _make_service(get_response=get_response)

        await service.delete("m-1", "t1")

        # get_document should be called to verify tenant
        service._es.get_document.assert_called_once_with("agent_memory", "m-1")

    async def test_delete_uses_correct_index(self):
        get_response = {
            "_source": {
                "memory_id": "m-1",
                "tenant_id": "t1",
            }
        }
        service = _make_service(get_response=get_response)

        await service.delete("m-1", "t1")

        service._es.delete_document.assert_called_once_with("agent_memory", "m-1")


# ---------------------------------------------------------------------------
# Tests: list_memories
# ---------------------------------------------------------------------------


class TestListMemories:
    """Tests for listing memories with filtering."""

    async def test_list_memories_returns_paginated_results(self):
        hits = [
            {"_source": {"memory_id": "m-1", "memory_type": "pattern"}},
            {"_source": {"memory_id": "m-2", "memory_type": "preference"}},
        ]
        response = {"hits": {"hits": hits, "total": {"value": 2}}}
        service = _make_service(search_response=response)

        result = await service.list_memories("t1")

        assert len(result["items"]) == 2
        assert result["total"] == 2
        assert result["page"] == 1
        assert result["size"] == 20

    async def test_list_memories_filters_by_tenant_id(self):
        service = _make_service()
        await service.list_memories("t1")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": "t1"}} in must

    async def test_list_memories_filters_by_memory_type(self):
        service = _make_service()
        await service.list_memories("t1", memory_type="pattern")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"memory_type": "pattern"}} in must

    async def test_list_memories_filters_by_tags(self):
        service = _make_service()
        await service.list_memories("t1", tags=["truck", "delay"])

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"tags": "truck"}} in must
        assert {"term": {"tags": "delay"}} in must

    async def test_list_memories_no_optional_filters(self):
        service = _make_service()
        await service.list_memories("t1")

        query = service._es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        # Only tenant_id filter
        assert len(must) == 1

    async def test_list_memories_sorts_by_created_at_desc(self):
        service = _make_service()
        await service.list_memories("t1")

        query = service._es.search_documents.call_args[0][1]
        assert query["sort"] == [{"created_at": {"order": "desc"}}]

    async def test_list_memories_pagination(self):
        service = _make_service()
        await service.list_memories("t1", page=3, size=10)

        query = service._es.search_documents.call_args[0][1]
        assert query["from"] == 20  # (3-1) * 10
        assert query["size"] == 10

    async def test_list_memories_returns_empty_for_no_results(self):
        service = _make_service()
        result = await service.list_memories("t1")

        assert result["items"] == []
        assert result["total"] == 0

    async def test_list_memories_handles_total_as_integer(self):
        """Some ES versions return total as an integer instead of dict."""
        response = {"hits": {"hits": [], "total": 5}}
        service = _make_service(search_response=response)

        result = await service.list_memories("t1")
        assert result["total"] == 5

    async def test_list_memories_searches_correct_index(self):
        service = _make_service()
        await service.list_memories("t1")

        call_args = service._es.search_documents.call_args
        assert call_args[0][0] == "agent_memory"
