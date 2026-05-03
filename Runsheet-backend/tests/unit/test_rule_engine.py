"""
Unit tests for RuleEngine — notification rule evaluation and management.

Tests evaluate_rule, list_rules, update_rule, and initialize_default_rules
against a mocked ElasticsearchService.

Requirements: 1.7, 1.8, 7.1, 7.2, 7.3, 7.4, 7.5
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from notifications.services.rule_engine import (
    RuleEngine,
    DEFAULT_CHANNELS,
    DEFAULT_EVENT_TYPES,
    NOTIFICATION_RULES_INDEX,
)
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


def _make_engine(es_mock: MagicMock) -> RuleEngine:
    """Create a RuleEngine with a mocked ES service."""
    return RuleEngine(es_service=es_mock)


def _rule_doc(
    event_type: str = "delay_alert",
    tenant_id: str = "tenant-1",
    enabled: bool = True,
    rule_id: str = "rule-abc",
) -> dict:
    """Return a sample rule document."""
    return {
        "rule_id": rule_id,
        "tenant_id": tenant_id,
        "event_type": event_type,
        "enabled": enabled,
        "default_channels": ["sms", "email", "whatsapp"],
        "template_id": None,
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
# evaluate_rule
# ---------------------------------------------------------------------------


class TestEvaluateRule:
    """Tests for RuleEngine.evaluate_rule."""

    async def test_returns_rule_when_enabled(self):
        """An enabled rule is returned as a dict."""
        rule = _rule_doc(enabled=True)
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([rule]))
        engine = _make_engine(es)

        result = await engine.evaluate_rule("delay_alert", "tenant-1")

        assert result is not None
        assert result["rule_id"] == "rule-abc"
        assert result["enabled"] is True

    async def test_returns_none_when_disabled(self):
        """A disabled rule causes evaluate_rule to return None."""
        rule = _rule_doc(enabled=False)
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([rule]))
        engine = _make_engine(es)

        result = await engine.evaluate_rule("delay_alert", "tenant-1")

        assert result is None

    async def test_returns_none_when_not_found(self):
        """No matching rule returns None."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        engine = _make_engine(es)

        result = await engine.evaluate_rule("unknown_event", "tenant-1")

        assert result is None

    async def test_queries_correct_index_and_filters(self):
        """Verify the ES query uses the correct index, event_type, and tenant_id."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        engine = _make_engine(es)

        await engine.evaluate_rule("eta_change", "tenant-42")

        es.search_documents.assert_called_once()
        call_args = es.search_documents.call_args
        assert call_args[0][0] == NOTIFICATION_RULES_INDEX
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"event_type": "eta_change"}} in must
        assert {"term": {"tenant_id": "tenant-42"}} in must


# ---------------------------------------------------------------------------
# list_rules
# ---------------------------------------------------------------------------


class TestListRules:
    """Tests for RuleEngine.list_rules."""

    async def test_returns_all_rules_for_tenant(self):
        """list_rules returns all rules for the given tenant."""
        rules = [
            _rule_doc(event_type="delay_alert"),
            _rule_doc(event_type="eta_change"),
        ]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(rules))
        engine = _make_engine(es)

        result = await engine.list_rules("tenant-1")

        assert len(result) == 2
        assert result[0]["event_type"] == "delay_alert"
        assert result[1]["event_type"] == "eta_change"

    async def test_returns_empty_list_when_no_rules(self):
        """list_rules returns an empty list when no rules exist."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        engine = _make_engine(es)

        result = await engine.list_rules("tenant-1")

        assert result == []


# ---------------------------------------------------------------------------
# update_rule
# ---------------------------------------------------------------------------


class TestUpdateRule:
    """Tests for RuleEngine.update_rule."""

    async def test_updates_enabled_field(self):
        """update_rule can toggle the enabled field."""
        rule = _rule_doc(enabled=True)
        es = _make_es_mock()
        # First call: _get_rule lookup; second call would be list_rules if needed
        es.search_documents = AsyncMock(return_value=_es_response([rule]))
        engine = _make_engine(es)

        result = await engine.update_rule("rule-abc", "tenant-1", {"enabled": False})

        assert result["enabled"] is False
        es.update_document.assert_called_once()
        call_args = es.update_document.call_args
        assert call_args[0][0] == NOTIFICATION_RULES_INDEX
        assert call_args[0][1] == "rule-abc"
        assert call_args[0][2]["enabled"] is False

    async def test_updates_default_channels(self):
        """update_rule can change default_channels."""
        rule = _rule_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([rule]))
        engine = _make_engine(es)

        result = await engine.update_rule(
            "rule-abc", "tenant-1", {"default_channels": ["sms"]}
        )

        assert result["default_channels"] == ["sms"]

    async def test_updates_template_id(self):
        """update_rule can set template_id."""
        rule = _rule_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([rule]))
        engine = _make_engine(es)

        result = await engine.update_rule(
            "rule-abc", "tenant-1", {"template_id": "tmpl-xyz"}
        )

        assert result["template_id"] == "tmpl-xyz"

    async def test_rejects_disallowed_fields(self):
        """update_rule raises validation error for disallowed fields."""
        es = _make_es_mock()
        engine = _make_engine(es)

        with pytest.raises(AppException) as exc_info:
            await engine.update_rule(
                "rule-abc", "tenant-1", {"event_type": "hacked"}
            )

        assert exc_info.value.status_code == 400

    async def test_raises_404_when_rule_not_found(self):
        """update_rule raises 404 when the rule does not exist."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        engine = _make_engine(es)

        with pytest.raises(AppException) as exc_info:
            await engine.update_rule("missing-id", "tenant-1", {"enabled": False})

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# initialize_default_rules
# ---------------------------------------------------------------------------


class TestInitializeDefaultRules:
    """Tests for RuleEngine.initialize_default_rules."""

    async def test_creates_rules_for_all_event_types(self):
        """initialize_default_rules creates one rule per event type."""
        es = _make_es_mock()
        # list_rules returns empty — no existing rules
        es.search_documents = AsyncMock(return_value=_es_response([]))
        engine = _make_engine(es)

        await engine.initialize_default_rules("tenant-1")

        assert es.index_document.call_count == len(DEFAULT_EVENT_TYPES)

        # Verify each call indexed to the correct index with expected fields
        indexed_event_types = set()
        for call in es.index_document.call_args_list:
            index_name = call[0][0]
            doc = call[0][2]
            assert index_name == NOTIFICATION_RULES_INDEX
            assert doc["tenant_id"] == "tenant-1"
            assert doc["enabled"] is True
            assert doc["default_channels"] == list(DEFAULT_CHANNELS)
            assert doc["rule_id"]  # non-empty UUID
            assert doc["created_at"]
            assert doc["updated_at"]
            indexed_event_types.add(doc["event_type"])

        assert indexed_event_types == set(DEFAULT_EVENT_TYPES)

    async def test_skips_existing_rules(self):
        """initialize_default_rules does not overwrite existing rules."""
        existing = [
            _rule_doc(event_type="delay_alert"),
            _rule_doc(event_type="eta_change"),
        ]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(existing))
        engine = _make_engine(es)

        await engine.initialize_default_rules("tenant-1")

        # Only 2 of 4 event types should be created
        assert es.index_document.call_count == 2
        created_types = {
            call[0][2]["event_type"] for call in es.index_document.call_args_list
        }
        assert created_types == {"delivery_confirmation", "order_status_update"}

    async def test_no_creates_when_all_exist(self):
        """initialize_default_rules does nothing when all rules already exist."""
        existing = [_rule_doc(event_type=et) for et in DEFAULT_EVENT_TYPES]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(existing))
        engine = _make_engine(es)

        await engine.initialize_default_rules("tenant-1")

        es.index_document.assert_not_called()
