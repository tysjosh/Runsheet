"""Unit tests for MeterAuditService wiring into InvoiceService.generate_from_order().

Covers task 10.6 of the fuel-compliance-backbone spec: MeterAuditService's
``check_meter_calibration_for_delivery()`` and ``link_ticket_to_invoice()``
were fully implemented and unit-tested (Req 8.2, 8.5) but had zero callers
in production code — this closes that gap by wiring them into
InvoiceService.generate_from_order() as a post-generation, non-blocking
check, mirroring the existing DyedDieselEnforcer wiring (task 9.9).

Test matrix:

* With a wired MeterAuditService and a delivery_result carrying a
  meter_number, get_meter_by_number() is called and, when the meter's
  calibration has expired, the invoice is flagged with warning code
  meter.calibration_expired and link_ticket_to_invoice() is still called.
* When the meter's calibration is valid, no warning is added but
  link_ticket_to_invoice() is still called.
* When no meter_number is present on the delivery snapshot (manual
  gallon entry, no meter ticket), the MeterAuditService is never called.
* When no MeterAuditService is wired (legacy), no lookup occurs.
* When get_meter_by_number() returns None (unregistered meter), the
  check is skipped gracefully — no flag, no link call.
* When the service raises an exception, the invoice is still returned
  (graceful degradation, non-blocking).

Validates: Requirements 8.2, 8.5
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.invoice_service import InvoiceService

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_meter"
_CUSTOMER_ID = "cust_meter"
_ACCOUNT_ID = "acct_meter"
_ORDER_ID = "order_meter_001"
_FIXED_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

_LINE_ITEM: Dict[str, Any] = {
    "line_id": "line_1",
    "product_code": "DIESEL_2",
    "quantity_gallons": 500.0,
    "quantity": 500.0,
    "unit_price_cents": 300,
    "subtotal_cents": 150_000,
}


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService for InvoiceService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(
        return_value={
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"max_seq": {"value": None}},
        }
    )
    return es


def _make_idempotency_service() -> AsyncMock:
    idemp = AsyncMock()
    idemp.is_duplicate = AsyncMock(return_value=False)
    idemp.mark_processed = AsyncMock(return_value=None)
    return idemp


def _make_meter_audit_service(
    *, meter_doc: Dict[str, Any] | None, flagged: bool = False
) -> AsyncMock:
    """Create a mocked MeterAuditService."""
    svc = AsyncMock()
    svc.get_meter_by_number = AsyncMock(return_value=meter_doc)
    svc.check_meter_calibration_for_delivery = AsyncMock(
        return_value={
            "flagged": flagged,
            "warning_code": "meter.calibration_expired" if flagged else None,
            "message": "Meter calibration expired" if flagged else None,
        }
    )
    svc.link_ticket_to_invoice = AsyncMock(return_value={})
    return svc


def _delivery_result(**overrides) -> Dict[str, Any]:
    base = {
        "pod_id": "pod_1",
        "actual_gallons": 500.0,
        "actual_gallons_source": "ocr",
        "delivered_at": _FIXED_NOW.isoformat(),
        "recipient_name": "Acme Co",
        "geotag": {"lat": 40.0, "lon": -74.0},
        "meter_number": "MTR-001",
        "ticket_number": "TCK-001",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_expired_calibration_flags_invoice_and_links_ticket(mock_now):
    """Expired meter calibration flags the invoice and still links the ticket.

    Validates: Requirements 8.2, 8.5
    """
    es = _make_es_service()
    idemp = _make_idempotency_service()
    meter_doc = {"meter_id": "meter_001", "meter_number": "MTR-001"}
    meter_svc = _make_meter_audit_service(meter_doc=meter_doc, flagged=True)

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_meter_audit_service(meter_svc)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[dict(_LINE_ITEM)],
        delivery_result=_delivery_result(),
    )

    meter_svc.get_meter_by_number.assert_awaited_once_with(
        _TENANT_ID, "MTR-001"
    )
    meter_svc.check_meter_calibration_for_delivery.assert_awaited_once()
    call_kwargs = meter_svc.check_meter_calibration_for_delivery.call_args.kwargs
    assert call_kwargs["meter_id"] == "meter_001"
    assert call_kwargs["invoice_id"] == doc["invoice_id"]
    assert call_kwargs["delivery_id"] == "pod_1"

    meter_svc.link_ticket_to_invoice.assert_awaited_once()
    link_kwargs = meter_svc.link_ticket_to_invoice.call_args.kwargs
    assert link_kwargs["meter_id"] == "meter_001"
    assert link_kwargs["meter_ticket_id"] == "TCK-001"
    assert link_kwargs["invoice_id"] == doc["invoice_id"]
    assert link_kwargs["gross_gallons"] == 500.0

    # The warning was persisted onto the invoice via update_document.
    es.update_document.assert_any_call(
        "invoices_current",
        doc["invoice_id"],
        {
            "doc": {
                "warnings": [
                    {
                        "code": "meter.calibration_expired",
                        "message": "Meter calibration expired",
                    }
                ]
            }
        },
    )


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_valid_calibration_links_ticket_without_flag(mock_now):
    """A meter with valid calibration links the ticket but adds no warning."""
    es = _make_es_service()
    idemp = _make_idempotency_service()
    meter_doc = {"meter_id": "meter_002", "meter_number": "MTR-001"}
    meter_svc = _make_meter_audit_service(meter_doc=meter_doc, flagged=False)

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_meter_audit_service(meter_svc)

    await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[dict(_LINE_ITEM)],
        delivery_result=_delivery_result(),
    )

    meter_svc.check_meter_calibration_for_delivery.assert_awaited_once()
    meter_svc.link_ticket_to_invoice.assert_awaited_once()

    # No warnings.doc update should have been issued.
    for call in es.update_document.call_args_list:
        args, kwargs = call
        if len(args) >= 3:
            assert "warnings" not in args[2].get("doc", {})


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_no_meter_number_skips_meter_audit_entirely(mock_now):
    """A delivery snapshot with no meter_number (manual entry) never
    triggers a MeterAuditService lookup — nothing to key off."""
    es = _make_es_service()
    idemp = _make_idempotency_service()
    meter_svc = _make_meter_audit_service(meter_doc=None)

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_meter_audit_service(meter_svc)

    await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[dict(_LINE_ITEM)],
        delivery_result=_delivery_result(meter_number=None, ticket_number=None),
    )

    meter_svc.get_meter_by_number.assert_not_awaited()
    meter_svc.check_meter_calibration_for_delivery.assert_not_awaited()
    meter_svc.link_ticket_to_invoice.assert_not_awaited()


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_no_meter_audit_service_wired_skips_check(mock_now):
    """Legacy behaviour: no MeterAuditService wired means no lookup at all."""
    es = _make_es_service()
    idemp = _make_idempotency_service()

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    # set_meter_audit_service() never called.

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[dict(_LINE_ITEM)],
        delivery_result=_delivery_result(),
    )

    assert doc["invoice_id"]


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_unregistered_meter_skips_gracefully(mock_now):
    """meter_number present but not found in the registry — no flag, no link."""
    es = _make_es_service()
    idemp = _make_idempotency_service()
    meter_svc = _make_meter_audit_service(meter_doc=None)

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_meter_audit_service(meter_svc)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[dict(_LINE_ITEM)],
        delivery_result=_delivery_result(),
    )

    meter_svc.get_meter_by_number.assert_awaited_once_with(
        _TENANT_ID, "MTR-001"
    )
    meter_svc.check_meter_calibration_for_delivery.assert_not_awaited()
    meter_svc.link_ticket_to_invoice.assert_not_awaited()
    assert doc["invoice_id"]


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_meter_audit_exception_does_not_block_invoice(mock_now):
    """A MeterAuditService failure is logged but never blocks the invoice."""
    es = _make_es_service()
    idemp = _make_idempotency_service()
    meter_svc = _make_meter_audit_service(meter_doc={"meter_id": "m1"})
    meter_svc.get_meter_by_number = AsyncMock(
        side_effect=RuntimeError("es unavailable")
    )

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_meter_audit_service(meter_svc)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[dict(_LINE_ITEM)],
        delivery_result=_delivery_result(),
    )

    assert doc["invoice_id"]
