"""Dunning thresholds + cancellation.

Implements the DunningService per design §8:
1. Scheduled evaluation: query overdue invoices, compute days_overdue,
   enqueue notifications for each crossed threshold that hasn't been
   recorded yet.
2. Cancellation: when an invoice transitions to paid/void, cancel any
   queued-but-unsent dunning notifications.

Duplicate prevention is enforced via the dunning_events ES index — a
notification is only enqueued if no dunning_events record exists for
(invoice_id, threshold_days).

Feature-flag gated: all operations are no-ops when
commerce.dunning_enabled is off for the tenant.

Validates: Requirements 7.3, 7.4, 7.5
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from commerce.services.commerce_es_mappings import (
    DUNNING_EVENTS_INDEX,
    INVOICES_CURRENT_INDEX,
)
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DUNNING_THRESHOLDS_DAYS: List[int] = [7, 14, 30]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DunningService:
    """Dunning threshold evaluation and notification lifecycle.

    Runs as a scheduled job. Per tick it scans overdue invoices, evaluates
    threshold crossings, enqueues notifications, and writes dunning_events
    rows for duplicate prevention.

    When an invoice is paid or voided, ``cancel_for_invoice`` marks all
    pending dunning_events as cancelled so the notification pipeline drops
    any queued-but-unsent emails.

    Every public method takes ``tenant_id`` and every ES query passes
    through ``inject_tenant_filter`` for tenant isolation.
    """

    def __init__(
        self,
        es_service,
        notification_service=None,
        feature_flag_service=None,
    ) -> None:
        """Initialize DunningService.

        Args:
            es_service: ElasticsearchService instance for index operations.
            notification_service: Optional notification service for enqueuing
                dunning emails. If None, notifications are logged but not sent.
            feature_flag_service: Optional feature flag service to check
                commerce.dunning_enabled. If None, dunning is assumed enabled.
        """
        self._es = es_service
        self._notification_service = notification_service
        self._feature_flag_service = feature_flag_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate_and_enqueue(
        self,
        tenant_id: str,
        thresholds: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Scan overdue invoices and enqueue dunning notifications.

        Per design §8:
        1. Query invoices_current where status in (open, partial, overdue)
           and due_date <= utcnow() - min(thresholds).
        2. For each invoice, compute days_overdue. For each threshold where
           days_overdue >= threshold AND no dunning_events record exists for
           (invoice_id, threshold), enqueue a notification and write a
           dunning_events row.

        Args:
            tenant_id: The tenant to evaluate.
            thresholds: Override threshold days list. Defaults to [7, 14, 30].

        Returns:
            Summary dict with counts of invoices scanned, notifications
            enqueued, and duplicates skipped.
        """
        # Feature flag gate
        if not await self._is_dunning_enabled(tenant_id):
            logger.info(
                "Dunning disabled for tenant %s — skipping evaluation",
                tenant_id,
            )
            return {
                "tenant_id": tenant_id,
                "skipped": True,
                "reason": "dunning_disabled",
                "invoices_scanned": 0,
                "notifications_enqueued": 0,
                "duplicates_skipped": 0,
            }

        if thresholds is None:
            thresholds = DEFAULT_DUNNING_THRESHOLDS_DAYS

        # Sort thresholds ascending for processing
        thresholds = sorted(thresholds)
        min_threshold = thresholds[0]

        now = utcnow()
        cutoff_date = (now - timedelta(days=min_threshold)).date()

        # Query overdue invoices
        overdue_invoices = await self._query_overdue_invoices(
            tenant_id, cutoff_date
        )

        invoices_scanned = len(overdue_invoices)
        notifications_enqueued = 0
        duplicates_skipped = 0

        for invoice in overdue_invoices:
            due_date_str = invoice.get("due_date")
            if not due_date_str:
                continue

            # Parse due_date and compute days overdue
            if isinstance(due_date_str, str):
                due_date_val = date.fromisoformat(due_date_str[:10])
            else:
                due_date_val = due_date_str

            days_overdue = (now.date() - due_date_val).days

            invoice_id = invoice.get("invoice_id")
            account_id = invoice.get("account_id", "")

            # Evaluate each threshold
            for threshold in thresholds:
                if days_overdue >= threshold:
                    # Check for existing dunning event (duplicate prevention)
                    has_event = await self._has_dunning_event(
                        tenant_id, invoice_id, threshold
                    )
                    if has_event:
                        duplicates_skipped += 1
                        continue

                    # Enqueue notification and write dunning_events row
                    await self._enqueue_dunning_notification(
                        tenant_id=tenant_id,
                        invoice_id=invoice_id,
                        account_id=account_id,
                        threshold_days=threshold,
                        days_overdue=days_overdue,
                        invoice=invoice,
                    )
                    notifications_enqueued += 1

        result = {
            "tenant_id": tenant_id,
            "skipped": False,
            "invoices_scanned": invoices_scanned,
            "notifications_enqueued": notifications_enqueued,
            "duplicates_skipped": duplicates_skipped,
        }

        logger.info(
            "Dunning evaluation complete for tenant %s: scanned=%d, enqueued=%d, skipped=%d",
            tenant_id,
            invoices_scanned,
            notifications_enqueued,
            duplicates_skipped,
        )
        return result

    async def cancel_for_invoice(
        self,
        tenant_id: str,
        invoice_id: str,
        reason: str = "invoice_paid",
    ) -> Dict[str, Any]:
        """Cancel pending dunning notifications for a paid/voided invoice.

        When InvoiceService.apply_payment transitions an invoice to paid or
        void, this method is called to mark all non-cancelled dunning_events
        for that invoice as cancelled. The notification pipeline consumes
        the cancellation_reason to drop queued-but-unsent emails.

        Args:
            tenant_id: The tenant owning the invoice.
            invoice_id: The invoice that was paid/voided.
            reason: Cancellation reason (e.g. 'invoice_paid', 'invoice_voided').

        Returns:
            Summary dict with count of cancelled events.
        """
        # Feature flag gate
        if not await self._is_dunning_enabled(tenant_id):
            return {
                "tenant_id": tenant_id,
                "invoice_id": invoice_id,
                "cancelled_count": 0,
                "reason": "dunning_disabled",
            }

        # Find all non-cancelled dunning events for this invoice
        pending_events = await self._get_pending_dunning_events(
            tenant_id, invoice_id
        )

        now = utcnow()
        cancelled_count = 0

        for event in pending_events:
            event_id = event.get("event_id")
            if not event_id:
                continue

            # Update the dunning event with cancellation info
            partial = {
                "cancelled_at": now.isoformat(),
                "cancellation_reason": reason,
            }
            await self._es.update_document(
                DUNNING_EVENTS_INDEX, event_id, partial
            )
            # Mirror the cancellation to Postgres.
            from commerce.services.commerce_persistence_bridge import (
                mirror_dunning_event_fields,
            )
            await mirror_dunning_event_fields(tenant_id, event_id, partial)
            cancelled_count += 1

        # Notify the notification service to drop queued messages
        if self._notification_service and cancelled_count > 0:
            try:
                await self._notification_service.cancel_queued(
                    tenant_id=tenant_id,
                    invoice_id=invoice_id,
                    reason=reason,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to cancel queued notifications for invoice %s: %s",
                    invoice_id,
                    exc,
                )

        logger.info(
            "Cancelled %d dunning events for invoice %s tenant %s (reason: %s)",
            cancelled_count,
            invoice_id,
            tenant_id,
            reason,
        )

        return {
            "tenant_id": tenant_id,
            "invoice_id": invoice_id,
            "cancelled_count": cancelled_count,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _is_dunning_enabled(self, tenant_id: str) -> bool:
        """Check if commerce.dunning_enabled is on for the tenant.

        If no feature_flag_service is configured, defaults to enabled
        (development mode).
        """
        if self._feature_flag_service is None:
            return True

        try:
            state = await self._feature_flag_service.get_overlay_state(
                "commerce.dunning_enabled", tenant_id
            )
            return state == "active"
        except Exception as exc:
            logger.warning(
                "Failed to check dunning_enabled flag for tenant %s: %s",
                tenant_id,
                exc,
            )
            # Fail-closed: if we can't check the flag, don't run dunning
            return False

    async def _has_dunning_event(
        self,
        tenant_id: str,
        invoice_id: str,
        threshold_days: int,
    ) -> bool:
        """Check if a dunning event already exists for (invoice_id, threshold).

        This is the duplicate prevention mechanism per Req 7.4. Returns True
        if a non-cancelled dunning_events record exists for the given
        invoice and threshold combination.
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"invoice_id": invoice_id}},
                        {"term": {"threshold_days": threshold_days}},
                    ],
                    "must_not": [
                        {"exists": {"field": "cancelled_at"}},
                    ],
                }
            },
            "size": 0,
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            DUNNING_EVENTS_INDEX, query, size=0
        )

        total = response.get("hits", {}).get("total", {})
        if hasattr(total, "get") or isinstance(total, dict):
            count = total.get("value", 0)
        else:
            count = total

        return count > 0

    async def _query_overdue_invoices(
        self,
        tenant_id: str,
        cutoff_date: date,
    ) -> List[Dict[str, Any]]:
        """Query invoices that are overdue past the minimum threshold.

        Returns invoices where:
        - status in (open, partial, overdue)
        - due_date <= cutoff_date
        """
        _DUNNING_STATUSES = ["open", "partial", "overdue"]

        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_invoices_open_for_aggregation,
        )

        pg = await read_invoices_open_for_aggregation(
            tenant_id, statuses=_DUNNING_STATUSES,
            due_on_or_before=cutoff_date, order_by_due_asc=True,
        )
        if pg is not _NOT_CUT_OVER:
            return pg

        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "terms": {
                                "status": _DUNNING_STATUSES
                            }
                        },
                        {
                            "range": {
                                "due_date": {
                                    "lte": cutoff_date.isoformat()
                                }
                            }
                        },
                    ]
                }
            },
            "size": 1000,
            "sort": [{"due_date": {"order": "asc"}}],
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=1000
        )

        hits = response.get("hits", {}).get("hits", [])
        return [hit["_source"] for hit in hits]

    async def _get_pending_dunning_events(
        self,
        tenant_id: str,
        invoice_id: str,
    ) -> List[Dict[str, Any]]:
        """Get all non-cancelled dunning events for an invoice."""
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"invoice_id": invoice_id}},
                    ],
                    "must_not": [
                        {"exists": {"field": "cancelled_at"}},
                    ],
                }
            },
            "size": 100,
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            DUNNING_EVENTS_INDEX, query, size=100
        )

        hits = response.get("hits", {}).get("hits", [])
        return [hit["_source"] for hit in hits]

    async def _enqueue_dunning_notification(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        account_id: str,
        threshold_days: int,
        days_overdue: int,
        invoice: Dict[str, Any],
    ) -> str:
        """Enqueue a dunning notification and write the dunning_events record.

        The template key follows the pattern: dunning_level_{threshold_days}

        Returns the event_id of the created dunning_events record.
        """
        now = utcnow()
        event_id = f"dun_{uuid4()}"
        template_key = f"dunning_level_{threshold_days}"

        # Write dunning_events record (Req 7.4)
        dunning_doc: Dict[str, Any] = {
            "event_id": event_id,
            "invoice_id": invoice_id,
            "account_id": account_id,
            "tenant_id": tenant_id,
            "threshold_days": threshold_days,
            "template_key": template_key,
            "queued_at": now.isoformat(),
            "cancelled_at": None,
            "cancellation_reason": None,
        }

        await self._es.index_document(
            DUNNING_EVENTS_INDEX, event_id, dunning_doc
        )

        # Dual-write the dunning event to the Postgres source-of-truth.
        from commerce.services.commerce_persistence_bridge import (
            mirror_dunning_event_create,
        )
        await mirror_dunning_event_create(dunning_doc)

        # Enqueue notification via notification service
        if self._notification_service:
            try:
                notification_payload = {
                    "tenant_id": tenant_id,
                    "invoice_id": invoice_id,
                    "account_id": account_id,
                    "template_key": template_key,
                    "threshold_days": threshold_days,
                    "days_overdue": days_overdue,
                    "invoice_number": invoice.get("invoice_number"),
                    "total_cents": invoice.get("total_cents"),
                    "remaining_cents": invoice.get("remaining_cents"),
                    "due_date": invoice.get("due_date"),
                    "customer_id": invoice.get("customer_id"),
                }
                await self._notification_service.enqueue(
                    tenant_id=tenant_id,
                    template_key=template_key,
                    payload=notification_payload,
                )
            except Exception as exc:
                logger.error(
                    "Failed to enqueue dunning notification for invoice %s "
                    "threshold %d: %s",
                    invoice_id,
                    threshold_days,
                    exc,
                )

        logger.info(
            "Enqueued dunning notification: invoice=%s, threshold=%d days, "
            "template=%s, tenant=%s",
            invoice_id,
            threshold_days,
            template_key,
            tenant_id,
        )
        return event_id
