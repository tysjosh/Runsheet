"""
Unit tests for per-template opt-out preferences.

Tests that customers can opt out of specific notification templates,
opted-out templates are not sent, mandatory templates cannot be opted out of,
and the default state is all templates enabled (no opt-outs).

Validates: Requirement 12.9
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from notifications.services.preference_resolver import (
    MANDATORY_TEMPLATE_KEYS,
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
    template_opt_outs: list | None = None,
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
        },
        "event_preferences": event_preferences or [
            {"event_type": "delay_alert", "enabled_channels": ["sms", "email"]},
        ],
        "template_opt_outs": template_opt_outs or [],
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def _es_response(docs: list[dict]) -> dict:
    """Build a mock ES search response from a list of documents."""
    return {
        "hits": {
            "hits": [{"_source": d} for d in docs],
            "total": {"value": len(docs)},
        }
    }


# ---------------------------------------------------------------------------
# is_template_opted_out
# ---------------------------------------------------------------------------


class TestIsTemplateOptedOut:
    """Tests for PreferenceResolver.is_template_opted_out."""

    async def test_returns_false_when_no_preference_exists(self):
        """Default is all templates enabled — no preference means not opted out."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        result = await resolver.is_template_opted_out(
            "cust-1", "low_tank_autofill_alert", "tenant-1"
        )

        assert result is False

    async def test_returns_false_when_template_not_in_opt_outs(self):
        """Returns False when the template is not in the customer's opt-out list."""
        pref = _preference_doc(template_opt_outs=["delivery_completed"])
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.is_template_opted_out(
            "cust-1", "low_tank_autofill_alert", "tenant-1"
        )

        assert result is False

    async def test_returns_true_when_template_is_opted_out(self):
        """Returns True when the customer has opted out of the template."""
        pref = _preference_doc(
            template_opt_outs=["low_tank_autofill_alert", "delivery_completed"]
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.is_template_opted_out(
            "cust-1", "low_tank_autofill_alert", "tenant-1"
        )

        assert result is True

    async def test_mandatory_template_cannot_be_opted_out(self):
        """Mandatory templates always return False even if in opt-out list."""
        # Even if somehow the opt-out list contains a mandatory key,
        # is_template_opted_out should return False
        pref = _preference_doc(
            template_opt_outs=["past_due_invoice", "e_bol_delivery"]
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        for mandatory_key in MANDATORY_TEMPLATE_KEYS:
            result = await resolver.is_template_opted_out(
                "cust-1", mandatory_key, "tenant-1"
            )
            assert result is False, f"Mandatory template '{mandatory_key}' should never be opted out"

    async def test_returns_false_when_opt_outs_field_missing(self):
        """Returns False when the preference doc has no template_opt_outs field."""
        pref = _preference_doc()
        # Remove the field to simulate legacy documents
        del pref["template_opt_outs"]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.is_template_opted_out(
            "cust-1", "low_tank_autofill_alert", "tenant-1"
        )

        assert result is False


# ---------------------------------------------------------------------------
# update_template_opt_outs
# ---------------------------------------------------------------------------


class TestUpdateTemplateOptOuts:
    """Tests for PreferenceResolver.update_template_opt_outs."""

    async def test_customer_can_opt_out_of_template(self):
        """Successfully updates opt-out list for a customer."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.update_template_opt_outs(
            "cust-1", "tenant-1", ["low_tank_autofill_alert"]
        )

        assert "low_tank_autofill_alert" in result["template_opt_outs"]
        es.update_document.assert_called_once()
        call_args = es.update_document.call_args
        partial_doc = call_args[0][2]
        assert "low_tank_autofill_alert" in partial_doc["template_opt_outs"]

    async def test_rejects_mandatory_template_opt_out(self):
        """Raises validation error when trying to opt out of mandatory templates."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        with pytest.raises(AppException) as exc_info:
            await resolver.update_template_opt_outs(
                "cust-1", "tenant-1", ["past_due_invoice"]
            )

        assert exc_info.value.status_code == 400
        assert "mandatory" in exc_info.value.message.lower()

    async def test_rejects_e_bol_delivery_opt_out(self):
        """Raises validation error when trying to opt out of e_bol_delivery."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        with pytest.raises(AppException) as exc_info:
            await resolver.update_template_opt_outs(
                "cust-1", "tenant-1", ["e_bol_delivery"]
            )

        assert exc_info.value.status_code == 400

    async def test_raises_404_when_no_preference_exists(self):
        """Raises 404 when no preference document exists for the customer."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        with pytest.raises(AppException) as exc_info:
            await resolver.update_template_opt_outs(
                "cust-missing", "tenant-1", ["low_tank_autofill_alert"]
            )

        assert exc_info.value.status_code == 404

    async def test_deduplicates_opt_out_list(self):
        """Deduplicates template keys in the opt-out list."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.update_template_opt_outs(
            "cust-1",
            "tenant-1",
            ["low_tank_autofill_alert", "low_tank_autofill_alert", "delivery_completed"],
        )

        # Should be deduplicated
        assert len(result["template_opt_outs"]) == 2
        assert "low_tank_autofill_alert" in result["template_opt_outs"]
        assert "delivery_completed" in result["template_opt_outs"]

    async def test_can_clear_all_opt_outs(self):
        """Passing an empty list clears all opt-outs (re-enables all templates)."""
        pref = _preference_doc(template_opt_outs=["low_tank_autofill_alert"])
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.update_template_opt_outs(
            "cust-1", "tenant-1", []
        )

        assert result["template_opt_outs"] == []

    async def test_multiple_non_mandatory_templates_can_be_opted_out(self):
        """Multiple non-mandatory templates can be opted out simultaneously."""
        pref = _preference_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([pref]))
        resolver = _make_resolver(es)

        result = await resolver.update_template_opt_outs(
            "cust-1",
            "tenant-1",
            ["low_tank_autofill_alert", "delivery_completed"],
        )

        assert set(result["template_opt_outs"]) == {
            "low_tank_autofill_alert",
            "delivery_completed",
        }


# ---------------------------------------------------------------------------
# Default state — all templates enabled
# ---------------------------------------------------------------------------


class TestDefaultOptOutState:
    """Tests verifying the default state is all templates enabled."""

    async def test_new_preference_has_empty_opt_outs(self):
        """A newly created preference has an empty template_opt_outs list."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        result = await resolver.upsert_preference("cust-new", "tenant-1", {
            "customer_name": "New Customer",
            "channels": {"email": "new@example.com"},
        })

        assert result["template_opt_outs"] == []

    async def test_no_preference_means_all_enabled(self):
        """When no preference exists, is_template_opted_out returns False for all templates."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        resolver = _make_resolver(es)

        all_template_keys = [
            "low_tank_autofill_alert",
            "past_due_invoice",
            "delivery_completed",
            "e_bol_delivery",
        ]

        for key in all_template_keys:
            result = await resolver.is_template_opted_out("cust-1", key, "tenant-1")
            assert result is False, f"Template '{key}' should be enabled by default"


# ---------------------------------------------------------------------------
# MANDATORY_TEMPLATE_KEYS constant
# ---------------------------------------------------------------------------


class TestMandatoryTemplateKeys:
    """Tests verifying the mandatory template keys constant."""

    def test_mandatory_keys_are_defined(self):
        """MANDATORY_TEMPLATE_KEYS contains expected regulatory templates."""
        assert "past_due_invoice" in MANDATORY_TEMPLATE_KEYS
        assert "e_bol_delivery" in MANDATORY_TEMPLATE_KEYS

    def test_mandatory_keys_is_frozen(self):
        """MANDATORY_TEMPLATE_KEYS is immutable (frozenset)."""
        assert isinstance(MANDATORY_TEMPLATE_KEYS, frozenset)

    def test_non_mandatory_templates_not_in_set(self):
        """Non-mandatory templates are not in MANDATORY_TEMPLATE_KEYS."""
        assert "low_tank_autofill_alert" not in MANDATORY_TEMPLATE_KEYS
        assert "delivery_completed" not in MANDATORY_TEMPLATE_KEYS
