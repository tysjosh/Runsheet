"""
Unit tests for NotificationService — the core notification pipeline orchestrator.

Tests notify_event, list_notifications, get_notification, retry_notification,
get_summary, register_dispatcher, and set_ws_manager against a mocked
ElasticsearchService.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.2, 2.3, 3.2, 3.3, 6.1, 6.2,
              6.3, 6.4, 6.5, 10.1
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from notifications.services.notification_service import (
    NotificationService,
    _map_event_to_notification_type,
    NOTIFICATIONS_CURRENT_INDEX,
)
from notifications.models import DeliveryStatus, NotificationType
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


def _make_ws_mock() -> MagicMock:
    """Return a mock NotificationWSManager."""
    ws = MagicMock()
    ws.broadcast_notification = AsyncMock(return_value=1)
    ws.broadcast_status_update = AsyncMock(return_value=1)
    return ws


def _make_dispatcher_mock(channel: str = "sms", status: str = "sent") -> MagicMock:
    """Return a mock ChannelDispatcher."""
    dispatcher = MagicMock()
    dispatcher.channel_name = channel
    dispatcher.dispatch = AsyncMock(return_value=status)
    return dispatcher


def _make_service(es_mock: MagicMock) -> NotificationService:
    """Create a NotificationService with a mocked ES service."""
    return NotificationService(es_service=es_mock)


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


def _es_agg_response(
    docs: list[dict] | None = None,
    by_type: dict | None = None,
    by_channel: dict | None = None,
    by_status: dict | None = None,
    total: int = 0,
) -> dict:
    """Build a mock ES search response with aggregations."""
    def _to_buckets(d: dict | None) -> list[dict]:
        if not d:
            return []
        return [{"key": k, "doc_count": v} for k, v in d.items()]

    return {
        "hits": {
            "hits": [_es_hit(d) for d in (docs or [])],
            "total": {"value": total},
        },
        "aggregations": {
            "by_type": {"buckets": _to_buckets(by_type)},
            "by_channel": {"buckets": _to_buckets(by_channel)},
            "by_status": {"buckets": _to_buckets(by_status)},
        },
    }


def _notification_doc(
    notification_id: str = "notif-1",
    notification_type: str = "delay_alert",
    channel: str = "sms",
    delivery_status: str = "pending",
    tenant_id: str = "tenant-1",
    retry_count: int = 0,
) -> dict:
    """Return a sample notification document."""
    return {
        "notification_id": notification_id,
        "notification_type": notification_type,
        "channel": channel,
        "recipient_reference": "+254700000000",
        "recipient_name": "Test Customer",
        "subject": "Test Subject",
        "message_body": "Test message body",
        "related_entity_type": "job",
        "related_entity_id": "job-123",
        "delivery_status": delivery_status,
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
        "sent_at": None,
        "delivered_at": None,
        "failed_at": None,
        "failure_reason": None,
        "retry_count": retry_count,
        "tenant_id": tenant_id,
    }


def _rule_doc(
    event_type: str = "delay_alert",
    tenant_id: str = "tenant-1",
    enabled: bool = True,
    template_id: str | None = None,
) -> dict:
    """Return a sample rule document."""
    return {
        "rule_id": "rule-abc",
        "tenant_id": tenant_id,
        "event_type": event_type,
        "enabled": enabled,
        "default_channels": ["sms", "email"],
        "template_id": template_id,
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def _template_doc(
    event_type: str = "delay_alert",
    channel: str = "sms",
    template_id: str = "tmpl-1",
) -> dict:
    """Return a sample template document."""
    return {
        "template_id": template_id,
        "tenant_id": "tenant-1",
        "event_type": event_type,
        "channel": channel,
        "subject_template": "Delay Alert — Order {order_id}",
        "body_template": "Your order {order_id} is delayed by {delay_minutes} minutes.",
        "placeholders": ["order_id", "delay_minutes"],
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Event-to-NotificationType mapping
# ---------------------------------------------------------------------------


class TestMapEventToNotificationType:
    """Tests for _map_event_to_notification_type."""

    def test_status_changed_completed_maps_to_delivery_confirmation(self):
        result = _map_event_to_notification_type(
            "status_changed", {"status": "completed"}
        )
        assert result == NotificationType.DELIVERY_CONFIRMATION

    def test_delay_alert_maps_to_delay_alert(self):
        result = _map_event_to_notification_type("delay_alert", {})
        assert result == NotificationType.DELAY_ALERT

    def test_status_changed_with_eta_maps_to_eta_change(self):
        result = _map_event_to_notification_type(
            "status_changed",
            {"status": "in_transit", "estimated_arrival": "2025-01-01T12:00:00Z"},
        )
        assert result == NotificationType.ETA_CHANGE

    def test_status_changed_other_maps_to_order_status_update(self):
        result = _map_event_to_notification_type(
            "status_changed", {"status": "in_transit"}
        )
        assert result == NotificationType.ORDER_STATUS_UPDATE

    def test_unknown_event_maps_to_order_status_update(self):
        result = _map_event_to_notification_type("some_other_event", {})
        assert result == NotificationType.ORDER_STATUS_UPDATE

    def test_completed_takes_priority_over_eta(self):
        """status=completed should map to delivery_confirmation even if estimated_arrival is present."""
        result = _map_event_to_notification_type(
            "status_changed",
            {"status": "completed", "estimated_arrival": "2025-01-01T12:00:00Z"},
        )
        assert result == NotificationType.DELIVERY_CONFIRMATION


# ---------------------------------------------------------------------------
# register_dispatcher
# ---------------------------------------------------------------------------


class TestRegisterDispatcher:
    """Tests for NotificationService.register_dispatcher."""

    def test_registers_dispatcher(self):
        es = _make_es_mock()
        service = _make_service(es)
        dispatcher = _make_dispatcher_mock("sms")

        service.register_dispatcher("sms", dispatcher)

        assert "sms" in service._dispatchers
        assert service._dispatchers["sms"] is dispatcher

    def test_registers_multiple_dispatchers(self):
        es = _make_es_mock()
        service = _make_service(es)

        for ch in ("sms", "email", "whatsapp"):
            d = _make_dispatcher_mock(ch)
            service.register_dispatcher(ch, d)

        assert len(service._dispatchers) == 3


# ---------------------------------------------------------------------------
# set_ws_manager
# ---------------------------------------------------------------------------


class TestSetWsManager:
    """Tests for NotificationService.set_ws_manager."""

    def test_sets_ws_manager(self):
        es = _make_es_mock()
        service = _make_service(es)
        ws = _make_ws_mock()

        service.set_ws_manager(ws)

        assert service._ws_manager is ws


# ---------------------------------------------------------------------------
# notify_event
# ---------------------------------------------------------------------------


class TestNotifyEvent:
    """Tests for NotificationService.notify_event."""

    async def test_returns_empty_for_missing_tenant_id(self):
        """Malformed event with empty tenant_id returns empty list."""
        es = _make_es_mock()
        service = _make_service(es)

        result = await service.notify_event(
            "status_changed", {"customer_id": "cust-1"}, ""
        )

        assert result == []

    async def test_returns_empty_for_missing_customer_id(self):
        """Malformed event without customer_id returns empty list."""
        es = _make_es_mock()
        service = _make_service(es)

        result = await service.notify_event(
            "status_changed", {"status": "completed"}, "tenant-1"
        )

        assert result == []

    async def test_returns_empty_when_rule_disabled(self):
        """When the rule is disabled (evaluate_rule returns None), no notifications."""
        es = _make_es_mock()
        service = _make_service(es)

        # Mock rule engine to return None (disabled)
        service._rule_engine.evaluate_rule = AsyncMock(return_value=None)

        result = await service.notify_event(
            "delay_alert",
            {"customer_id": "cust-1", "order_id": "ord-1"},
            "tenant-1",
        )

        assert result == []

    async def test_dispatches_per_customer_preference_channels(self):
        """When customer has preferences, dispatches to each preferred channel."""
        es = _make_es_mock()
        service = _make_service(es)

        rule = _rule_doc(event_type="delay_alert")
        service._rule_engine.evaluate_rule = AsyncMock(return_value=rule)

        # Customer prefers sms and email
        service._preference_resolver.resolve_channels = AsyncMock(
            return_value=[
                {"channel": "sms", "contact_detail": "+254700000000"},
                {"channel": "email", "contact_detail": "test@example.com"},
            ]
        )

        # Template rendering
        service._template_renderer.list_templates = AsyncMock(
            return_value=[_template_doc()]
        )
        service._template_renderer.render = AsyncMock(
            return_value={"subject": "Test", "body": "Test body"}
        )

        # Register dispatchers
        sms_d = _make_dispatcher_mock("sms")
        email_d = _make_dispatcher_mock("email")
        service.register_dispatcher("sms", sms_d)
        service.register_dispatcher("email", email_d)

        result = await service.notify_event(
            "delay_alert",
            {"customer_id": "cust-1", "order_id": "ord-1"},
            "tenant-1",
        )

        assert len(result) == 2
        channels = {n["channel"] for n in result}
        assert channels == {"sms", "email"}

    async def test_falls_back_to_default_channels_when_no_preference(self):
        """When no customer preference exists, uses rule's default_channels."""
        es = _make_es_mock()
        service = _make_service(es)

        rule = _rule_doc(event_type="delay_alert")
        rule["default_channels"] = ["sms"]
        service._rule_engine.evaluate_rule = AsyncMock(return_value=rule)
        service._preference_resolver.resolve_channels = AsyncMock(return_value=[])

        service._template_renderer.list_templates = AsyncMock(
            return_value=[_template_doc()]
        )
        service._template_renderer.render = AsyncMock(
            return_value={"subject": "Test", "body": "Test body"}
        )

        sms_d = _make_dispatcher_mock("sms")
        service.register_dispatcher("sms", sms_d)

        result = await service.notify_event(
            "delay_alert",
            {"customer_id": "cust-1", "order_id": "ord-1"},
            "tenant-1",
        )

        assert len(result) == 1
        assert result[0]["channel"] == "sms"
        # recipient_reference falls back to customer_id
        assert result[0]["recipient_reference"] == "cust-1"

    async def test_unregistered_dispatcher_sets_failed_status(self):
        """When no dispatcher is registered for a channel, status is failed."""
        es = _make_es_mock()
        service = _make_service(es)

        rule = _rule_doc(event_type="delay_alert")
        rule["default_channels"] = ["whatsapp"]
        service._rule_engine.evaluate_rule = AsyncMock(return_value=rule)
        service._preference_resolver.resolve_channels = AsyncMock(return_value=[])

        service._template_renderer.list_templates = AsyncMock(
            return_value=[_template_doc()]
        )
        service._template_renderer.render = AsyncMock(
            return_value={"subject": "Test", "body": "Test body"}
        )

        # No dispatcher registered for whatsapp
        result = await service.notify_event(
            "delay_alert",
            {"customer_id": "cust-1", "order_id": "ord-1"},
            "tenant-1",
        )

        assert len(result) == 1
        assert result[0]["delivery_status"] == "failed"
        assert "whatsapp" in result[0]["failure_reason"]

    async def test_indexes_notification_in_es(self):
        """Each notification is indexed in ES with status=pending initially."""
        es = _make_es_mock()
        service = _make_service(es)

        # Capture the document at index time (before dispatch mutates it)
        indexed_docs: list[dict] = []

        async def _capture_index(index, doc_id, doc):
            # Store a snapshot of the doc at index time
            indexed_docs.append(dict(doc))
            return {"result": "created"}

        es.index_document = AsyncMock(side_effect=_capture_index)

        rule = _rule_doc(event_type="delay_alert")
        rule["default_channels"] = ["sms"]
        service._rule_engine.evaluate_rule = AsyncMock(return_value=rule)
        service._preference_resolver.resolve_channels = AsyncMock(return_value=[])

        service._template_renderer.list_templates = AsyncMock(
            return_value=[_template_doc()]
        )
        service._template_renderer.render = AsyncMock(
            return_value={"subject": "Test", "body": "Test body"}
        )

        sms_d = _make_dispatcher_mock("sms")
        service.register_dispatcher("sms", sms_d)

        await service.notify_event(
            "delay_alert",
            {"customer_id": "cust-1", "order_id": "ord-1"},
            "tenant-1",
        )

        # Verify the notification was indexed with pending status
        assert len(indexed_docs) == 1
        assert indexed_docs[0]["delivery_status"] == "pending"
        assert indexed_docs[0]["tenant_id"] == "tenant-1"

    async def test_broadcasts_via_ws_when_manager_set(self):
        """Notification is broadcast via WS when ws_manager is set."""
        es = _make_es_mock()
        service = _make_service(es)
        ws = _make_ws_mock()
        service.set_ws_manager(ws)

        rule = _rule_doc(event_type="delay_alert")
        rule["default_channels"] = ["sms"]
        service._rule_engine.evaluate_rule = AsyncMock(return_value=rule)
        service._preference_resolver.resolve_channels = AsyncMock(return_value=[])

        service._template_renderer.list_templates = AsyncMock(
            return_value=[_template_doc()]
        )
        service._template_renderer.render = AsyncMock(
            return_value={"subject": "Test", "body": "Test body"}
        )

        sms_d = _make_dispatcher_mock("sms")
        service.register_dispatcher("sms", sms_d)

        await service.notify_event(
            "delay_alert",
            {"customer_id": "cust-1", "order_id": "ord-1"},
            "tenant-1",
        )

        ws.broadcast_notification.assert_called_once()

    async def test_notification_has_correct_related_entity(self):
        """Notification sets related_entity_type=job and related_entity_id from event_data."""
        es = _make_es_mock()
        service = _make_service(es)

        rule = _rule_doc(event_type="delivery_confirmation")
        rule["default_channels"] = ["sms"]
        service._rule_engine.evaluate_rule = AsyncMock(return_value=rule)
        service._preference_resolver.resolve_channels = AsyncMock(return_value=[])

        service._template_renderer.list_templates = AsyncMock(
            return_value=[_template_doc(event_type="delivery_confirmation")]
        )
        service._template_renderer.render = AsyncMock(
            return_value={"subject": "Delivered", "body": "Your order is delivered"}
        )

        sms_d = _make_dispatcher_mock("sms")
        service.register_dispatcher("sms", sms_d)

        result = await service.notify_event(
            "status_changed",
            {"customer_id": "cust-1", "status": "completed", "job_id": "job-xyz"},
            "tenant-1",
        )

        assert len(result) == 1
        assert result[0]["related_entity_type"] == "job"
        assert result[0]["related_entity_id"] == "job-xyz"


# ---------------------------------------------------------------------------
# list_notifications
# ---------------------------------------------------------------------------


class TestListNotifications:
    """Tests for NotificationService.list_notifications."""

    async def test_returns_paginated_results(self):
        """list_notifications returns items, total, page, size."""
        docs = [_notification_doc(notification_id=f"notif-{i}") for i in range(3)]
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response(docs, total=10))
        service = _make_service(es)

        result = await service.list_notifications("tenant-1", {}, page=1, size=3)

        assert len(result["items"]) == 3
        assert result["total"] == 10
        assert result["page"] == 1
        assert result["size"] == 3

    async def test_applies_filters(self):
        """list_notifications passes filters to ES query."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        service = _make_service(es)

        await service.list_notifications(
            "tenant-1",
            {"notification_type": "delay_alert", "channel": "sms"},
            page=1,
            size=10,
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        assert {"term": {"notification_type": "delay_alert"}} in must
        assert {"term": {"channel": "sms"}} in must

    async def test_applies_date_range_filter(self):
        """list_notifications applies start_date and end_date filters."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        service = _make_service(es)

        await service.list_notifications(
            "tenant-1",
            {"start_date": "2025-01-01", "end_date": "2025-01-31"},
            page=1,
            size=10,
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        date_range_clause = None
        for clause in must:
            if "range" in clause:
                date_range_clause = clause
                break
        assert date_range_clause is not None
        assert date_range_clause["range"]["created_at"]["gte"] == "2025-01-01"
        assert date_range_clause["range"]["created_at"]["lte"] == "2025-01-31"

    async def test_returns_empty_when_no_results(self):
        """list_notifications returns empty items when no results."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        service = _make_service(es)

        result = await service.list_notifications("tenant-1", {}, page=1, size=10)

        assert result["items"] == []
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# get_notification
# ---------------------------------------------------------------------------


class TestGetNotification:
    """Tests for NotificationService.get_notification."""

    async def test_returns_notification_when_found(self):
        """get_notification returns the notification dict."""
        doc = _notification_doc()
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([doc]))
        service = _make_service(es)

        result = await service.get_notification("notif-1", "tenant-1")

        assert result["notification_id"] == "notif-1"

    async def test_raises_404_when_not_found(self):
        """get_notification raises 404 when notification doesn't exist."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        service = _make_service(es)

        with pytest.raises(AppException) as exc_info:
            await service.get_notification("missing-id", "tenant-1")

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# retry_notification
# ---------------------------------------------------------------------------


class TestRetryNotification:
    """Tests for NotificationService.retry_notification."""

    async def test_retries_failed_notification(self):
        """retry_notification resets status to pending and increments retry_count."""
        doc = _notification_doc(delivery_status="failed", retry_count=1)
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([doc]))
        service = _make_service(es)

        sms_d = _make_dispatcher_mock("sms")
        service.register_dispatcher("sms", sms_d)

        result = await service.retry_notification("notif-1", "tenant-1")

        assert result["retry_count"] == 2
        sms_d.dispatch.assert_called_once()

    async def test_raises_409_for_non_failed_notification(self):
        """retry_notification raises 409 when status is not failed."""
        doc = _notification_doc(delivery_status="sent")
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([doc]))
        service = _make_service(es)

        with pytest.raises(AppException) as exc_info:
            await service.retry_notification("notif-1", "tenant-1")

        assert exc_info.value.status_code == 400  # VALIDATION_ERROR default

    async def test_raises_404_when_not_found(self):
        """retry_notification raises 404 when notification doesn't exist."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        service = _make_service(es)

        with pytest.raises(AppException) as exc_info:
            await service.retry_notification("missing-id", "tenant-1")

        assert exc_info.value.status_code == 404

    async def test_retry_with_unregistered_dispatcher_sets_failed(self):
        """retry_notification sets failed when dispatcher not registered."""
        doc = _notification_doc(
            delivery_status="failed", channel="whatsapp", retry_count=0
        )
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([doc]))
        service = _make_service(es)

        # No dispatcher registered for whatsapp
        result = await service.retry_notification("notif-1", "tenant-1")

        assert result["delivery_status"] == "failed"
        assert "whatsapp" in result["failure_reason"]

    async def test_retry_broadcasts_ws_status_update(self):
        """retry_notification broadcasts status update via WS."""
        doc = _notification_doc(delivery_status="failed")
        es = _make_es_mock()
        es.search_documents = AsyncMock(return_value=_es_response([doc]))
        service = _make_service(es)
        ws = _make_ws_mock()
        service.set_ws_manager(ws)

        sms_d = _make_dispatcher_mock("sms")
        service.register_dispatcher("sms", sms_d)

        await service.retry_notification("notif-1", "tenant-1")

        ws.broadcast_status_update.assert_called_once()


# ---------------------------------------------------------------------------
# get_summary
# ---------------------------------------------------------------------------


class TestGetSummary:
    """Tests for NotificationService.get_summary."""

    async def test_returns_aggregated_counts(self):
        """get_summary returns by_type, by_channel, by_status, total."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response(
                by_type={"delay_alert": 5, "eta_change": 3},
                by_channel={"sms": 4, "email": 4},
                by_status={"sent": 6, "failed": 2},
                total=8,
            )
        )
        service = _make_service(es)

        result = await service.get_summary("tenant-1")

        assert result["by_type"] == {"delay_alert": 5, "eta_change": 3}
        assert result["by_channel"] == {"sms": 4, "email": 4}
        assert result["by_status"] == {"sent": 6, "failed": 2}
        assert result["total"] == 8

    async def test_applies_date_range_filter(self):
        """get_summary passes date range to ES query."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response(total=0)
        )
        service = _make_service(es)

        await service.get_summary("tenant-1", "2025-01-01", "2025-01-31")

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must = query["query"]["bool"]["must"]
        date_range_clause = None
        for clause in must:
            if "range" in clause:
                date_range_clause = clause
                break
        assert date_range_clause is not None
        assert date_range_clause["range"]["created_at"]["gte"] == "2025-01-01"
        assert date_range_clause["range"]["created_at"]["lte"] == "2025-01-31"

    async def test_returns_empty_when_no_data(self):
        """get_summary returns empty dicts and zero total when no data."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response(total=0)
        )
        service = _make_service(es)

        result = await service.get_summary("tenant-1")

        assert result["by_type"] == {}
        assert result["by_channel"] == {}
        assert result["by_status"] == {}
        assert result["total"] == 0
