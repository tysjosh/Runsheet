"""
Unit tests for PreferenceResolver — customer notification preference resolution and management.

Tests resolve_channels, list_preferences, get_preference, and upsert_preference
against a mocked ElasticsearchService.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from notifications.services.preference_resolver import (
    PreferenceResolver,
)
from notifications.services.notification_es_mappings import NOTIFICATION_PREFERENCES_INDEX
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    return es


def _make_resolver(es_mock: MagicMock) -> PreferenceResolver:
    """Create a PreferenceResolver with a mocked ES service."""
    return PreferenceResolver(es_service=es_mock)


def _preference_doc(
    customer_id: str = "cust-1",
    tenant_id: str = "tenant-1",
    customer_name: str = "Acme Corp",
    channels: dict | None = None,
    event_preferences: list | None = None,
    preference_id: str | None = None,
) -> dict:
    """Return a sample preference document."""
    return {
        "preference_id": preference_id or customer_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "channels": channels or {
            "sms": "+254700000001",
            "email": "acme@example.com",
            "whatsapp": "+254700000001",
        },
        "event_preferences": event_preferences or [
            {"event_type": "delay_alert", "enabled_channels": ["sms", "email"]},
            {"event_type": "delivery_confirmation", "enabled_channels": ["email"]},
        ],
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def _es_hit(doc: dict) -> dict:
    """Wrap a document in an ES hit envelope."""
    return {"_source": doc}


def _es_response(docs: list[dict]) -> dict:
    """Build a mock ES search response from a list of documents."""
    return {
        "hits": {
            "hits": [_es_hit(d) for d in docs],
            "total": {"value": len(docs)},
        }
    }


# ---------------------------------------------------------------------------
# resolve_channels
# ---------------------------------------------------------------------------


class TestResolveChannels:
    """Tests for PreferenceResolver.resolve_channels."""

    async def test_returns_channels_for_matching_event(self):
        """Returns correct channel+contact_detail pairs for a matching event type."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.resolve_channels("cust-1", "delay_alert", "tenant-1")

        assert len(result) == 2
        assert {"channel": "sms", "contact_detail": "+254700000001"} in result
        assert {"channel": "email", "contact_detail": "acme@example.com"} in result

    async def test_returns_single_channel(self):
        """Returns a single channel when only one is enabled for the event."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.resolve_channels("cust-1", "delivery_confirmation", "tenant-1")

        assert len(result) == 1
        assert result[0] == {"channel": "email", "contact_detail": "acme@example.com"}

    async def test_returns_empty_when_no_preference(self):
        """Returns empty list when no preference exists for the customer."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        result = await resolver.resolve_channels("cust-unknown", "delay_alert", "tenant-1")

        assert result == []

    async def test_returns_empty_when_event_type_not_in_preferences(self):
        """Returns empty list when the event type is not in event_preferences."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.resolve_channels("cust-1", "order_status_update", "tenant-1")

        assert result == []

    async def test_skips_channel_without_contact_detail(self):
        """Skips channels that are enabled but have no contact detail in channels map."""
        pref = _preference_doc(
            channels={"sms": "+254700000001"},  # no email contact
            event_preferences=[
                {"event_type": "delay_alert", "enabled_channels": ["sms", "email"]},
            ],
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.resolve_channels("cust-1", "delay_alert", "tenant-1")

        assert len(result) == 1
        assert result[0]["channel"] == "sms"

    async def test_returns_empty_when_no_enabled_channels(self):
        """Returns empty list when enabled_channels is empty for the event."""
        pref = _preference_doc(
            event_preferences=[
                {"event_type": "delay_alert", "enabled_channels": []},
            ],
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.resolve_channels("cust-1", "delay_alert", "tenant-1")

        assert result == []


# ---------------------------------------------------------------------------
# list_preferences
# ---------------------------------------------------------------------------


class TestListPreferences:
    """Tests for PreferenceResolver.list_preferences."""

    async def test_returns_paginated_results(self):
        """list_preferences returns items with pagination metadata."""
        prefs = [
            _preference_doc(customer_id="cust-1"),
            _preference_doc(customer_id="cust-2", customer_name="Beta Inc"),
        ]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(prefs))
        resolver = _make_resolver(es)

        result = await resolver.list_preferences("tenant-1", page=1, size=10)

        assert len(result["items"]) == 2
        assert result["total"] == 2
        assert result["page"] == 1
        assert result["size"] == 10

    async def test_returns_empty_list(self):
        """list_preferences returns empty items when no preferences exist."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        result = await resolver.list_preferences("tenant-1", page=1, size=10)

        assert result["items"] == []
        assert result["total"] == 0

    async def test_search_adds_match_clause(self):
        """list_preferences with search adds a match clause for customer_name."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        await resolver.list_preferences("tenant-1", page=1, size=10, search="Acme")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        # Should have tenant_id term + customer_name match
        assert len(must) == 2
        match_clause = must[1]
        assert "match" in match_clause
        assert "customer_name" in match_clause["match"]

    async def test_pagination_offset_calculation(self):
        """list_preferences calculates correct from offset for page > 1."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        await resolver.list_preferences("tenant-1", page=3, size=5)

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        assert query["from"] == 10  # (3-1) * 5


# ---------------------------------------------------------------------------
# get_preference
# ---------------------------------------------------------------------------


class TestGetPreference:
    """Tests for PreferenceResolver.get_preference."""

    async def test_returns_preference_when_found(self):
        """get_preference returns the preference document."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.get_preference("cust-1", "tenant-1")

        assert result["customer_id"] == "cust-1"
        assert result["customer_name"] == "Acme Corp"

    async def test_raises_404_when_not_found(self):
        """get_preference raises 404 when no preference exists."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        with pytest.raises(AppException) as exc_info:
            await resolver.get_preference("cust-missing", "tenant-1")

        assert exc_info.value.status_code == 404
        assert "cust-missing" in exc_info.value.message


# ---------------------------------------------------------------------------
# upsert_preference
# ---------------------------------------------------------------------------


class TestUpsertPreference:
    """Tests for PreferenceResolver.upsert_preference."""

    async def test_creates_new_preference(self):
        """upsert_preference creates a new document when none exists."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        data = {
            "customer_name": "New Customer",
            "channels": {"sms": "+254700000099"},
            "event_preferences": [
                {"event_type": "delay_alert", "enabled_channels": ["sms"]},
            ],
        }

        result = await resolver.upsert_preference("cust-new", "tenant-1", data)

        assert result["preference_id"] == "cust-new"
        assert result["customer_id"] == "cust-new"
        assert result["tenant_id"] == "tenant-1"
        assert result["customer_name"] == "New Customer"
        assert result["channels"] == {"sms": "+254700000099"}
        assert result["created_at"]
        assert result["updated_at"]

        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == NOTIFICATION_PREFERENCES_INDEX
        assert call_args[0][1] == "cust-new"

    async def test_updates_existing_preference(self):
        """upsert_preference updates an existing document."""
        existing = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([existing]))
        resolver = _make_resolver(es)

        data = {
            "customer_name": "Acme Corp Updated",
            "channels": {"sms": "+254700000002", "email": "new@acme.com"},
        }

        result = await resolver.upsert_preference("cust-1", "tenant-1", data)

        assert result["customer_name"] == "Acme Corp Updated"
        assert result["channels"]["sms"] == "+254700000002"
        # created_at should be preserved from original
        assert result["created_at"] == "2025-01-01T00:00:00+00:00"

        es.update_document.assert_called_once()
        es.index_document.assert_not_called()

    async def test_update_does_not_overwrite_immutable_fields(self):
        """upsert_preference does not allow overwriting preference_id, tenant_id, customer_id, created_at."""
        existing = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([existing]))
        resolver = _make_resolver(es)

        data = {
            "preference_id": "hacked-id",
            "tenant_id": "hacked-tenant",
            "customer_id": "hacked-customer",
            "created_at": "1999-01-01T00:00:00+00:00",
            "customer_name": "Legit Update",
        }

        result = await resolver.upsert_preference("cust-1", "tenant-1", data)

        # The update_document call should not include immutable fields
        call_args = es.update_document.call_args
        partial_doc = call_args[0][2]
        assert "preference_id" not in partial_doc
        assert "tenant_id" not in partial_doc
        assert "customer_id" not in partial_doc
        assert "created_at" not in partial_doc
        assert partial_doc["customer_name"] == "Legit Update"

    async def test_create_sets_timestamps(self):
        """upsert_preference sets created_at and updated_at on new documents."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        result = await resolver.upsert_preference("cust-new", "tenant-1", {
            "customer_name": "Test",
        })

        assert result["created_at"] is not None
        assert result["updated_at"] is not None
        assert result["created_at"] == result["updated_at"]

    async def test_create_defaults_empty_channels_and_preferences(self):
        """upsert_preference defaults channels and event_preferences to empty when not provided."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        result = await resolver.upsert_preference("cust-new", "tenant-1", {})

        assert result["channels"] == {}
        assert result["event_preferences"] == []
        assert result["customer_name"] == ""
