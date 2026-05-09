"""
Legacy Dinee shipment adapter — transforms legacy shipment payloads into FuelOrders.

This adapter accepts the legacy shipment payload shape from the deprecation
window's ``/webhooks/dinee`` route and derives a minimal FuelOrder so existing
integrations do not break during the rename cutover. This adapter is the only
way ``/webhooks/dinee`` reaches the new pipeline.

The adapter produces a minimal order with:
- ``call_type="one_off"``
- ``fill_to_full=false``
- ``product_code=null`` (legacy orders are exempt from the product_code requirement)
- ``gallons_requested=null`` (legacy orders are exempt from the volume requirement)
- ``intake_channel="legacy"``
- ``intake_channel_id="dinee-legacy"``
- ``intake_metadata.legacy_shipment_id=<shipment_id>``

Adapter version pinned to ``"1.0"``.

Validates: Requirements 1.3.2, 2.2.8.
"""
from __future__ import annotations

from typing import Any, Dict

from fuel.intake.adapter_base import AdapterError, IntakeContext, IntakeResult


class LegacyDineeShipmentAdapter:
    """Intake adapter for the legacy Dinee shipment channel.

    Accepts the legacy shipment payload shape and derives a minimal
    FuelOrder document. This adapter exists solely for backward
    compatibility during the deprecation window.

    Attributes:
        channel_type: Always ``"legacy"``.
        schema_version: Pinned to ``"1.0"``.
    """

    channel_type: str = "legacy"
    schema_version: str = "1.0"

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        """Transform a legacy Dinee shipment payload into a FuelOrder + events.

        The adapter maps the legacy shipment fields to the FuelOrder shape.
        It derives a minimal order — ``product_code`` and ``gallons_requested``
        are left null (the forecaster or dispatcher attaches them later).
        It does NOT set ``order_id``, ``tenant_id``, or ``status`` — those
        are platform-assigned by the ``OrderIntakePipeline``.

        Args:
            payload: The raw JSON body from the legacy ``/webhooks/dinee``
                     endpoint. Expected to contain at minimum a
                     ``shipment_id`` and customer/address fields in the
                     legacy shape.
            context: Per-request context carrying tenant identity, the
                     resolved intake channel, and trace info.

        Returns:
            An IntakeResult with the order_doc and a single
            ``order_placed`` event.

        Raises:
            AdapterError: When the legacy payload is missing critical fields.
        """
        # Extract the shipment_id — the key identifier from the legacy shape
        shipment_id = payload.get("shipment_id") or payload.get("id")
        if not shipment_id:
            raise AdapterError(
                error_type="adapter_validation_failed",
                message="Legacy payload missing shipment_id",
            )

        # Map legacy fields to the FuelOrder shape
        # Legacy payloads use various field names; we handle common patterns
        customer_id = (
            payload.get("customer_id")
            or payload.get("sender_id")
            or payload.get("client_id")
            or str(shipment_id)
        )
        customer_name = (
            payload.get("customer_name")
            or payload.get("sender_name")
            or payload.get("client_name")
            or "Legacy Customer"
        )
        ship_to_address = (
            payload.get("ship_to_address")
            or payload.get("destination_address")
            or payload.get("delivery_address")
            or payload.get("address")
            or "Unknown"
        )
        ship_to_lat = payload.get("ship_to_lat") or payload.get("destination_lat") or 0.0
        ship_to_lon = payload.get("ship_to_lon") or payload.get("destination_lon") or 0.0

        # Build the order document — minimal legacy shape
        order_doc: Dict[str, Any] = {
            # Customer reference (best-effort from legacy fields)
            "customer_id": customer_id,
            "customer_name": customer_name,
            "customer_phone": payload.get("customer_phone") or payload.get("sender_phone"),
            "customer_email": payload.get("customer_email") or payload.get("sender_email"),
            "ship_to_address": ship_to_address,
            "ship_to_lat": ship_to_lat,
            "ship_to_lon": ship_to_lon,
            "customer_tank_id": payload.get("customer_tank_id"),
            # Product details — null for legacy (exempt from validation)
            "product_code": None,
            "gallons_requested": None,
            "fill_to_full": False,
            "call_type": "one_off",
            "delivery_window_start": payload.get("delivery_window_start")
            or payload.get("estimated_delivery_start"),
            "delivery_window_end": payload.get("delivery_window_end")
            or payload.get("estimated_delivery_end")
            or payload.get("estimated_delivery"),
            "po_number": payload.get("po_number"),
            "special_instructions": payload.get("special_instructions")
            or payload.get("notes"),
            # Intake provenance
            "intake_channel": "legacy",
            "intake_channel_id": "dinee-legacy",
            "intake_metadata": {
                "legacy_shipment_id": str(shipment_id),
            },
            # Preserve the legacy origin for dual-write rollback
            "legacy_origin_snapshot": payload.get("origin"),
            "source_schema_version": self.schema_version,
        }

        # Emit a single order_placed event
        event_docs = [
            {
                "event_type": "order_placed",
                "event_payload": {
                    "intake_channel": "legacy",
                    "intake_channel_id": "dinee-legacy",
                    "legacy_shipment_id": str(shipment_id),
                },
            }
        ]

        return IntakeResult(order_doc=order_doc, event_docs=event_docs)


__all__ = ["LegacyDineeShipmentAdapter"]
