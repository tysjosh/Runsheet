"""
Core notification service — the main orchestrator for the Customer Notification Pipeline.

Consumes operational events, evaluates notification rules, resolves customer
preferences, renders templates, dispatches notifications through channel
dispatchers, stores results in Elasticsearch, and broadcasts updates via
WebSocket.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.2, 2.3, 3.2, 3.3, 6.1, 6.2,
              6.3, 6.4, 6.5, 10.1
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from errors.exceptions import resource_not_found, validation_error
from notifications.models import DeliveryStatus, NotificationType
from notifications.services.channel_dispatchers import ChannelDispatcher
from notifications.services.notification_es_mappings import NOTIFICATIONS_CURRENT_INDEX
from notifications.services.preference_resolver import PreferenceResolver
from notifications.services.rule_engine import RuleEngine
from notifications.services.template_renderer import TemplateRenderer
from services.elasticsearch_service import ElasticsearchService

if TYPE_CHECKING:
    from notifications.services.retry_pipeline import RetryPipeline
    from notifications.ws.notification_ws_manager import NotificationWSManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event-type → NotificationType mapping helpers
# ---------------------------------------------------------------------------

# Required fields that must be present in event_data for notification generation
_REQUIRED_EVENT_FIELDS = {"customer_id"}


def _map_event_to_notification_type(
    event_type: str, event_data: dict
) -> NotificationType:
    """Map an operational event to the corresponding NotificationType.

    Mapping rules (evaluated in order):
    1. ``status_changed`` with status ``completed`` → ``delivery_confirmation``
    2. ``delay_alert`` → ``delay_alert``
    3. ``status_changed`` with updated ``estimated_arrival`` → ``eta_change``
    4. Any other ``status_changed`` → ``order_status_update``
    5. Anything else → ``order_status_update``

    Validates: Requirements 1.1, 1.2, 1.3, 1.4
    """
    if event_type == "status_changed":
        status = event_data.get("status", "")
        if status == "completed":
            return NotificationType.DELIVERY_CONFIRMATION
        if event_data.get("estimated_arrival") is not None:
            return NotificationType.ETA_CHANGE
        return NotificationType.ORDER_STATUS_UPDATE

    if event_type == "delay_alert":
        return NotificationType.DELAY_ALERT

    # Fallback for any other event type
    return NotificationType.ORDER_STATUS_UPDATE


class NotificationService:
    """Core orchestrator for the Customer Notification Pipeline.

    Follows the same pattern as ``JobService`` — constructor takes
    ``ElasticsearchService``, exposes async methods, uses ``_es`` for all
    storage operations.

    Validates: Requirements 1.1–1.6, 2.2, 2.3, 3.2, 3.3, 6.1–6.5, 10.1
    """

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service
        self._rule_engine = RuleEngine(es_service)
        self._preference_resolver = PreferenceResolver(es_service)
        self._template_renderer = TemplateRenderer(es_service)
        self._dispatchers: dict[str, ChannelDispatcher] = {}
        self._ws_manager: NotificationWSManager | None = None
        self._retry_pipeline: RetryPipeline | None = None

    # ------------------------------------------------------------------
    # WS manager wiring (called by bootstrap after construction)
    # ------------------------------------------------------------------

    def set_ws_manager(self, ws_manager: NotificationWSManager) -> None:
        """Wire the WebSocket manager after construction.

        Called by the bootstrap module so that the service can broadcast
        real-time notification events to connected clients.
        """
        self._ws_manager = ws_manager

    def set_retry_pipeline(self, retry_pipeline: RetryPipeline) -> None:
        """Wire the retry pipeline after construction.

        Called by the bootstrap module so that failed dispatches are
        automatically scheduled for retry with exponential backoff.

        Validates: Requirements 3.1, 3.3
        """
        self._retry_pipeline = retry_pipeline

    # ------------------------------------------------------------------
    # Dispatcher registration
    # ------------------------------------------------------------------

    def register_dispatcher(
        self, channel: str, dispatcher: ChannelDispatcher
    ) -> None:
        """Register a channel dispatcher for pluggable channel delivery.

        Validates: Requirement 2.1
        """
        self._dispatchers[channel] = dispatcher
        logger.info("Registered dispatcher for channel: %s", channel)

    # ------------------------------------------------------------------
    # notify_event — main orchestrator
    # ------------------------------------------------------------------

    async def notify_event(
        self, event_type: str, event_data: dict, tenant_id: str
    ) -> list[dict]:
        """Main entry point — called by JobService after broadcasting.

        Orchestration flow:
        1. Validate event data (reject malformed events gracefully)
        2. Map event_type → NotificationType
        3. Evaluate rule via RuleEngine — skip if disabled/not found
        4. Resolve customer preferences — fall back to rule defaults
        5. For each channel: render template → create notification →
           index in ES → dispatch → update status → broadcast via WS

        Validates: Requirements 1.1–1.6, 2.2, 2.3, 3.2, 3.3, 10.1

        Args:
            event_type: The operational event type (e.g. ``status_changed``).
            event_data: Dict of event payload fields.
            tenant_id: Tenant scope.

        Returns:
            List of notification dicts that were created.
        """
        # --- 1. Validate required fields ---
        if not tenant_id:
            logger.error(
                "Malformed event: missing tenant_id. event_type=%s",
                event_type,
            )
            return []

        customer_id = event_data.get("customer_id")
        if not customer_id:
            logger.error(
                "Malformed event: missing customer_id in event_data. "
                "event_type=%s tenant_id=%s",
                event_type,
                tenant_id,
            )
            return []

        # --- 2. Map event → notification type ---
        try:
            notification_type = _map_event_to_notification_type(
                event_type, event_data
            )
        except Exception as exc:
            logger.error(
                "Failed to map event_type=%s to NotificationType: %s",
                event_type,
                exc,
            )
            return []

        # --- 3. Evaluate rule ---
        rule = await self._rule_engine.evaluate_rule(
            notification_type.value, tenant_id
        )
        if rule is None:
            logger.debug(
                "No enabled rule for notification_type=%s tenant_id=%s — skipping",
                notification_type.value,
                tenant_id,
            )
            return []

        # --- 4. Resolve customer preferences ---
        channel_details = await self._preference_resolver.resolve_channels(
            customer_id, notification_type.value, tenant_id
        )

        # Fall back to rule's default_channels when no preference exists
        if not channel_details:
            default_channels = rule.get("default_channels", [])
            # Build channel_details from defaults — use customer_id as
            # the recipient_reference placeholder since we have no stored
            # contact details.
            channel_details = [
                {"channel": ch, "contact_detail": customer_id}
                for ch in default_channels
            ]
            logger.debug(
                "No preference for customer_id=%s — falling back to "
                "default_channels=%s",
                customer_id,
                default_channels,
            )

        if not channel_details:
            logger.debug(
                "No channels resolved for customer_id=%s notification_type=%s — skipping",
                customer_id,
                notification_type.value,
            )
            return []

        # --- 5. Per-channel: render → create → index → dispatch → update → broadcast ---
        notifications: list[dict] = []

        for ch_info in channel_details:
            channel = ch_info["channel"]
            contact_detail = ch_info["contact_detail"]

            notification = await self._process_channel(
                notification_type=notification_type,
                channel=channel,
                contact_detail=contact_detail,
                event_data=event_data,
                rule=rule,
                tenant_id=tenant_id,
            )
            notifications.append(notification)

        return notifications

    # ------------------------------------------------------------------
    # list_notifications
    # ------------------------------------------------------------------

    async def list_notifications(
        self,
        tenant_id: str,
        filters: dict,
        page: int,
        size: int,
    ) -> dict:
        """Paginated notification query with filters.

        Validates: Requirement 6.1

        Args:
            tenant_id: Tenant scope.
            filters: Optional filter dict with keys: notification_type,
                channel, delivery_status, related_entity_id,
                recipient_reference, start_date, end_date.
            page: 1-based page number.
            size: Number of results per page.

        Returns:
            Dict with ``items``, ``total``, ``page``, ``size`` keys.
        """
        must_clauses: list[dict] = [
            {"term": {"tenant_id": tenant_id}},
        ]

        # Apply optional filters
        for field in (
            "notification_type",
            "channel",
            "delivery_status",
            "related_entity_id",
            "recipient_reference",
            "proposal_id",
        ):
            value = filters.get(field)
            if value:
                must_clauses.append({"term": {field: value}})

        # Date range filter
        start_date = filters.get("start_date")
        end_date = filters.get("end_date")
        if start_date or end_date:
            date_range: dict = {}
            if start_date:
                date_range["gte"] = start_date
            if end_date:
                date_range["lte"] = end_date
            must_clauses.append({"range": {"created_at": date_range}})

        from_offset = (page - 1) * size

        query = {
            "query": {"bool": {"must": must_clauses}},
            "sort": [{"created_at": {"order": "desc"}}],
            "from": from_offset,
            "size": size,
        }

        response = await self._es.search_documents(
            NOTIFICATIONS_CURRENT_INDEX, query, size=size
        )

        hits = response["hits"]["hits"]
        total = response["hits"]["total"]
        total_count = total["value"] if isinstance(total, dict) else total

        return {
            "items": [hit["_source"] for hit in hits],
            "total": total_count,
            "page": page,
            "size": size,
        }

    # ------------------------------------------------------------------
    # get_notification
    # ------------------------------------------------------------------

    async def get_notification(
        self, notification_id: str, tenant_id: str
    ) -> dict:
        """Single notification with full audit trail.

        Validates: Requirement 6.2

        Args:
            notification_id: The notification identifier.
            tenant_id: Tenant scope.

        Returns:
            The notification document dict.

        Raises:
            AppException: 404 if not found.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"notification_id": notification_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }

        response = await self._es.search_documents(
            NOTIFICATIONS_CURRENT_INDEX, query, size=1
        )
        hits = response["hits"]["hits"]

        if not hits:
            raise resource_not_found(
                f"Notification '{notification_id}' not found",
                details={"notification_id": notification_id},
            )

        return hits[0]["_source"]

    # ------------------------------------------------------------------
    # retry_notification
    # ------------------------------------------------------------------

    async def retry_notification(
        self, notification_id: str, tenant_id: str
    ) -> dict:
        """Re-dispatch a failed notification.

        Validates: Requirements 6.3, 6.4

        Args:
            notification_id: The notification identifier.
            tenant_id: Tenant scope.

        Returns:
            The updated notification dict.

        Raises:
            AppException: 404 if not found.
            AppException: 409 if delivery_status is not ``failed``.
        """
        notification = await self.get_notification(notification_id, tenant_id)

        if notification["delivery_status"] != DeliveryStatus.FAILED.value:
            raise validation_error(
                f"Notification '{notification_id}' is not in a retryable state",
                details={
                    "notification_id": notification_id,
                    "current_status": notification["delivery_status"],
                },
            )

        # Reset to pending and increment retry_count
        now = datetime.now(timezone.utc).isoformat()
        notification["delivery_status"] = DeliveryStatus.PENDING.value
        notification["retry_count"] = notification.get("retry_count", 0) + 1
        notification["updated_at"] = now

        await self._es.update_document(
            NOTIFICATIONS_CURRENT_INDEX,
            notification_id,
            {
                "delivery_status": notification["delivery_status"],
                "retry_count": notification["retry_count"],
                "updated_at": now,
            },
        )

        # Re-dispatch through the channel dispatcher
        channel = notification["channel"]
        dispatcher = self._dispatchers.get(channel)

        if dispatcher is None:
            await self._update_status(
                notification,
                DeliveryStatus.FAILED,
                failure_reason=f"No dispatcher registered for channel: {channel}",
            )
        else:
            try:
                delivery_status_str = await dispatcher.dispatch(notification)
                new_status = DeliveryStatus(delivery_status_str)
                await self._update_status(notification, new_status)
            except Exception as exc:
                logger.error(
                    "Dispatcher error during retry for notification_id=%s channel=%s: %s",
                    notification_id,
                    channel,
                    exc,
                )
                await self._update_status(
                    notification,
                    DeliveryStatus.FAILED,
                    failure_reason=str(exc),
                )

        # Broadcast status update via WS
        if self._ws_manager:
            try:
                await self._ws_manager.broadcast_status_update(
                    notification_id,
                    notification["delivery_status"],
                    notification,
                )
            except Exception as exc:
                logger.warning(
                    "WS broadcast failed for retry notification_id=%s: %s",
                    notification_id,
                    exc,
                )

        return notification

    # ------------------------------------------------------------------
    # get_summary
    # ------------------------------------------------------------------

    async def get_summary(
        self,
        tenant_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """Aggregate counts by type, channel, status.

        Validates: Requirement 6.5

        Args:
            tenant_id: Tenant scope.
            start_date: Optional ISO date string for range start.
            end_date: Optional ISO date string for range end.

        Returns:
            Dict with ``by_type``, ``by_channel``, ``by_status``, ``total``.
        """
        must_clauses: list[dict] = [
            {"term": {"tenant_id": tenant_id}},
        ]

        if start_date or end_date:
            date_range: dict = {}
            if start_date:
                date_range["gte"] = start_date
            if end_date:
                date_range["lte"] = end_date
            must_clauses.append({"range": {"created_at": date_range}})

        query = {
            "query": {"bool": {"must": must_clauses}},
            "size": 0,
            "aggs": {
                "by_type": {
                    "terms": {"field": "notification_type", "size": 50}
                },
                "by_channel": {
                    "terms": {"field": "channel", "size": 50}
                },
                "by_status": {
                    "terms": {"field": "delivery_status", "size": 50}
                },
            },
        }

        response = await self._es.search_documents(
            NOTIFICATIONS_CURRENT_INDEX, query, size=0
        )

        total_hits = response["hits"]["total"]
        total = total_hits["value"] if isinstance(total_hits, dict) else total_hits

        def _buckets_to_dict(agg_key: str) -> dict[str, int]:
            buckets = response.get("aggregations", {}).get(agg_key, {}).get("buckets", [])
            return {b["key"]: b["doc_count"] for b in buckets}

        return {
            "by_type": _buckets_to_dict("by_type"),
            "by_channel": _buckets_to_dict("by_channel"),
            "by_status": _buckets_to_dict("by_status"),
            "total": total,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _process_channel(
        self,
        *,
        notification_type: NotificationType,
        channel: str,
        contact_detail: str,
        event_data: dict,
        rule: dict,
        tenant_id: str,
    ) -> dict:
        """Process a single channel for a notification event.

        Renders the template, creates the notification document, indexes it
        in ES, dispatches via the channel dispatcher, updates the status,
        and broadcasts via WS.

        Returns the notification dict.
        """
        now = datetime.now(timezone.utc).isoformat()
        notification_id = str(uuid.uuid4())

        # --- Render template ---
        subject = ""
        body = ""
        template_id = rule.get("template_id")

        try:
            if template_id:
                rendered = await self._template_renderer.render(
                    template_id, event_data, tenant_id
                )
                subject = rendered.get("subject", "")
                body = rendered.get("body", "")
            else:
                # Look up template by event_type + channel
                templates = await self._template_renderer.list_templates(
                    tenant_id,
                    event_type=notification_type.value,
                    channel=channel,
                )
                if templates:
                    tmpl = templates[0]
                    tmpl_id = tmpl.get("template_id")
                    if tmpl_id:
                        rendered = await self._template_renderer.render(
                            tmpl_id, event_data, tenant_id
                        )
                        subject = rendered.get("subject", "")
                        body = rendered.get("body", "")
                    else:
                        body = f"Notification: {notification_type.value}"
                else:
                    body = f"Notification: {notification_type.value}"
        except Exception as exc:
            logger.warning(
                "Template rendering failed for notification_type=%s channel=%s: %s",
                notification_type.value,
                channel,
                exc,
            )
            body = f"Notification: {notification_type.value}"

        # --- Build notification document ---
        notification = {
            "notification_id": notification_id,
            "notification_type": notification_type.value,
            "channel": channel,
            "recipient_reference": contact_detail,
            "recipient_name": event_data.get("customer_name"),
            "subject": subject or None,
            "message_body": body,
            "related_entity_type": "job",
            "related_entity_id": event_data.get("job_id"),
            "delivery_status": DeliveryStatus.PENDING.value,
            "created_at": now,
            "updated_at": now,
            "sent_at": None,
            "delivered_at": None,
            "failed_at": None,
            "failure_reason": None,
            "retry_count": 0,
            "tenant_id": tenant_id,
        }

        # Include proposal_id if present in event_data (Req 4.1)
        proposal_id = event_data.get("proposal_id")
        if proposal_id:
            notification["proposal_id"] = proposal_id

        # --- Index in ES (status=pending) ---
        await self._es.index_document(
            NOTIFICATIONS_CURRENT_INDEX, notification_id, notification
        )

        # --- Dispatch via channel dispatcher ---
        dispatcher = self._dispatchers.get(channel)

        if dispatcher is None:
            await self._update_status(
                notification,
                DeliveryStatus.FAILED,
                failure_reason=f"No dispatcher registered for channel: {channel}",
            )
        else:
            try:
                delivery_status_str = await dispatcher.dispatch(notification)
                new_status = DeliveryStatus(delivery_status_str)
                await self._update_status(notification, new_status)
            except Exception as exc:
                logger.error(
                    "Dispatcher error for notification_id=%s channel=%s: %s",
                    notification_id,
                    channel,
                    exc,
                )
                await self._update_status(
                    notification,
                    DeliveryStatus.FAILED,
                    failure_reason=str(exc),
                )

        # --- Broadcast via WS ---
        if self._ws_manager:
            try:
                await self._ws_manager.broadcast_notification(notification)
            except Exception as exc:
                logger.warning(
                    "WS broadcast failed for notification_id=%s: %s",
                    notification_id,
                    exc,
                )

        return notification

    async def _update_status(
        self,
        notification: dict,
        new_status: DeliveryStatus,
        *,
        failure_reason: str | None = None,
    ) -> None:
        """Update a notification's delivery status in ES and in the local dict.

        Sets the appropriate timestamp field based on the new status:
        - ``sent`` → ``sent_at``
        - ``delivered`` → ``delivered_at``
        - ``failed`` → ``failed_at``

        When the new status is ``failed`` and a retry pipeline is wired,
        the pipeline is invoked to schedule a retry or move to DLQ.

        Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.6
        """
        now = datetime.now(timezone.utc).isoformat()

        partial_doc: dict = {
            "delivery_status": new_status.value,
            "updated_at": now,
        }

        # Set the corresponding timestamp field
        if new_status == DeliveryStatus.SENT:
            partial_doc["sent_at"] = now
            notification["sent_at"] = now
        elif new_status == DeliveryStatus.DELIVERED:
            partial_doc["delivered_at"] = now
            notification["delivered_at"] = now
        elif new_status == DeliveryStatus.FAILED:
            partial_doc["failed_at"] = now
            notification["failed_at"] = now
            if failure_reason:
                partial_doc["failure_reason"] = failure_reason
                notification["failure_reason"] = failure_reason

        notification["delivery_status"] = new_status.value
        notification["updated_at"] = now

        notification_id = notification["notification_id"]

        try:
            await self._es.update_document(
                NOTIFICATIONS_CURRENT_INDEX, notification_id, partial_doc
            )
        except Exception as exc:
            logger.error(
                "Failed to update notification status in ES: "
                "notification_id=%s new_status=%s error=%s",
                notification_id,
                new_status.value,
                exc,
            )

        # Trigger retry pipeline for failed dispatches
        if new_status == DeliveryStatus.FAILED and self._retry_pipeline is not None:
            try:
                await self._retry_pipeline.schedule_retry(notification)
            except Exception as exc:
                logger.error(
                    "Retry pipeline error for notification_id=%s: %s",
                    notification_id,
                    exc,
                )
