"""
API partner generic adapter — the reference implementation for partner integrations.

This adapter accepts a JSON body shaped like the FuelOrder plus an ``event_id``
and stamps ``intake_channel="api_partner"``,
``intake_channel_id=context.channel.channel_id``, and
``intake_metadata.partner_ref`` from the payload.

This is the reference implementation that future voice-AI or EDI adapters
can model after. It is also wired to the ``curl-friendly`` channel every
tenant gets by default.

Validates: Requirement 2.3.
"""
from __future__ import annotations

from typing import Any, Dict

from fuel.intake.adapter_base import AdapterError, IntakeContext, IntakeResult


class ApiPartnerGenericAdapter:
    """Reference intake adapter for API partner channels.

    Accepts a JSON body that closely mirrors the FuelOrder shape plus
    an ``event_id`` for idempotency. Stamps the API-partner-specific
    intake metadata.

    Attributes:
        channel_type: Always ``"api_partner"``.
        schema_version: The schema version this adapter handles (``"1.0"``).
    """

    channel_type: str = "api_partner"
    schema_version: str = "1.0"

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        """Transform an API partner payload into a FuelOrder + events.

        The payload is expected to be shaped like a FuelOrder with an
        additional ``event_id`` field (used for idempotency by the
        pipeline) and an optional ``partner_ref`` for the partner's
        own reference tracking.

        The adapter does NOT set ``order_id``, ``tenant_id``, or
        ``status`` — those are platform-assigned by the
        ``OrderIntakePipeline``.

        Args:
            payload: The JSON body from the API partner. Expected to
                     contain all FuelOrder business fields plus
                     ``event_id`` and optionally ``partner_ref``.
            context: Per-request context carrying tenant identity, the
                     resolved intake channel, and trace info.

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

        # Extract partner_ref for intake metadata
        partner_ref = payload.get("partner_ref")

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
            "intake_channel": "api_partner",
            "intake_channel_id": context.channel.channel_id,
            "intake_metadata": {
                "partner_ref": partner_ref,
            },
            "source_schema_version": self.schema_version,
        }

        # Emit a single order_placed event
        event_docs = [
            {
                "event_type": "order_placed",
                "event_payload": {
                    "intake_channel": "api_partner",
                    "intake_channel_id": context.channel.channel_id,
                    "partner_ref": partner_ref,
                },
            }
        ]

        return IntakeResult(order_doc=order_doc, event_docs=event_docs)


__all__ = ["ApiPartnerGenericAdapter"]
