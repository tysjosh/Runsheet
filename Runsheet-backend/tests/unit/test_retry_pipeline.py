"""
Unit tests for RetryPipeline — exponential backoff and dead-letter queue.

Tests compute_backoff_delay, schedule_retry, move_to_dlq, poll_and_retry,
and the wiring into NotificationService._update_status for failed dispatches.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from notifications.services.retry_pipeline import RetryPipeline
from notifications.services.notification_service import NotificationService
from notifications.services.notification_es_mappings import (
    DEAD_LETTER_QUEUE_INDEX,
    NOTIFICATIONS_CURRENT_INDEX,
)
from notifications.models import DeliveryStatus


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


def _make_notification_service(es_mock: MagicMock) -> NotificationService:
    """Create a NotificationService with a mocked ES service."""
    return NotificationService(es_service=es_mock)


def _make_pipeline(
    notification_service: NotificationService,
    es_mock: MagicMock,
    max_retries: int = 3,
    base_delay_seconds: int = 60,
) -> RetryPipeline:
    """Create a RetryPipeline with mocked dependencies."""
    return RetryPipeline(
        notification_service=notification_service,
        es_service=es_mock,
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
    )


def _notification_doc(
    notification_id: str = "notif-1",
    channel: str = "sms",
    delivery_status: str = "failed",
    retry_count: int = 0,
    tenant_id: str = "tenant-1",
    failure_reason: str | None = "Provider timeout",
) -> dict:
    """Return a sample notification document."""
    return {
        "notification_id": notification_id,
        "notification_type": "delay_alert",
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
        "failure_reason": failure_reason,
        "retry_count": retry_count,
        "scheduled_retry_at": None,
        "tenant_id": tenant_id,
    }


# ---------------------------------------------------------------------------
# compute_backoff_delay
# ---------------------------------------------------------------------------


class TestComputeBackoffDelay:
    """Tests for RetryPipeline.compute_backoff_delay."""

    def test_first_retry_returns_base_delay(self):
        """retry_count=0 → base_delay * 2^0 = base_delay."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, base_delay_seconds=60)

        assert pipeline.compute_backoff_delay(0) == 60

    def test_second_retry_doubles_delay(self):
        """retry_count=1 → base_delay * 2^1 = 2 * base_delay."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, base_delay_seconds=60)

        assert pipeline.compute_backoff_delay(1) == 120

    def test_third_retry_quadruples_delay(self):
        """retry_count=2 → base_delay * 2^2 = 4 * base_delay."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, base_delay_seconds=60)

        assert pipeline.compute_backoff_delay(2) == 240

    def test_custom_base_delay(self):
        """Custom base_delay_seconds is respected."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, base_delay_seconds=30)

        assert pipeline.compute_backoff_delay(0) == 30
        assert pipeline.compute_backoff_delay(1) == 60
        assert pipeline.compute_backoff_delay(2) == 120

    def test_formula_is_base_times_two_to_the_retry_count(self):
        """Verify the formula: base_delay_seconds * 2^retry_count."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, base_delay_seconds=10)

        for i in range(6):
            assert pipeline.compute_backoff_delay(i) == 10 * (2 ** i)


# ---------------------------------------------------------------------------
# schedule_retry
# ---------------------------------------------------------------------------


class TestScheduleRetry:
    """Tests for RetryPipeline.schedule_retry."""

    async def test_sets_status_to_retry_pending(self):
        """schedule_retry sets delivery_status to retry_pending."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)

        notification = _notification_doc(retry_count=0)
        await pipeline.schedule_retry(notification)

        assert notification["delivery_status"] == DeliveryStatus.RETRY_PENDING.value

    async def test_sets_scheduled_retry_at(self):
        """schedule_retry sets scheduled_retry_at to now + delay."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, base_delay_seconds=60)

        notification = _notification_doc(retry_count=0)
        await pipeline.schedule_retry(notification)

        assert notification["scheduled_retry_at"] is not None
        # Parse the scheduled time and verify it's roughly 60s in the future
        scheduled = datetime.fromisoformat(notification["scheduled_retry_at"])
        now = datetime.now(timezone.utc)
        diff = (scheduled - now).total_seconds()
        # Allow some tolerance for test execution time
        assert 55 <= diff <= 65

    async def test_updates_es_document(self):
        """schedule_retry calls update_document on ES."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)

        notification = _notification_doc(retry_count=0)
        await pipeline.schedule_retry(notification)

        es.update_document.assert_called_once()
        call_args = es.update_document.call_args
        assert call_args[0][0] == NOTIFICATIONS_CURRENT_INDEX
        assert call_args[0][1] == "notif-1"
        partial_doc = call_args[0][2]
        assert partial_doc["delivery_status"] == DeliveryStatus.RETRY_PENDING.value
        assert "scheduled_retry_at" in partial_doc

    async def test_moves_to_dlq_when_max_retries_exceeded(self):
        """schedule_retry delegates to move_to_dlq when retry_count >= max_retries."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, max_retries=3)

        notification = _notification_doc(retry_count=3)
        await pipeline.schedule_retry(notification)

        assert notification["delivery_status"] == DeliveryStatus.DEAD_LETTER.value

    async def test_second_retry_has_longer_delay(self):
        """retry_count=1 produces a longer delay than retry_count=0."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, base_delay_seconds=60)

        notif_0 = _notification_doc(notification_id="n0", retry_count=0)
        notif_1 = _notification_doc(notification_id="n1", retry_count=1)

        await pipeline.schedule_retry(notif_0)
        await pipeline.schedule_retry(notif_1)

        t0 = datetime.fromisoformat(notif_0["scheduled_retry_at"])
        t1 = datetime.fromisoformat(notif_1["scheduled_retry_at"])
        # notif_1 should be scheduled further out
        assert t1 > t0


# ---------------------------------------------------------------------------
# move_to_dlq
# ---------------------------------------------------------------------------


class TestMoveToDlq:
    """Tests for RetryPipeline.move_to_dlq."""

    async def test_sets_status_to_dead_letter(self):
        """move_to_dlq sets delivery_status to dead_letter."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)

        notification = _notification_doc(retry_count=3)
        await pipeline.move_to_dlq(notification)

        assert notification["delivery_status"] == DeliveryStatus.DEAD_LETTER.value

    async def test_indexes_in_dlq_index(self):
        """move_to_dlq indexes the notification in the dead_letter_queue index."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)

        notification = _notification_doc(retry_count=3)
        await pipeline.move_to_dlq(notification)

        # Should have called index_document for DLQ and update_document for original
        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == DEAD_LETTER_QUEUE_INDEX
        dlq_doc = call_args[0][2]
        assert dlq_doc["notification_id"] == "notif-1"
        assert dlq_doc["tenant_id"] == "tenant-1"
        assert "original_notification" in dlq_doc
        assert "moved_at" in dlq_doc

    async def test_updates_original_notification_in_es(self):
        """move_to_dlq updates the original notification status in ES."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)

        notification = _notification_doc(retry_count=3)
        await pipeline.move_to_dlq(notification)

        es.update_document.assert_called_once()
        call_args = es.update_document.call_args
        assert call_args[0][0] == NOTIFICATIONS_CURRENT_INDEX
        assert call_args[0][1] == "notif-1"
        partial_doc = call_args[0][2]
        assert partial_doc["delivery_status"] == DeliveryStatus.DEAD_LETTER.value

    async def test_broadcasts_ws_event(self):
        """move_to_dlq broadcasts a WS event when ws_manager is set."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        ws = _make_ws_mock()
        ns.set_ws_manager(ws)
        pipeline = _make_pipeline(ns, es)

        notification = _notification_doc(retry_count=3)
        await pipeline.move_to_dlq(notification)

        ws.broadcast_status_update.assert_called_once()
        call_args = ws.broadcast_status_update.call_args
        assert call_args[0][0] == "notif-1"
        assert call_args[0][1] == DeliveryStatus.DEAD_LETTER.value
        data = call_args[0][2]
        assert data["moved_to_dlq"] is True

    async def test_no_ws_broadcast_when_no_manager(self):
        """move_to_dlq does not fail when no ws_manager is set."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)

        notification = _notification_doc(retry_count=3)
        # Should not raise
        await pipeline.move_to_dlq(notification)

        assert notification["delivery_status"] == DeliveryStatus.DEAD_LETTER.value

    async def test_dlq_doc_contains_failure_reasons(self):
        """DLQ document includes the failure_reasons from the notification."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)

        notification = _notification_doc(
            retry_count=3, failure_reason="Rate limited: 429"
        )
        await pipeline.move_to_dlq(notification)

        dlq_doc = es.index_document.call_args[0][2]
        assert dlq_doc["failure_reasons"] == "Rate limited: 429"


# ---------------------------------------------------------------------------
# _poll_once (internal, tested via poll behavior)
# ---------------------------------------------------------------------------


class TestPollOnce:
    """Tests for RetryPipeline._poll_once."""

    async def test_no_action_when_no_pending_notifications(self):
        """_poll_once does nothing when no retry_pending notifications exist."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [], "total": {"value": 0}}}
        )
        ns = _make_notification_service(es)
        dispatcher = MagicMock()
        dispatcher.channel_name = "sms"
        dispatcher.dispatch = AsyncMock(return_value="sent")
        ns.register_dispatcher("sms", dispatcher)
        pipeline = _make_pipeline(ns, es)

        await pipeline._poll_once()

        # Only the search call, no dispatch
        dispatcher.dispatch.assert_not_called()

    async def test_retries_due_notifications(self):
        """_poll_once re-dispatches notifications past scheduled_retry_at."""
        es = _make_es_mock()
        notification = _notification_doc(
            delivery_status="retry_pending", retry_count=1
        )
        es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": notification}],
                    "total": {"value": 1},
                }
            }
        )
        ns = _make_notification_service(es)
        dispatcher = MagicMock()
        dispatcher.channel_name = "sms"
        dispatcher.dispatch = AsyncMock(return_value="sent")
        ns.register_dispatcher("sms", dispatcher)
        pipeline = _make_pipeline(ns, es)

        await pipeline._poll_once()

        dispatcher.dispatch.assert_called_once()

    async def test_moves_to_dlq_on_max_retries_during_poll(self):
        """_poll_once moves to DLQ when retry_count reaches max_retries."""
        es = _make_es_mock()
        notification = _notification_doc(
            delivery_status="retry_pending", retry_count=2
        )
        es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": notification}],
                    "total": {"value": 1},
                }
            }
        )
        ns = _make_notification_service(es)
        dispatcher = MagicMock()
        dispatcher.channel_name = "sms"
        dispatcher.dispatch = AsyncMock(return_value="failed")
        ns.register_dispatcher("sms", dispatcher)
        pipeline = _make_pipeline(ns, es, max_retries=3)

        await pipeline._poll_once()

        # retry_count was 2, incremented to 3 which equals max_retries
        # so it should be moved to DLQ
        assert notification["delivery_status"] == DeliveryStatus.DEAD_LETTER.value


# ---------------------------------------------------------------------------
# NotificationService._update_status wiring
# ---------------------------------------------------------------------------


class TestUpdateStatusRetryWiring:
    """Tests for retry pipeline wiring into NotificationService._update_status."""

    async def test_failed_status_triggers_retry_pipeline(self):
        """When _update_status is called with FAILED and retry pipeline is set,
        schedule_retry is invoked."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)
        ns.set_retry_pipeline(pipeline)

        notification = _notification_doc(
            delivery_status="pending", retry_count=0
        )

        await ns._update_status(
            notification,
            DeliveryStatus.FAILED,
            failure_reason="Provider timeout",
        )

        # The notification should now be retry_pending (pipeline scheduled it)
        assert notification["delivery_status"] == DeliveryStatus.RETRY_PENDING.value

    async def test_sent_status_does_not_trigger_retry(self):
        """When _update_status is called with SENT, retry pipeline is not invoked."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)
        pipeline.schedule_retry = AsyncMock()
        ns.set_retry_pipeline(pipeline)

        notification = _notification_doc(
            delivery_status="pending", retry_count=0
        )

        await ns._update_status(notification, DeliveryStatus.SENT)

        pipeline.schedule_retry.assert_not_called()

    async def test_no_retry_when_pipeline_not_set(self):
        """When no retry pipeline is set, _update_status just updates status."""
        es = _make_es_mock()
        ns = _make_notification_service(es)

        notification = _notification_doc(
            delivery_status="pending", retry_count=0
        )

        await ns._update_status(
            notification,
            DeliveryStatus.FAILED,
            failure_reason="Provider timeout",
        )

        # Status should be failed (no retry pipeline to change it)
        assert notification["delivery_status"] == DeliveryStatus.FAILED.value

    async def test_max_retries_exceeded_moves_to_dlq(self):
        """When retry_count >= max_retries, _update_status → schedule_retry → move_to_dlq."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es, max_retries=3)
        ns.set_retry_pipeline(pipeline)

        notification = _notification_doc(
            delivery_status="pending", retry_count=3
        )

        await ns._update_status(
            notification,
            DeliveryStatus.FAILED,
            failure_reason="Provider timeout",
        )

        assert notification["delivery_status"] == DeliveryStatus.DEAD_LETTER.value


# ---------------------------------------------------------------------------
# Configurable defaults
# ---------------------------------------------------------------------------


class TestConfigurableDefaults:
    """Tests for configurable max_retries and base_delay_seconds."""

    def test_default_max_retries_is_3(self):
        """Default max_retries is 3."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = RetryPipeline(ns, es)
        assert pipeline._max_retries == 3

    def test_default_base_delay_is_60(self):
        """Default base_delay_seconds is 60."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = RetryPipeline(ns, es)
        assert pipeline._base_delay == 60

    def test_custom_max_retries(self):
        """Custom max_retries is respected."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = RetryPipeline(ns, es, max_retries=5)
        assert pipeline._max_retries == 5

    def test_custom_base_delay(self):
        """Custom base_delay_seconds is respected."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = RetryPipeline(ns, es, base_delay_seconds=30)
        assert pipeline._base_delay == 30

    def test_default_poll_interval_is_30(self):
        """Default poll_interval_seconds is 30."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = RetryPipeline(ns, es)
        assert pipeline._poll_interval == 30


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Tests for RetryPipeline start/stop lifecycle."""

    def test_stop_sets_running_false(self):
        """stop() sets _running to False."""
        es = _make_es_mock()
        ns = _make_notification_service(es)
        pipeline = _make_pipeline(ns, es)
        pipeline._running = True

        pipeline.stop()

        assert pipeline._running is False
