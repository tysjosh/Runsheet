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
from notifications.services.storm_mode_notifications import (
    StormModeNotificationResolver,
    StormNotificationDecision,
)
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
        # Storm_Mode-aware template selector (Task 10.9, Req 9.2.6). When
        # no ``StormModeNotificationResolver`` is wired, ``notify_event``
        # falls back to the default event_type / template flow, so
        # tenants without Phase 10 enabled see no behavior change.
        self._storm_notification_resolver: (
            StormModeNotificationResolver | None
        ) = None

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

    def set_storm_notification_resolver(
        self, resolver: StormModeNotificationResolver | None
    ) -> None:
        """Wire the Storm_Mode notification resolver.

        When wired, :meth:`notify_event` consults the resolver for each
        event and — when Storm_Mode is active for the recipient's
        tenant *and* the recipient is a keep-full or generator
        customer — swaps to the severe-weather template variant and
        attaches the triggering Weather_Alert reference to the
        persisted notification.

        Passing ``None`` (or never calling this method) preserves the
        pre-Storm_Mode behavior so non-fuel tenants see no change.

        Validates: Requirement 9.2.6 / Task 10.9
        """
        self._storm_notification_resolver = resolver

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

        # --- 4b. Storm_Mode template swap (Task 10.9, Req 9.2.6) ---
        # When Storm_Mode is active for the tenant and the recipient is a
        # keep-full or generator customer, the resolver returns a
        # decision that swaps the default template to the severe-weather
        # variant and attaches a ``weather_alert_ref`` to the persisted
        # notification. The resolver short-circuits to an "inactive"
        # decision when no resolver is wired, no StormModeEvaluator is
        # available, Storm_Mode is inactive for the tenant, or the
        # recipient is not eligible — so the non-storm code path is a
        # true no-op for every other caller.
        storm_decision = await self._resolve_storm_decision(
            tenant_id=tenant_id,
            customer_id=customer_id,
            event_type=notification_type.value,
        )
        if storm_decision.storm_mode_active and storm_decision.placeholder_data:
            # Merge placeholder values without overwriting caller-
            # supplied event_data. Callers always win because they own
            # the semantic meaning of each field.
            merged_event_data = dict(storm_decision.placeholder_data)
            merged_event_data.update(event_data)
            event_data = merged_event_data

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
                storm_decision=storm_decision,
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
        must_clauses: list[dict] = []

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

        bool_query: dict = {
            "must": must_clauses,
            "filter": [{"term": {"tenant_id": tenant_id}}],
        }

        # Free-text "contains" search matching the UI's promise of searching by
        # recipient, entity id, or message. Case-insensitive wildcard over the
        # recipient (reference + name), the related entity id, and the message
        # body / subject, requiring at least one match.
        search = filters.get("search")
        if search and str(search).strip():
            needle = str(search).strip()
            escaped = needle.replace("\\", "\\\\").replace("*", "\\*").replace(
                "?", "\\?"
            )
            pattern = f"*{escaped}*"
            bool_query["should"] = [
                {"wildcard": {field: {"value": pattern, "case_insensitive": True}}}
                for field in (
                    "recipient_reference",
                    "recipient_name.keyword",
                    "related_entity_id",
                    "subject.keyword",
                    "message_body",
                )
            ]
            bool_query["minimum_should_match"] = 1

        query = {
            "query": {"bool": bool_query},
            "sort": [{"created_at": {"order": "desc"}}],
            "from": from_offset,
            "size": size,
        }

        response = await self._es.search_documents(
            NOTIFICATIONS_CURRENT_INDEX, query, size=size
        )

        hits = response["hits"]["hits"]
        total = response["hits"]["total"]
        total_count = total["value"] if hasattr(total, "get") or isinstance(total, dict) else total

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
                    ],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
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
        must_clauses: list[dict] = []

        if start_date or end_date:
            date_range: dict = {}
            if start_date:
                date_range["gte"] = start_date
            if end_date:
                date_range["lte"] = end_date
            must_clauses.append({"range": {"created_at": date_range}})

        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
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
        # Handle both dict and int (ObjectApiResponse can return either)
        total = total_hits["value"] if hasattr(total_hits, 'get') else total_hits

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

    async def _resolve_storm_decision(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        event_type: str,
    ) -> StormNotificationDecision:
        """Return the Storm_Mode decision for an incoming event.

        Delegates to the injected :class:`StormModeNotificationResolver`
        when wired. Any upstream error — missing wiring, broken state
        provider, malformed profile — collapses into
        :meth:`StormNotificationDecision.inactive` so a bad Storm_Mode
        signal never blocks a customer notification (Task 10.9,
        Req 9.2.6).
        """
        resolver = self._storm_notification_resolver
        if resolver is None:
            return StormNotificationDecision.inactive()
        try:
            return await resolver.resolve(
                tenant_id=tenant_id,
                customer_id=customer_id,
                event_type=event_type,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "NotificationService: Storm_Mode resolver raised for "
                "tenant=%s customer=%s event=%s: %s — falling back to "
                "default templates",
                tenant_id,
                customer_id,
                event_type,
                exc,
            )
            return StormNotificationDecision.inactive()

    async def _render_by_event_and_channel(
        self,
        *,
        tenant_id: str,
        channel: str,
        event_data: dict,
        primary_event_type: str | None,
        fallback_event_type: str,
    ) -> dict | None:
        """Resolve the template for ``event_type`` + ``channel`` and render it.

        When ``primary_event_type`` is provided (Storm_Mode variant
        selection), the method attempts the primary event_type first
        and falls back to ``fallback_event_type`` when no template is
        configured for the tenant / channel combination. Returns
        ``None`` when neither lookup finds a usable template so the
        caller can emit the generic "Notification: {event_type}"
        message body.

        Validates: Requirement 9.2.6 / Task 10.9 (graceful fallback).
        """
        attempt_order: list[str] = []
        if primary_event_type and primary_event_type != fallback_event_type:
            attempt_order.append(primary_event_type)
        attempt_order.append(fallback_event_type)

        for event_type in attempt_order:
            templates = await self._template_renderer.list_templates(
                tenant_id,
                event_type=event_type,
                channel=channel,
            )
            if not templates:
                continue
            tmpl = templates[0]
            tmpl_id = tmpl.get("template_id")
            if not tmpl_id:
                continue
            return await self._template_renderer.render(
                tmpl_id, event_data, tenant_id
            )
        return None

    async def _process_channel(
        self,
        *,
        notification_type: NotificationType,
        channel: str,
        contact_detail: str,
        event_data: dict,
        rule: dict,
        tenant_id: str,
        storm_decision: StormNotificationDecision | None = None,
    ) -> dict:
        """Process a single channel for a notification event.

        Renders the template, creates the notification document, indexes it
        in ES, dispatches via the channel dispatcher, updates the status,
        and broadcasts via WS.

        When ``storm_decision.storm_mode_active`` is ``True`` the method
        attempts to render the severe-weather variant template first and
        stamps the triggering Weather_Alert reference plus a
        ``storm_variant_reason`` onto the persisted notification
        document (Task 10.9, Req 9.2.6). If the storm-variant template
        is missing for the tenant/channel, rendering transparently falls
        back to the default event_type so the notification still ships.

        Returns the notification dict.
        """
        now = datetime.now(timezone.utc).isoformat()
        notification_id = str(uuid.uuid4())

        # --- Render template ---
        subject = ""
        body = ""
        template_id = rule.get("template_id")

        storm_active = bool(
            storm_decision and storm_decision.storm_mode_active
        )
        storm_event_type = (
            storm_decision.storm_event_type if storm_active else None
        )

        try:
            if template_id:
                rendered = await self._template_renderer.render(
                    template_id, event_data, tenant_id
                )
                subject = rendered.get("subject", "")
                body = rendered.get("body", "")
            else:
                rendered = await self._render_by_event_and_channel(
                    tenant_id=tenant_id,
                    channel=channel,
                    event_data=event_data,
                    primary_event_type=storm_event_type,
                    fallback_event_type=notification_type.value,
                )
                if rendered is not None:
                    subject = rendered.get("subject", "")
                    body = rendered.get("body", "")
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

        # Storm_Mode metadata (Task 10.9, Req 9.2.6) — only attached when
        # the severe-weather variant was actually selected so non-storm
        # notifications stay visually identical to the pre-Phase 10
        # output.
        if storm_active:
            notification["storm_mode_active"] = True
            if storm_decision.weather_alert_ref is not None:
                notification["weather_alert_ref"] = (
                    storm_decision.weather_alert_ref
                )
            if storm_decision.storm_variant_reason is not None:
                notification["storm_variant_reason"] = (
                    storm_decision.storm_variant_reason
                )

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
