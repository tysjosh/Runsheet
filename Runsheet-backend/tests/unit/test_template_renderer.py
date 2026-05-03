"""
Unit tests for TemplateRenderer — template rendering and management.

Tests render, list_templates, update_template, and initialize_default_templates
against a mocked ElasticsearchService.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from notifications.services.template_renderer import (
    TemplateRenderer,
    SafeDict,
    render_template,
    DEFAULT_TEMPLATES,
    NOTIFICATION_TEMPLATES_INDEX,
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


def _make_renderer(es_mock: MagicMock) -> TemplateRenderer:
    """Create a TemplateRenderer with a mocked ES service."""
    return TemplateRenderer(es_service=es_mock)


def _template_doc(
    template_id: str = "tmpl-abc",
    tenant_id: str = "tenant-1",
    event_type: str = "delay_alert",
    channel: str = "sms",
    subject_template: str = "Delay Alert — Order {order_id}",
    body_template: str = "Your delivery {order_id} is delayed by {delay_minutes} minutes. New ETA: {new_eta}",
    placeholders: list[str] | None = None,
) -> dict:
    """Return a sample template document."""
    return {
        "template_id": template_id,
        "tenant_id": tenant_id,
        "event_type": event_type,
        "channel": channel,
        "subject_template": subject_template,
        "body_template": body_template,
        "placeholders": placeholders or ["order_id", "delay_minutes", "new_eta"],
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
# SafeDict
# ---------------------------------------------------------------------------


class TestSafeDict:
    """Tests for the SafeDict helper class."""

    def test_returns_value_for_existing_key(self):
        sd = SafeDict({"name": "Alice"})
        assert sd["name"] == "Alice"

    def test_returns_missing_for_absent_key(self):
        sd = SafeDict({"name": "Alice"})
        assert sd["unknown_key"] == "[missing]"

    def test_returns_missing_for_empty_dict(self):
        sd = SafeDict()
        assert sd["anything"] == "[missing]"


# ---------------------------------------------------------------------------
# render_template (module-level function)
# ---------------------------------------------------------------------------


class TestRenderTemplateFunction:
    """Tests for the render_template helper function."""

    def test_replaces_all_placeholders(self):
        result = render_template(
            "Order {order_id} delayed by {delay_minutes} min",
            {"order_id": "ORD-123", "delay_minutes": "15"},
        )
        assert result == "Order ORD-123 delayed by 15 min"

    def test_missing_placeholder_produces_missing_marker(self):
        result = render_template(
            "Order {order_id} ETA: {new_eta}",
            {"order_id": "ORD-123"},
        )
        assert result == "Order ORD-123 ETA: [missing]"

    def test_all_placeholders_missing(self):
        result = render_template(
            "{order_id} — {status}",
            {},
        )
        assert result == "[missing] — [missing]"

    def test_empty_template_string(self):
        result = render_template("", {"order_id": "ORD-1"})
        assert result == ""

    def test_no_placeholders_in_template(self):
        result = render_template("Hello, world!", {"order_id": "ORD-1"})
        assert result == "Hello, world!"

    def test_extra_data_keys_are_ignored(self):
        result = render_template(
            "Order {order_id}",
            {"order_id": "ORD-1", "extra_key": "ignored"},
        )
        assert result == "Order ORD-1"

    def test_special_characters_in_values(self):
        result = render_template(
            "Customer: {name}",
            {"name": "O'Brien & Co."},
        )
        assert result == "Customer: O'Brien & Co."


# ---------------------------------------------------------------------------
# TemplateRenderer.render
# ---------------------------------------------------------------------------


class TestRender:
    """Tests for TemplateRenderer.render."""

    async def test_renders_subject_and_body(self):
        tmpl = _template_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([tmpl]))
        renderer = _make_renderer(es)

        result = await renderer.render(
            "tmpl-abc",
            {"order_id": "ORD-99", "delay_minutes": "30", "new_eta": "14:00"},
            "tenant-1",
        )

        assert result["subject"] == "Delay Alert — Order ORD-99"
        assert "ORD-99" in result["body"]
        assert "30" in result["body"]
        assert "14:00" in result["body"]

    async def test_missing_placeholder_in_body(self):
        tmpl = _template_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([tmpl]))
        renderer = _make_renderer(es)

        result = await renderer.render(
            "tmpl-abc",
            {"order_id": "ORD-99"},  # missing delay_minutes and new_eta
            "tenant-1",
        )

        assert "[missing]" in result["body"]
        assert "ORD-99" in result["body"]

    async def test_empty_subject_template(self):
        tmpl = _template_doc(subject_template="")
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([tmpl]))
        renderer = _make_renderer(es)

        result = await renderer.render("tmpl-abc", {"order_id": "ORD-1"}, "tenant-1")

        assert result["subject"] == ""

    async def test_none_subject_template(self):
        tmpl = _template_doc(subject_template=None)
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([tmpl]))
        renderer = _make_renderer(es)

        result = await renderer.render("tmpl-abc", {"order_id": "ORD-1"}, "tenant-1")

        assert result["subject"] == ""

    async def test_raises_404_when_template_not_found(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        renderer = _make_renderer(es)

        with pytest.raises(AppException) as exc_info:
            await renderer.render("missing-id", {}, "tenant-1")

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TemplateRenderer.list_templates
# ---------------------------------------------------------------------------


class TestListTemplates:
    """Tests for TemplateRenderer.list_templates."""

    async def test_returns_all_templates_for_tenant(self):
        templates = [
            _template_doc(event_type="delay_alert", channel="sms"),
            _template_doc(event_type="delay_alert", channel="email"),
        ]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(templates))
        renderer = _make_renderer(es)

        result = await renderer.list_templates("tenant-1")

        assert len(result) == 2

    async def test_filters_by_event_type(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        renderer = _make_renderer(es)

        await renderer.list_templates("tenant-1", event_type="delay_alert")

        query = es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"event_type": "delay_alert"}} in must

    async def test_filters_by_channel(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        renderer = _make_renderer(es)

        await renderer.list_templates("tenant-1", channel="email")

        query = es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"channel": "email"}} in must

    async def test_filters_by_both_event_type_and_channel(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        renderer = _make_renderer(es)

        await renderer.list_templates("tenant-1", event_type="eta_change", channel="sms")

        query = es.search_documents.call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"event_type": "eta_change"}} in must
        assert {"term": {"channel": "sms"}} in must

    async def test_returns_empty_list_when_no_templates(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        renderer = _make_renderer(es)

        result = await renderer.list_templates("tenant-1")

        assert result == []


# ---------------------------------------------------------------------------
# TemplateRenderer.update_template
# ---------------------------------------------------------------------------


class TestUpdateTemplate:
    """Tests for TemplateRenderer.update_template."""

    async def test_updates_body_template(self):
        tmpl = _template_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([tmpl]))
        renderer = _make_renderer(es)

        result = await renderer.update_template(
            "tmpl-abc", "tenant-1", {"body_template": "New body {order_id}"}
        )

        assert result["body_template"] == "New body {order_id}"
        es.update_document.assert_called_once()
        call_args = es.update_document.call_args
        assert call_args[0][0] == NOTIFICATION_TEMPLATES_INDEX
        assert call_args[0][1] == "tmpl-abc"
        assert call_args[0][2]["body_template"] == "New body {order_id}"

    async def test_updates_subject_template(self):
        tmpl = _template_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([tmpl]))
        renderer = _make_renderer(es)

        result = await renderer.update_template(
            "tmpl-abc", "tenant-1", {"subject_template": "New Subject"}
        )

        assert result["subject_template"] == "New Subject"

    async def test_rejects_disallowed_fields(self):
        es = _make_es_mock()
        renderer = _make_renderer(es)

        with pytest.raises(AppException) as exc_info:
            await renderer.update_template(
                "tmpl-abc", "tenant-1", {"event_type": "hacked"}
            )

        assert exc_info.value.status_code == 400

    async def test_raises_404_when_template_not_found(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        renderer = _make_renderer(es)

        with pytest.raises(AppException) as exc_info:
            await renderer.update_template(
                "missing-id", "tenant-1", {"body_template": "x"}
            )

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TemplateRenderer.initialize_default_templates
# ---------------------------------------------------------------------------


class TestInitializeDefaultTemplates:
    """Tests for TemplateRenderer.initialize_default_templates."""

    async def test_creates_all_12_default_templates(self):
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        renderer = _make_renderer(es)

        await renderer.initialize_default_templates("tenant-1")

        assert es.index_document.call_count == 12  # 4 event types × 3 channels

        indexed_keys = set()
        for call in es.index_document.call_args_list:
            index_name = call[0][0]
            doc = call[0][2]
            assert index_name == NOTIFICATION_TEMPLATES_INDEX
            assert doc["tenant_id"] == "tenant-1"
            assert doc["template_id"]  # non-empty UUID
            assert doc["created_at"]
            assert doc["updated_at"]
            assert doc["body_template"]  # non-empty
            indexed_keys.add((doc["event_type"], doc["channel"]))

        # Verify all 12 combinations
        expected_event_types = {
            "delivery_confirmation", "delay_alert", "eta_change", "order_status_update"
        }
        expected_channels = {"sms", "email", "whatsapp"}
        expected_keys = {
            (et, ch) for et in expected_event_types for ch in expected_channels
        }
        assert indexed_keys == expected_keys

    async def test_skips_existing_templates(self):
        existing = [
            _template_doc(event_type="delay_alert", channel="sms"),
            _template_doc(event_type="delay_alert", channel="email"),
            _template_doc(event_type="delay_alert", channel="whatsapp"),
        ]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(existing))
        renderer = _make_renderer(es)

        await renderer.initialize_default_templates("tenant-1")

        # 12 total - 3 existing = 9 new
        assert es.index_document.call_count == 9
        created_keys = {
            (call[0][2]["event_type"], call[0][2]["channel"])
            for call in es.index_document.call_args_list
        }
        # None of the created templates should be delay_alert
        for key in created_keys:
            assert key[0] != "delay_alert"

    async def test_no_creates_when_all_exist(self):
        existing = [
            _template_doc(event_type=et, channel=ch)
            for et in ["delivery_confirmation", "delay_alert", "eta_change", "order_status_update"]
            for ch in ["sms", "email", "whatsapp"]
        ]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(existing))
        renderer = _make_renderer(es)

        await renderer.initialize_default_templates("tenant-1")

        es.index_document.assert_not_called()

    async def test_default_templates_have_valid_placeholders(self):
        """Verify all default templates use valid {placeholder} syntax."""
        for tmpl_def in DEFAULT_TEMPLATES:
            body = tmpl_def["body_template"]
            subject = tmpl_def.get("subject_template", "")
            placeholders = tmpl_def["placeholders"]

            # Build a data dict with all placeholders set to test values
            data = {p: f"test_{p}" for p in placeholders}

            # Rendering should not raise and should replace all placeholders
            rendered_body = render_template(body, data)
            assert "[missing]" not in rendered_body, (
                f"Default template {tmpl_def['event_type']}/{tmpl_def['channel']} "
                f"has placeholders not listed in its placeholders field"
            )

            if subject:
                rendered_subject = render_template(subject, data)
                assert "[missing]" not in rendered_subject
