"""Subscriber for BOL generation — fires e_bol_delivery notification.

Called by the PODBOLFinalizer after a signed BOL PDF is successfully
generated. Sends the ``e_bol_delivery`` notification to the customer's
designated BOL recipient email with the PDF attachment reference.

The handler is fault-tolerant: notification failures are logged but NEVER
block the BOL generation / delivery pipeline. The PODBOLFinalizer catches
exceptions from subscribers so even if this handler raises, the BOL
generation completes.

Deduplication: Uses a per-instance set of bol_ids to ensure the
notification fires at most once per BOL within the same process lifecycle.
The NotificationService's rule engine provides additional deduplication at
the persistence layer.

Validates: Requirement 12.8
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


class BOLSignedSubscriber:
    """Handles BOL generation events to send e_bol_delivery notifications.

    This subscriber is called by the PODBOLFinalizer after a signed BOL
    PDF is successfully generated. On each event it:

    1. Checks deduplication (skip if already notified for this bol_id).
    2. Extracts delivery summary from the BOL document.
    3. Calls ``NotificationService.notify_event()`` with event_type
       ``e_bol_delivery`` and the template placeholders including the
       PDF attachment reference.

    Failures are logged but MUST NOT block the delivery pipeline.

    Args:
        notification_service: The NotificationService instance for
            sending notifications.

    Validates: Requirement 12.8
    """

    def __init__(self, notification_service: Any) -> None:
        """Initialize the subscriber.

        Args:
            notification_service: A NotificationService instance for
                dispatching e_bol_delivery notifications.
        """
        self._notification_service = notification_service
        # Per-instance deduplication set — ensures the notification fires
        # at most once per BOL within the same process lifecycle.
        self._notified_bols: Set[str] = set()

    async def __call__(self, bol_document: Dict[str, Any]) -> None:
        """Handle a BOL generated event.

        Called by the PODBOLFinalizer after a signed BOL PDF is
        successfully generated and persisted.

        Args:
            bol_document: The BOL document dict containing bol_id,
                tenant_id, file_ref, fields (with delivery summary),
                and other metadata.
        """
        bol_id = bol_document.get("bol_id", "")
        tenant_id = bol_document.get("tenant_id", "")

        # Deduplication: skip if already notified for this BOL
        if bol_id in self._notified_bols:
            logger.debug(
                "BOLSignedSubscriber: already notified for "
                "bol=%s tenant=%s — skipping duplicate",
                bol_id,
                tenant_id,
            )
            return

        # Must have notification service
        if self._notification_service is None:
            logger.debug(
                "BOLSignedSubscriber: no notification_service "
                "available — skipping notification for bol=%s",
                bol_id,
            )
            return

        # Build event_data with template placeholders for e_bol_delivery
        event_data = self._build_event_data(bol_document)

        # Must have customer_id to send notification
        customer_id = event_data.get("customer_id")
        if not customer_id:
            logger.debug(
                "BOLSignedSubscriber: no customer_id on bol=%s "
                "tenant=%s — skipping notification",
                bol_id,
                tenant_id,
            )
            return

        # Fire the notification — non-blocking (errors logged, not raised)
        try:
            await self._notification_service.notify_event(
                event_type="e_bol_delivery",
                event_data=event_data,
                tenant_id=tenant_id,
            )
            # Mark as notified (deduplication)
            self._notified_bols.add(bol_id)
            logger.info(
                "BOLSignedSubscriber: fired e_bol_delivery "
                "notification for bol=%s customer=%s tenant=%s",
                bol_id,
                customer_id,
                tenant_id,
            )
        except Exception as exc:
            # Fault-tolerant: log the error but do not re-raise.
            # The delivery pipeline must not be blocked by notification
            # failures.
            logger.error(
                "BOLSignedSubscriber: failed to send "
                "e_bol_delivery notification for bol=%s tenant=%s: %s",
                bol_id,
                tenant_id,
                exc,
            )

    @staticmethod
    def _build_event_data(bol_document: Dict[str, Any]) -> Dict[str, Any]:
        """Extract template placeholders from the BOL document.

        Maps BOL fields to the e_bol_delivery template placeholders:
        - customer_name, load_number, product, gross_gallons,
          net_gallons, terminal, driver
        - attachment_ref (file_ref for the signed BOL PDF)

        Args:
            bol_document: The BOL document dict.

        Returns:
            Dict of event_data suitable for NotificationService.notify_event().
        """
        # The BOL document has a nested 'fields' dict with the delivery
        # details (BOLFields from bol_service.py)
        fields = bol_document.get("fields", {})
        if isinstance(fields, dict):
            bol_fields = fields
        else:
            # If fields is a Pydantic model, convert to dict
            try:
                bol_fields = fields.dict() if hasattr(fields, "dict") else {}
            except Exception:
                bol_fields = {}

        # Map product_code to a human-readable product name
        product_code = bol_fields.get("product_code", "")
        product_name = bol_fields.get("product_name") or _product_code_to_name(
            product_code
        )

        return {
            "customer_id": bol_fields.get("customer_id", bol_document.get("customer_id", "")),
            "customer_name": bol_fields.get(
                "customer_name",
                bol_fields.get("destination_name", "Valued Customer"),
            ),
            "load_number": bol_fields.get(
                "bol_number", bol_document.get("bol_id", "N/A")
            ),
            "product": product_name,
            "gross_gallons": str(bol_fields.get("gross_gallons", "N/A")),
            "net_gallons": str(bol_fields.get("net_gallons", bol_fields.get("gross_gallons", "N/A"))),
            "terminal": bol_fields.get(
                "terminal_name",
                bol_fields.get("origin_name", "N/A"),
            ),
            "driver": bol_fields.get(
                "driver_name", bol_fields.get("driver_id", "N/A")
            ),
            # Attachment metadata — the file_ref (S3 path) for the signed
            # BOL PDF. The notification dispatcher uses this to attach the
            # PDF to the outgoing email.
            "attachment_ref": bol_document.get("file_ref", ""),
            "attachment_type": "signed_bol_pdf",
            "bol_id": bol_document.get("bol_id", ""),
        }


def _product_code_to_name(product_code: str) -> str:
    """Map common fuel product codes to human-readable names.

    Falls back to the raw product_code if no mapping exists.
    """
    _PRODUCT_NAMES = {
        "DIESEL_2": "Diesel #2",
        "DIESEL_1": "Diesel #1",
        "OFF_ROAD_DIESEL": "Off-Road Diesel (Dyed)",
        "HEATING_OIL": "Heating Oil",
        "PROPANE": "Propane",
        "GASOLINE_87": "Gasoline 87",
        "GASOLINE_89": "Gasoline 89",
        "GASOLINE_93": "Gasoline 93",
        "KEROSENE": "Kerosene",
        "DEF": "Diesel Exhaust Fluid (DEF)",
    }
    return _PRODUCT_NAMES.get(product_code, product_code or "Fuel")
