"""
Dispatcher intake adapter — transforms dispatcher keyboard payloads into FuelOrders.

This adapter handles orders created via the ``POST /api/orders`` dispatcher
keyboard endpoint. It stamps ``intake_channel="dispatcher"``,
``intake_channel_id`` from the resolved channel, and populates
``intake_metadata.dispatcher_user_id`` and ``intake_metadata.session_id``
from the request context and payload respectively.

Validates: Requirements 2.4.3, 2.3.4.
"""
from __future__ import annotations

from typing import Any, Dict

from fuel.intake.adapter_base import AdapterError, IntakeContext, IntakeResult


class DispatcherIntakeAdapter:
    """Intake adapter for the dispatcher keyboard channel.

    Accepts the REST body from ``POST /api/orders`` and produces a
    canonical FuelOrder document with dispatcher-specific metadata.

    Attributes:
        channel_type: Always ``"dispatcher"``.
        schema_version: The schema version this adapter handles (``"1.0"``).
    """

    channel_type: str = "dispatcher"
    schema_version: str = "1.0"

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        """Transform a dispatcher keyboard payload into a FuelOrder + events.

        The adapter extracts business fields from the payload and stamps
        the dispatcher-specific intake metadata. It does NOT set
        ``order_id``, ``tenant_id``, or ``status`` — those are
        platform-assigned by the ``OrderIntakePipeline``.

        Args:
            payload: The JSON body from the dispatcher keyboard endpoint.
                     Expected to contain customer info, product details,
                     delivery window, call_type, and optionally a
                     ``session_id``.
            context: Per-request context carrying tenant identity, the
                     resolved intake channel, trace info, and the acting
                     dispatcher's user ID.

        Returns:
            An IntakeResult with the order_doc and a single
            ``order_placed`` event.

        Raises:
            AdapterError: When required fields are missing from the payload.
        """
        # Validate required fields
        required_fields = [
            "customer_id", "customer_name", "ship_to_address",
            "ship_to_lat", "ship_to_lon", "call_type",
        ]
        missing = [f for f in required_fields if not payload.get(f)]
        if missing:
            raise AdapterError(
                error_type="adapter_validation_failed",
                message=f"Missing required fields: {', '.join(missing)}",
            )

        # Build the order document — adapters own business shape
        order_doc: Dict[str, Any] = {
            # Customer reference
            "customer_id": payload["customer_id"],
            "customer_name": payload["customer_name"],
            "customer_phone": payload.get("customer_phone"),
            "customer_email": payload.get("customer_email"),
            "ship_to_address": payload["ship_to_address"],
            "ship_to_lat": payload["ship_to_lat"],
            "ship_to_lon": payload["ship_to_lon"],
            "customer_tank_id": payload.get("customer_tank_id"),
            # Product details
            "product_code": payload.get("product_code"),
            "gallons_requested": payload.get("gallons_requested"),
            "fill_to_full": payload.get("fill_to_full", False),
            "call_type": payload["call_type"],
            "delivery_window_start": payload.get("delivery_window_start"),
            "delivery_window_end": payload.get("delivery_window_end"),
            "po_number": payload.get("po_number"),
            "special_instructions": payload.get("special_instructions"),
            # Intake provenance
            "intake_channel": "dispatcher",
            "intake_channel_id": context.channel.channel_id,
            "intake_metadata": {
                "dispatcher_user_id": context.actor_user_id,
                "session_id": payload.get("session_id"),
            },
            "source_schema_version": self.schema_version,
        }

        # Emit a single order_placed event
        event_docs = [
            {
                "event_type": "order_placed",
                "event_payload": {
                    "intake_channel": "dispatcher",
                    "intake_channel_id": context.channel.channel_id,
                    "dispatcher_user_id": context.actor_user_id,
                },
            }
        ]

        return IntakeResult(order_doc=order_doc, event_docs=event_docs)


__all__ = ["DispatcherIntakeAdapter"]
