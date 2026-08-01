"""
CSV intake adapter — transforms bulk-upload rows into FuelOrders.

This adapter handles orders created via the ``POST /api/orders/bulk``
CSV upload endpoint. It reads a single row from the bulk upload and
stamps ``intake_channel="csv"``, ``intake_metadata.import_batch_id``,
and ``intake_metadata.csv_row_number``.

Validates: Requirement 2.4.4.
"""
from __future__ import annotations

from typing import Any, Dict

from fuel.intake.adapter_base import AdapterError, IntakeContext, IntakeResult
from services.money import (
    legacy_unit_price_cents,
    line_subtotal_cents,
    unit_price_micros_from_record,
)


class CsvIntakeAdapter:
    """Intake adapter for the CSV bulk-upload channel.

    Accepts a single row from a bulk upload (already parsed into a dict)
    and produces a canonical FuelOrder document with CSV-specific metadata.

    Attributes:
        channel_type: Always ``"csv"``.
        schema_version: The schema version this adapter handles (``"1.0"``).
    """

    channel_type: str = "csv"
    schema_version: str = "1.0"

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        """Transform a CSV row payload into a FuelOrder + events.

        The adapter extracts business fields from the row and stamps
        the CSV-specific intake metadata (batch ID and row number).
        It does NOT set ``order_id``, ``tenant_id``, or ``status`` —
        those are platform-assigned by the ``OrderIntakePipeline``.

        Args:
            payload: A dict representing a single CSV row. Expected to
                     contain customer info, product details, delivery
                     window, call_type, and the CSV metadata fields
                     ``import_batch_id`` and ``csv_row_number``.
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
        missing = [
            f
            for f in required_fields
            if payload.get(f) is None
            or (isinstance(payload.get(f), str) and not payload.get(f).strip())
        ]
        if missing:
            raise AdapterError(
                error_type="adapter_validation_failed",
                message=f"Missing required fields: {', '.join(missing)}",
            )

        # CSV metadata is required
        import_batch_id = payload.get("import_batch_id")
        csv_row_number = payload.get("csv_row_number")
        if not import_batch_id:
            raise AdapterError(
                error_type="adapter_validation_failed",
                message="Missing required field: import_batch_id",
            )
        if csv_row_number is None:
            raise AdapterError(
                error_type="adapter_validation_failed",
                message="Missing required field: csv_row_number",
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
            "intake_channel": "csv",
            "intake_channel_id": context.channel.channel_id,
            "intake_metadata": {
                "import_batch_id": import_batch_id,
                "csv_row_number": csv_row_number,
                "source_system": payload.get("source_system"),
                "source_record_id": payload.get("source_order_id"),
                "source_updated_at": payload.get("source_updated_at"),
            },
            "source_schema_version": self.schema_version,
        }

        unit_price_micros = unit_price_micros_from_record(payload)
        if unit_price_micros is not None:
            order_doc["unit_price_micros"] = unit_price_micros
            order_doc["unit_price_cents"] = legacy_unit_price_cents(
                unit_price_micros
            )
            requested = payload.get("gallons_requested")
            if requested:
                subtotal_cents = line_subtotal_cents(
                    requested,
                    unit_price_micros,
                )
                tax_cents = int(payload.get("tax_cents") or 0)
                order_doc["subtotal_cents"] = subtotal_cents
                order_doc["tax_cents"] = tax_cents
                order_doc["total_cents"] = subtotal_cents + tax_cents

        # Emit a single order_placed event
        event_docs = [
            {
                "event_type": "order_placed",
                "event_payload": {
                    "intake_channel": "csv",
                    "intake_channel_id": context.channel.channel_id,
                    "import_batch_id": import_batch_id,
                    "csv_row_number": csv_row_number,
                    "source_system": payload.get("source_system"),
                    "source_record_id": payload.get("source_order_id"),
                },
            }
        ]

        return IntakeResult(order_doc=order_doc, event_docs=event_docs)


__all__ = ["CsvIntakeAdapter"]
