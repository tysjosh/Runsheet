"""Unit tests for DyedDieselEnforcer wiring into InvoiceService.generate_from_order().

Covers task 9.9 of the fuel-compliance-backbone spec:

    Wire ``validate_invoice`` into ``InvoiceService.generate_from_order()``
    to confirm dyed-diesel invoices exclude road-use excise tax (Req 6.5)
    and log the sale for IRS audit readiness (Req 6.7).

Test matrix:

* With a wired DyedDieselEnforcer and dyed-diesel line items, the
  enforcer's validate_invoice() is called after invoice generation.
* When validate_invoice() passes and dyed diesel is present,
  log_dyed_sale() is called with the correct parameters.
* When validate_invoice() fails, a warning is logged but the invoice
  is still returned (non-blocking).
* When no DyedDieselEnforcer is wired (legacy), no validation occurs.
* When the enforcer raises an exception, the invoice is still returned
  (graceful degradation).

Validates: Requirements 6.5, 6.7
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from commerce.services.invoice_service import InvoiceService
from compliance.services.dyed_diesel_enforcer import ValidationResult


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_dyed"
_CUSTOMER_ID = "cust_dyed"
_ACCOUNT_ID = "acct_dyed"
_ORDER_ID = "order_dyed_001"
_FIXED_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

_DYED_DIESEL_LINE: Dict[str, Any] = {
    "line_id": "line_dyed",
    "product_code": "OFF_ROAD_DIESEL",
    "quantity_gallons": 500.0,
    "quantity": 500.0,
    "unit_price_cents": 300,
    "subtotal_cents": 150_000,
}

_CLEAR_DIESEL_LINE: Dict[str, Any] = {
    "line_id": "line_clear",
    "product_code": "DIESEL_2",
    "quantity_gallons": 1000.0,
    "quantity": 1000.0,
    "unit_price_cents": 350,
    "subtotal_cents": 350_000,
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
    """Create a mocked IdempotencyService."""
    idemp = AsyncMock()
    idemp.is_duplicate = AsyncMock(return_value=False)
    idemp.mark_processed = AsyncMock(return_value=None)
    return idemp


def _make_enforcer(valid: bool = True) -> AsyncMock:
    """Create a mocked DyedDieselEnforcer."""
    enforcer = AsyncMock()
    enforcer.validate_invoice = AsyncMock(
        return_value=ValidationResult(valid=valid)
    )
    enforcer.log_dyed_sale = AsyncMock(return_value=None)
    # Expose the static method for is_dyed_diesel checks
    from compliance.services.dyed_diesel_enforcer import DyedDieselEnforcer
    enforcer.is_dyed_diesel = DyedDieselEnforcer.is_dyed_diesel
    return enforcer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_dyed_diesel_validate_invoice_called_on_generation(mock_now):
    """When DyedDieselEnforcer is wired and invoice has dyed diesel,
    validate_invoice() is called after generation.

    Validates: Requirement 6.5
    """
    es = _make_es_service()
    idemp = _make_idempotency_service()
    enforcer = _make_enforcer(valid=True)

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_dyed_diesel_enforcer(enforcer)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[_DYED_DIESEL_LINE.copy()],
        tax_cents=0,
    )

    # validate_invoice should have been called
    enforcer.validate_invoice.assert_called_once_with(
        tenant_id=_TENANT_ID,
        invoice_id=doc["invoice_id"],
    )


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_dyed_diesel_log_sale_called_on_valid_dyed_invoice(mock_now):
    """When validate_invoice passes and invoice has dyed diesel,
    log_dyed_sale() is called for IRS audit.

    Validates: Requirement 6.7
    """
    es = _make_es_service()
    idemp = _make_idempotency_service()
    enforcer = _make_enforcer(valid=True)

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_dyed_diesel_enforcer(enforcer)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[_DYED_DIESEL_LINE.copy()],
        tax_cents=0,
    )

    # log_dyed_sale should have been called with correct params
    enforcer.log_dyed_sale.assert_called_once()
    call_kwargs = enforcer.log_dyed_sale.call_args[1]
    assert call_kwargs["tenant_id"] == _TENANT_ID
    assert call_kwargs["customer_id"] == _CUSTOMER_ID
    assert call_kwargs["invoice_id"] == doc["invoice_id"]
    assert call_kwargs["gallons"] == 500.0
    assert call_kwargs["product_code"] == "OFF_ROAD_DIESEL"


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_dyed_diesel_log_sale_not_called_on_validation_failure(mock_now):
    """When validate_invoice fails, log_dyed_sale() is NOT called.
    The invoice is still returned (non-blocking).

    Validates: Requirement 6.5
    """
    es = _make_es_service()
    idemp = _make_idempotency_service()
    enforcer = _make_enforcer(valid=False)
    enforcer.validate_invoice = AsyncMock(
        return_value=ValidationResult(
            valid=False,
            error_code="dyed.tax_exemption_not_applied",
            message="Road-use excise tax found on dyed diesel invoice",
        )
    )

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_dyed_diesel_enforcer(enforcer)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id=_ORDER_ID,
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[_DYED_DIESEL_LINE.copy()],
        tax_cents=0,
    )

    # Invoice should still be returned
    assert doc["invoice_id"] is not None
    assert doc["status"] == "draft"

    # validate_invoice was called
    enforcer.validate_invoice.assert_called_once()

    # log_dyed_sale should NOT have been called
    enforcer.log_dyed_sale.assert_not_called()


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_no_enforcer_wired_skips_dyed_check(mock_now):
    """When no DyedDieselEnforcer is wired, no validation occurs.
    Legacy behaviour is preserved.
    """
    es = _make_es_service()
    idemp = _make_idempotency_service()

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    # Do NOT set enforcer

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id="order_no_enforcer",
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[_DYED_DIESEL_LINE.copy()],
        tax_cents=0,
    )

    # Invoice should still be generated successfully
    assert doc["invoice_id"] is not None
    assert doc["status"] == "draft"


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_enforcer_exception_does_not_block_invoice(mock_now):
    """When the enforcer raises an exception, the invoice is still returned.
    Graceful degradation — errors are logged but never propagate.

    Validates: Requirements 6.5, 6.7
    """
    es = _make_es_service()
    idemp = _make_idempotency_service()
    enforcer = _make_enforcer(valid=True)
    enforcer.validate_invoice = AsyncMock(
        side_effect=RuntimeError("ES connection timeout")
    )

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_dyed_diesel_enforcer(enforcer)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id="order_exc",
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[_DYED_DIESEL_LINE.copy()],
        tax_cents=0,
    )

    # Invoice should still be returned despite the exception
    assert doc["invoice_id"] is not None
    assert doc["status"] == "draft"


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_clear_diesel_invoice_no_log_dyed_sale(mock_now):
    """When the invoice has only clear diesel (no dyed), log_dyed_sale()
    is not called even if the enforcer is wired.
    """
    es = _make_es_service()
    idemp = _make_idempotency_service()
    enforcer = _make_enforcer(valid=True)

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_dyed_diesel_enforcer(enforcer)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id="order_clear",
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[_CLEAR_DIESEL_LINE.copy()],
        tax_cents=5000,
    )

    # validate_invoice is still called (it checks internally)
    enforcer.validate_invoice.assert_called_once()

    # log_dyed_sale should NOT be called for clear diesel
    enforcer.log_dyed_sale.assert_not_called()


@pytest.mark.asyncio
@patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
async def test_mixed_invoice_logs_only_dyed_gallons(mock_now):
    """When the invoice has both dyed and clear diesel, log_dyed_sale()
    is called with only the dyed diesel gallons.

    Validates: Requirement 6.7
    """
    es = _make_es_service()
    idemp = _make_idempotency_service()
    enforcer = _make_enforcer(valid=True)

    svc = InvoiceService(es_service=es, idempotency_service=idemp)
    svc.set_dyed_diesel_enforcer(enforcer)

    doc = await svc.generate_from_order(
        tenant_id=_TENANT_ID,
        order_id="order_mixed",
        customer_id=_CUSTOMER_ID,
        account_id=_ACCOUNT_ID,
        line_items=[_DYED_DIESEL_LINE.copy(), _CLEAR_DIESEL_LINE.copy()],
        tax_cents=5000,
    )

    # log_dyed_sale should be called with only the dyed gallons
    enforcer.log_dyed_sale.assert_called_once()
    call_kwargs = enforcer.log_dyed_sale.call_args[1]
    assert call_kwargs["gallons"] == 500.0  # Only dyed diesel gallons
    assert call_kwargs["product_code"] == "OFF_ROAD_DIESEL"
