"""
Retry pipeline with exponential backoff and dead-letter queue.

Provides automatic retry scheduling for failed notifications with
configurable backoff delays, and moves permanently failed notifications
to a dead-letter queue for manual review.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from notifications.models import DeliveryStatus
from notifications.services.notification_es_mappings import (
    DEAD_LETTER_QUEUE_INDEX,
    NOTIFICATIONS_CURRENT_INDEX,
)

if TYPE_CHECKING:
    from notifications.services.notification_service import NotificationService
    from notifications.ws.notification_ws_manager import NotificationWSManager
    from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class RetryPipeline:
    """Background retry processor with exponential backoff and DLQ.

    Uses ES-backed scheduling rather than an external queue. A background
    task polls for ``retry_pending`` notifications past their
    ``scheduled_retry_at`` timestamp and re-dispatches them.

    Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
    """

    def __init__(
        self,
        notification_service: NotificationService,
        es_service: ElasticsearchService,
        max_retries: int = 3,
        base_delay_seconds: int = 60,
        poll_interval_seconds: int = 30,
    ):
        self._notification_service = notification_service
        self._es = es_service
        self._max_retries = max_retries
        self._base_delay = base_delay_seconds
        self._poll_interval = poll_interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_backoff_delay(self, retry_count: int) -> int:
        """Compute delay: ``base_delay_seconds * 2^retry_count``.

        Validates: Requirement 3.1
        """
        return self._base_delay * (2 ** retry_count)

    async def schedule_retry(self, notification: dict) -> None:
        """Set status to ``retry_pending`` and compute ``scheduled_retry_at``.

        If the notification has already exhausted all retries, delegates
        to :meth:`move_to_dlq` instead.

        Validates: Requirements 3.1, 3.5
        """
        retry_count = notification.get("retry_count", 0)

        if retry_count >= self._max_retries:
            await self.move_to_dlq(notification)
            return

        delay = self.compute_backoff_delay(retry_count)
        scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        scheduled_at_iso = scheduled_at.isoformat()
        now = datetime.now(timezone.utc).isoformat()

        notification_id = notification["notification_id"]

        partial_doc = {
            "delivery_status": DeliveryStatus.RETRY_PENDING.value,
            "scheduled_retry_at": scheduled_at_iso,
            "updated_at": now,
        }

        # Update local dict
        notification["delivery_status"] = DeliveryStatus.RETRY_PENDING.value
        notification["scheduled_retry_at"] = scheduled_at_iso
        notification["updated_at"] = now

        try:
            await self._es.update_document(
                NOTIFICATIONS_CURRENT_INDEX, notification_id, partial_doc
            )
            logger.info(
                "Scheduled retry for notification_id=%s retry_count=%d "
                "delay=%ds scheduled_at=%s",
                notification_id,
                retry_count,
                delay,
                scheduled_at_iso,
            )
        except Exception as exc:
            logger.error(
                "Failed to schedule retry for notification_id=%s: %s",
                notification_id,
                exc,
            )

    async def move_to_dlq(self, notification: dict) -> None:
        """Move notification to ``dead_letter_queue`` index.

        Sets status to ``dead_letter`` and broadcasts a WS event.

        Validates: Requirements 3.2, 3.6
        """
        notification_id = notification["notification_id"]
        now = datetime.now(timezone.utc).isoformat()

        # Build DLQ document
        dlq_doc = {
            "notification_id": notification_id,
            "original_notification": notification,
            "failure_reasons": notification.get("failure_reason", "Unknown failure"),
            "moved_at": now,
            "tenant_id": notification.get("tenant_id", ""),
        }

        # Index in DLQ
        try:
            dlq_id = str(uuid.uuid4())
            await self._es.index_document(
                DEAD_LETTER_QUEUE_INDEX, dlq_id, dlq_doc
            )
        except Exception as exc:
            logger.error(
                "Failed to index notification_id=%s in DLQ: %s",
                notification_id,
                exc,
            )

        # Update original notification status to dead_letter
        partial_doc = {
            "delivery_status": DeliveryStatus.DEAD_LETTER.value,
            "updated_at": now,
        }

        notification["delivery_status"] = DeliveryStatus.DEAD_LETTER.value
        notification["updated_at"] = now

        try:
            await self._es.update_document(
                NOTIFICATIONS_CURRENT_INDEX, notification_id, partial_doc
            )
        except Exception as exc:
            logger.error(
                "Failed to update notification_id=%s to dead_letter: %s",
                notification_id,
                exc,
            )

        # Broadcast WS event
        ws_manager: NotificationWSManager | None = (
            self._notification_service._ws_manager
        )
        if ws_manager:
            try:
                await ws_manager.broadcast_status_update(
                    notification_id,
                    DeliveryStatus.DEAD_LETTER.value,
                    {
                        "notification_id": notification_id,
                        "delivery_status": DeliveryStatus.DEAD_LETTER.value,
                        "moved_to_dlq": True,
                        "failure_reasons": dlq_doc["failure_reasons"],
                    },
                )
            except Exception as exc:
                logger.warning(
                    "WS broadcast failed for DLQ notification_id=%s: %s",
                    notification_id,
                    exc,
                )

        logger.info(
            "Moved notification_id=%s to dead-letter queue after %d retries",
            notification_id,
            notification.get("retry_count", 0),
        )

    async def poll_and_retry(self) -> None:
        """Background loop: find ``retry_pending`` notifications past
        ``scheduled_retry_at`` and re-dispatch them.

        Validates: Requirements 3.1, 3.3
        """
        self._running = True
        logger.info(
            "Retry pipeline started (poll_interval=%ds, max_retries=%d, "
            "base_delay=%ds)",
            self._poll_interval,
            self._max_retries,
            self._base_delay,
        )

        while self._running:
            try:
                await self._poll_once()
            except Exception as exc:
                logger.error("Retry pipeline poll error: %s", exc)

            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        """Single poll iteration — find and re-dispatch due notifications."""
        now = datetime.now(timezone.utc).isoformat()

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"delivery_status": DeliveryStatus.RETRY_PENDING.value}},
                        {"range": {"scheduled_retry_at": {"lte": now}}},
                    ]
                }
            },
            "sort": [{"scheduled_retry_at": {"order": "asc"}}],
            "size": 50,
        }

        try:
            response = await self._es.search_documents(
                NOTIFICATIONS_CURRENT_INDEX, query, size=50
            )
        except Exception as exc:
            logger.error("Failed to query retry_pending notifications: %s", exc)
            return

        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return

        logger.info("Found %d notifications due for retry", len(hits))

        for hit in hits:
            notification = hit["_source"]
            await self._retry_single(notification)

    async def _retry_single(self, notification: dict) -> None:
        """Re-dispatch a single notification through its channel dispatcher."""
        notification_id = notification["notification_id"]
        channel = notification.get("channel", "")
        retry_count = notification.get("retry_count", 0)

        # Increment retry count
        new_retry_count = retry_count + 1
        notification["retry_count"] = new_retry_count

        dispatcher = self._notification_service._dispatchers.get(channel)

        if dispatcher is None:
            notification["failure_reason"] = (
                f"No dispatcher registered for channel: {channel}"
            )
            # Check if we've exhausted retries
            if new_retry_count >= self._max_retries:
                await self.move_to_dlq(notification)
            else:
                await self.schedule_retry(notification)
            return

        try:
            delivery_status_str = await dispatcher.dispatch(notification)
            new_status = DeliveryStatus(delivery_status_str)

            if new_status == DeliveryStatus.FAILED:
                # Dispatch returned failed — schedule another retry or DLQ
                if new_retry_count >= self._max_retries:
                    await self.move_to_dlq(notification)
                else:
                    await self.schedule_retry(notification)
            else:
                # Success — update status
                now = datetime.now(timezone.utc).isoformat()
                partial_doc = {
                    "delivery_status": new_status.value,
                    "retry_count": new_retry_count,
                    "updated_at": now,
                    "scheduled_retry_at": None,
                }
                if new_status == DeliveryStatus.SENT:
                    partial_doc["sent_at"] = now
                    notification["sent_at"] = now

                notification["delivery_status"] = new_status.value
                notification["updated_at"] = now

                await self._es.update_document(
                    NOTIFICATIONS_CURRENT_INDEX, notification_id, partial_doc
                )

                logger.info(
                    "Retry succeeded for notification_id=%s status=%s "
                    "retry_count=%d",
                    notification_id,
                    new_status.value,
                    new_retry_count,
                )

        except Exception as exc:
            logger.error(
                "Dispatcher error during retry for notification_id=%s: %s",
                notification_id,
                exc,
            )
            notification["failure_reason"] = str(exc)
            if new_retry_count >= self._max_retries:
                await self.move_to_dlq(notification)
            else:
                await self.schedule_retry(notification)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> asyncio.Task:
        """Start the background poll loop.

        Returns the asyncio Task so the caller can cancel it on shutdown.
        """
        self._task = asyncio.create_task(self.poll_and_retry())
        return self._task

    def stop(self) -> None:
        """Stop the background poll loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Retry pipeline stopped")
