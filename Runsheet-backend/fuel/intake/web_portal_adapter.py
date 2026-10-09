"""
Web-portal intake adapter — customer-portal delivery requests into FuelOrders.

Handles requests submitted through ``POST /api/portal/orders`` (customer
portal design §4.2). ``PortalOrderService`` builds the payload from the
customer's own tank (product, coordinates) and the verified portal scope, so
the adapter only shapes it. It stamps ``intake_channel="web_portal"``,
``intake_channel_id`` from the ephemeral portal channel and
``intake_metadata.portal_session_id`` (unset), and carries no customer phone
or email.

The pipeline, not this adapter, puts the order on the portal review hold
(step i3), because the hold depends on what the ``before_accept`` hooks did.
"""
from __future__ import annotations

from typing import Any, Dict

from fuel.intake.adapter_base import AdapterError, IntakeContext, IntakeResult

_REQUIRED_FIELDS = (
    "customer_id",
    "customer_name",
    "ship_to_address",
    "ship_to_lat",
    "ship_to_lon",
    "customer_tank_id",
    "product_code",
    "call_type",
)


class WebPortalIntakeAdapter:
    """Intake adapter for the ``web_portal`` channel (schema ``1.0``)."""

    channel_type: str = "web_portal"
    schema_version: str = "1.0"

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        """Shape a portal request into an order document and one event.

        Raises:
            AdapterError: When a required field is missing.
        """
        missing = [f for f in _REQUIRED_FIELDS if payload.get(f) in (None, "")]
        if missing:
            raise AdapterError(
                error_type="adapter_validation_failed",
                message=f"Missing required fields: {', '.join(missing)}",
            )

        order_doc: Dict[str, Any] = {
            "customer_id": payload["customer_id"],
            "customer_name": payload["customer_name"],
            "customer_phone": None,
            "customer_email": None,
            "ship_to_address": payload["ship_to_address"],
            "ship_to_lat": payload["ship_to_lat"],
            "ship_to_lon": payload["ship_to_lon"],
            "customer_tank_id": payload["customer_tank_id"],
            "product_code": payload["product_code"],
            "gallons_requested": payload.get("gallons_requested"),
            "fill_to_full": bool(payload.get("fill_to_full", False)),
            "call_type": payload["call_type"],
            "delivery_window_start": payload.get("delivery_window_start"),
            "delivery_window_end": payload.get("delivery_window_end"),
            "po_number": payload.get("po_number"),
            "special_instructions": payload.get("special_instructions"),
            "intake_channel": "web_portal",
            "intake_channel_id": context.channel.channel_id,
            "intake_metadata": {"portal_session_id": None},
            "source_schema_version": self.schema_version,
        }

        event_docs = [
            {
                "event_type": "order_placed",
                "event_payload": {
                    "intake_channel": "web_portal",
                    "intake_channel_id": context.channel.channel_id,
                    "actor_user_id": context.actor_user_id,
                },
            }
        ]
        return IntakeResult(order_doc=order_doc, event_docs=event_docs)


__all__ = ["WebPortalIntakeAdapter"]
