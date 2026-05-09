"""Integration tests for CommerceExternalSync adapter.

Tests the adapter that bridges canonical commerce entities to the existing
QBO + Stripe connectors (Design §7).

Scenarios covered:
1. QBO push flow — on_invoice_finalized builds correct payload and calls
   qbo_connector.sync_push.
2. QBO payment ingestion — on_qbo_payment_observed extracts payment details
   and calls PaymentService.ingest with source="qbo".
3. Stripe charge ingestion — on_stripe_charge_observed extracts payment
   details and calls PaymentService.ingest with source="stripe".
4. Dead-letter path — After 3 failed QBO pushes, invoice transitions to
   qbo_push_state=dead_letter.
5. Error isolation — Failures in external sync don't propagate to callers.
6. Idempotency — Duplicate events are handled gracefully.
7. Skips non-succeeded Stripe events.
8. Skips events with missing tenant_id or invoice_id.

Validates: Design §7, Requirements 5.6, 6.1, 6.2
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commerce.services.commerce_external_sync import CommerceExternalSync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_ext_sync_test"
_INVOICE_ID = "inv_ext_001"
_ACCOUNT_ID = "acct_ext_001"
_CUSTOMER_ID = "cust_ext_001"


def _make_invoice(
    *,
    tenant_id: str = _TENANT_ID,
    invoice_id: str = _INVOICE_ID,
    account_id: str = _ACCOUNT_ID,
    customer_id: str = _CUSTOMER_ID,
    total_cents: int = 1750000,
    subtotal_cents: int = 1650000,
    tax_cents: int = 100000,
    qbo_push_attempts: int = 0,
) -> Dict[str, Any]:
    """Build a minimal finalized invoice document for testing."""
    return {
        "invoice_id": invoice_id,
        "tenant_id": tenant_id,
        "account_id": account_id,
        "customer_id": customer_id,
        "customer_name": "Test Customer Inc.",
        "invoice_number": "INV-2025-0042",
        "order_id": "ord_ext_001",
        "issued_at": "2025-01-15T10:00:00Z",
        "total_cents": total_cents,
        "subtotal_cents": subtotal_cents,
        "tax_cents": tax_cents,
        "line_items": [
            {
                "product_code": "ULSD",
                "quantity_gallons": 500.0,
                "unit_price_cents": 33000,
                "line_total_cents": subtotal_cents,
            }
        ],
        "external_refs": {"qbo": "qbo_inv_123"},
        "qbo_push_attempts": qbo_push_attempts,
    }


def _make_qbo_payment_event(
    *,
    tenant_id: str = _TENANT_ID,
    invoice_id: str = _INVOICE_ID,
    account_id: str = _ACCOUNT_ID,
    payment_id: str = "789",
    total_amt: float = 1500.00,
    txn_date: str = "2025-01-15",
    method_name: str = "Check",
) -> Dict[str, Any]:
    """Build a QBO Payment event dict for testing."""
    return {
        "Id": payment_id,
        "TotalAmt": total_amt,
        "TxnDate": txn_date,
        "PaymentMethodRef": {"value": "1", "name": method_name},
        "LinkedTxn": [{"TxnId": "456", "TxnType": "Invoice"}],
        "MetaData": {},
        "tenant_id": tenant_id,
        "matched_invoice_id": invoice_id,
        "matched_account_id": account_id,
    }


def _make_stripe_event(
    *,
    tenant_id: str = _TENANT_ID,
    invoice_id: str = _INVOICE_ID,
    account_id: str = _ACCOUNT_ID,
    event_id: str = "pi_test_001",
    amount: int = 150000,
    status: str = "succeeded",
    payment_method_types: list = None,
) -> Dict[str, Any]:
    """Build a Stripe charge/payment_intent event dict for testing."""
    if payment_method_types is None:
        payment_method_types = ["card"]
    return {
        "id": event_id,
        "amount": amount,
        "currency": "usd",
        "status": status,
        "payment_method_types": payment_method_types,
        "metadata": {
            "invoice_id": invoice_id,
            "account_id": account_id,
            "tenant_id": tenant_id,
        },
        "tenant_id": tenant_id,
        "matched_invoice_id": invoice_id,
        "matched_account_id": account_id,
    }


def _make_sync_run(*, status: str = "success", run_id: str = "run_001", error_details: str = None):
    """Build a mock sync_run result object."""
    run = MagicMock()
    run.status = status
    run.run_id = run_id
    run.error_details = error_details
    return run


def _build_adapter(
    *,
    qbo_connector: Any = None,
    stripe_connector: Any = None,
    invoice_service: Any = None,
    payment_service: Any = None,
) -> CommerceExternalSync:
    """Build a CommerceExternalSync adapter with mocked dependencies."""
    if qbo_connector is None:
        qbo_connector = AsyncMock()
    if stripe_connector is None:
        stripe_connector = AsyncMock()
    if invoice_service is None:
        invoice_service = AsyncMock()
    if payment_service is None:
        payment_service = AsyncMock()
    return CommerceExternalSync(
        qbo_connector=qbo_connector,
        stripe_connector=stripe_connector,
        invoice_service=invoice_service,
        payment_service=payment_service,
    )


# ---------------------------------------------------------------------------
# Test: QBO push flow
# ---------------------------------------------------------------------------


class TestQBOPushFlow:
    """on_invoice_finalized builds correct payload and calls
    qbo_connector.sync_push.

    Validates: Design §7, Requirement 5.6
    """

    @pytest.mark.asyncio
    async def test_builds_payload_and_calls_sync_push(self):
        """on_invoice_finalized dispatches a well-formed payload to sync_push."""
        qbo_connector = AsyncMock()
        qbo_connector.sync_push = AsyncMock(
            return_value=_make_sync_run(status="success")
        )

        adapter = _build_adapter(qbo_connector=qbo_connector)
        invoice = _make_invoice()

        await adapter.on_invoice_finalized(invoice)

        qbo_connector.sync_push.assert_called_once()
        payload = qbo_connector.sync_push.call_args[0][0]

        # Verify payload fields match the invoice
        assert payload["invoice_id"] == _INVOICE_ID
        assert payload["customer_id"] == _CUSTOMER_ID
        assert payload["tenant_id"] == _TENANT_ID
        assert payload["total_cents"] == 1750000
        assert payload["subtotal_cents"] == 1650000
        assert payload["tax_cents"] == 100000
        assert payload["memo"] == "Invoice INV-2025-0042"
        assert payload["reconciliation_id"] == "ord_ext_001"
        assert payload["invoice_doc_number"] == "INV-2025-0042"

    @pytest.mark.asyncio
    async def test_payload_includes_line_item_details(self):
        """Payload includes product_code, delivered_gallons, unit_price_usd."""
        qbo_connector = AsyncMock()
        qbo_connector.sync_push = AsyncMock(
            return_value=_make_sync_run(status="success")
        )

        adapter = _build_adapter(qbo_connector=qbo_connector)
        invoice = _make_invoice()

        await adapter.on_invoice_finalized(invoice)

        payload = qbo_connector.sync_push.call_args[0][0]
        assert payload["product_code"] == "ULSD"
        assert payload["delivered_gallons"] == 500.0
        assert payload["unit_price_usd"] == 330.0  # 33000 cents / 100

    @pytest.mark.asyncio
    async def test_skips_push_when_no_qbo_connector(self):
        """When qbo_connector is None, on_invoice_finalized is a no-op."""
        adapter = CommerceExternalSync(
            qbo_connector=None,
            stripe_connector=AsyncMock(),
            invoice_service=AsyncMock(),
            payment_service=AsyncMock(),
        )
        invoice = _make_invoice()

        # Should not raise
        await adapter.on_invoice_finalized(invoice)

    @pytest.mark.asyncio
    async def test_successful_push_logs_without_error(self):
        """A successful sync_push does not trigger dead-letter or retry."""
        qbo_connector = AsyncMock()
        qbo_connector.sync_push = AsyncMock(
            return_value=_make_sync_run(status="success")
        )
        invoice_service = AsyncMock()

        adapter = _build_adapter(
            qbo_connector=qbo_connector, invoice_service=invoice_service
        )
        invoice = _make_invoice()

        await adapter.on_invoice_finalized(invoice)

        # _es.update_document should NOT be called on success
        invoice_service._es.update_document.assert_not_called()


# ---------------------------------------------------------------------------
# Test: QBO payment ingestion
# ---------------------------------------------------------------------------


class TestQBOPaymentIngestion:
    """on_qbo_payment_observed extracts payment details and calls
    PaymentService.ingest with source="qbo".

    Validates: Design §7, Requirement 6.1
    """

    @pytest.mark.asyncio
    async def test_ingests_qbo_payment_with_correct_params(self):
        """Payment is ingested with source='qbo' and correct amount."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event(total_amt=1500.00)

        await adapter.on_qbo_payment_observed(event)

        payment_service.ingest.assert_called_once()
        call_kwargs = payment_service.ingest.call_args[1]

        assert call_kwargs["tenant_id"] == _TENANT_ID
        assert call_kwargs["invoice_id"] == _INVOICE_ID
        assert call_kwargs["account_id"] == _ACCOUNT_ID
        assert call_kwargs["amount_cents"] == 150000  # $1500.00 * 100
        assert call_kwargs["source"] == "qbo"
        assert call_kwargs["method"] == "check"
        assert call_kwargs["external_id"] == "qbo:789"
        assert call_kwargs["actor"] == "qbo"

    @pytest.mark.asyncio
    async def test_derives_ach_payment_method(self):
        """QBO PaymentMethodRef with 'ACH' maps to method='ach'."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event(method_name="ACH Transfer")

        await adapter.on_qbo_payment_observed(event)

        call_kwargs = payment_service.ingest.call_args[1]
        assert call_kwargs["method"] == "ach"

    @pytest.mark.asyncio
    async def test_derives_wire_payment_method(self):
        """QBO PaymentMethodRef with 'Wire' maps to method='wire'."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event(method_name="Wire Transfer")

        await adapter.on_qbo_payment_observed(event)

        call_kwargs = payment_service.ingest.call_args[1]
        assert call_kwargs["method"] == "wire"

    @pytest.mark.asyncio
    async def test_parses_txn_date_as_received_at(self):
        """TxnDate is parsed into received_at datetime."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event(txn_date="2025-03-20")

        await adapter.on_qbo_payment_observed(event)

        call_kwargs = payment_service.ingest.call_args[1]
        received_at = call_kwargs["received_at"]
        assert received_at is not None
        assert received_at.year == 2025
        assert received_at.month == 3
        assert received_at.day == 20


# ---------------------------------------------------------------------------
# Test: Stripe charge ingestion
# ---------------------------------------------------------------------------


class TestStripeChargeIngestion:
    """on_stripe_charge_observed extracts payment details and calls
    PaymentService.ingest with source="stripe".

    Validates: Design §7, Requirement 6.2
    """

    @pytest.mark.asyncio
    async def test_ingests_stripe_charge_with_correct_params(self):
        """Payment is ingested with source='stripe' and correct amount."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event(amount=250000)

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_called_once()
        call_kwargs = payment_service.ingest.call_args[1]

        assert call_kwargs["tenant_id"] == _TENANT_ID
        assert call_kwargs["invoice_id"] == _INVOICE_ID
        assert call_kwargs["account_id"] == _ACCOUNT_ID
        assert call_kwargs["amount_cents"] == 250000
        assert call_kwargs["source"] == "stripe"
        assert call_kwargs["method"] == "card"
        assert call_kwargs["external_id"] == "pi_test_001"
        assert call_kwargs["actor"] == "stripe"

    @pytest.mark.asyncio
    async def test_derives_ach_from_us_bank_account(self):
        """Stripe payment_method_types=['us_bank_account'] maps to 'ach'."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event(payment_method_types=["us_bank_account"])

        await adapter.on_stripe_charge_observed(event)

        call_kwargs = payment_service.ingest.call_args[1]
        assert call_kwargs["method"] == "ach"

    @pytest.mark.asyncio
    async def test_resolves_tenant_from_metadata(self):
        """tenant_id can be resolved from metadata when not top-level."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event()
        # Remove top-level tenant_id, keep only in metadata
        del event["tenant_id"]

        await adapter.on_stripe_charge_observed(event)

        call_kwargs = payment_service.ingest.call_args[1]
        assert call_kwargs["tenant_id"] == _TENANT_ID


# ---------------------------------------------------------------------------
# Test: Dead-letter path
# ---------------------------------------------------------------------------


class TestDeadLetterPath:
    """After 3 failed QBO pushes, invoice transitions to
    qbo_push_state=dead_letter.

    Validates: Requirement 5.6b
    """

    @pytest.mark.asyncio
    async def test_dead_letter_after_three_failures(self):
        """Invoice is dead-lettered after 3 consecutive push failures."""
        qbo_connector = AsyncMock()
        qbo_connector.sync_push = AsyncMock(
            return_value=_make_sync_run(status="failed", error_details="QBO 500")
        )
        invoice_service = AsyncMock()

        adapter = _build_adapter(
            qbo_connector=qbo_connector, invoice_service=invoice_service
        )

        # Invoice already has 2 prior attempts — this is the 3rd
        invoice = _make_invoice(qbo_push_attempts=2)

        await adapter.on_invoice_finalized(invoice)

        # Should call update_document to mark dead_letter
        invoice_service._es.update_document.assert_called_once()
        update_args = invoice_service._es.update_document.call_args[0]
        update_doc = update_args[2]

        assert update_doc["qbo_push_state"] == "dead_letter"
        assert update_doc["qbo_push_attempts"] == 3
        assert "QBO 500" in update_doc["qbo_push_last_error"]

    @pytest.mark.asyncio
    async def test_retry_state_before_dead_letter_threshold(self):
        """Invoice gets retry state when under 3 attempts."""
        qbo_connector = AsyncMock()
        qbo_connector.sync_push = AsyncMock(
            return_value=_make_sync_run(status="failed", error_details="timeout")
        )
        invoice_service = AsyncMock()

        adapter = _build_adapter(
            qbo_connector=qbo_connector, invoice_service=invoice_service
        )

        # First failure (0 prior attempts)
        invoice = _make_invoice(qbo_push_attempts=0)

        await adapter.on_invoice_finalized(invoice)

        invoice_service._es.update_document.assert_called_once()
        update_args = invoice_service._es.update_document.call_args[0]
        update_doc = update_args[2]

        assert update_doc["qbo_push_state"] == "retry"
        assert update_doc["qbo_push_attempts"] == 1

    @pytest.mark.asyncio
    async def test_second_failure_still_retry(self):
        """Second failure still results in retry state (not dead_letter)."""
        qbo_connector = AsyncMock()
        qbo_connector.sync_push = AsyncMock(
            return_value=_make_sync_run(status="failed", error_details="rate limit")
        )
        invoice_service = AsyncMock()

        adapter = _build_adapter(
            qbo_connector=qbo_connector, invoice_service=invoice_service
        )

        invoice = _make_invoice(qbo_push_attempts=1)

        await adapter.on_invoice_finalized(invoice)

        update_args = invoice_service._es.update_document.call_args[0]
        update_doc = update_args[2]

        assert update_doc["qbo_push_state"] == "retry"
        assert update_doc["qbo_push_attempts"] == 2


# ---------------------------------------------------------------------------
# Test: Error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    """Failures in external sync don't propagate to callers.

    Validates: Cross-cutting invariant — error isolation.
    """

    @pytest.mark.asyncio
    async def test_qbo_push_exception_does_not_propagate(self):
        """Exception in qbo_connector.sync_push is swallowed."""
        qbo_connector = AsyncMock()
        qbo_connector.sync_push = AsyncMock(
            side_effect=RuntimeError("QBO API unreachable")
        )

        adapter = _build_adapter(qbo_connector=qbo_connector)
        invoice = _make_invoice()

        # Should NOT raise
        await adapter.on_invoice_finalized(invoice)

    @pytest.mark.asyncio
    async def test_qbo_payment_exception_does_not_propagate(self):
        """Exception in PaymentService.ingest during QBO ingestion is swallowed."""
        payment_service = AsyncMock()
        payment_service.ingest = AsyncMock(
            side_effect=RuntimeError("ES cluster unavailable")
        )

        adapter = _build_adapter(payment_service=payment_service)
        event = _make_qbo_payment_event()

        # Should NOT raise
        await adapter.on_qbo_payment_observed(event)

    @pytest.mark.asyncio
    async def test_stripe_charge_exception_does_not_propagate(self):
        """Exception in PaymentService.ingest during Stripe ingestion is swallowed."""
        payment_service = AsyncMock()
        payment_service.ingest = AsyncMock(
            side_effect=RuntimeError("Connection reset")
        )

        adapter = _build_adapter(payment_service=payment_service)
        event = _make_stripe_event()

        # Should NOT raise
        await adapter.on_stripe_charge_observed(event)

    @pytest.mark.asyncio
    async def test_dead_letter_update_failure_does_not_propagate(self):
        """Exception in _mark_qbo_push_dead_letter is swallowed."""
        qbo_connector = AsyncMock()
        qbo_connector.sync_push = AsyncMock(
            return_value=_make_sync_run(status="failed", error_details="err")
        )
        invoice_service = AsyncMock()
        invoice_service._es.update_document = AsyncMock(
            side_effect=RuntimeError("ES write failed")
        )

        adapter = _build_adapter(
            qbo_connector=qbo_connector, invoice_service=invoice_service
        )
        invoice = _make_invoice(qbo_push_attempts=2)

        # Should NOT raise even though dead-letter update fails
        await adapter.on_invoice_finalized(invoice)


# ---------------------------------------------------------------------------
# Test: Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Duplicate events are handled gracefully.

    PaymentService.ingest is idempotent via IdempotencyService key
    ``idemp:{tenant_id}:payment:{source}:{external_id}``.
    Duplicate calls should not raise or produce side effects beyond
    the first ingestion.

    Validates: Cross-cutting invariant — idempotency.
    """

    @pytest.mark.asyncio
    async def test_duplicate_qbo_payment_calls_ingest_twice(self):
        """Duplicate QBO events both call ingest (idempotency is in PaymentService)."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event()

        await adapter.on_qbo_payment_observed(event)
        await adapter.on_qbo_payment_observed(event)

        # Both calls go through — PaymentService handles dedup internally
        assert payment_service.ingest.call_count == 2

    @pytest.mark.asyncio
    async def test_duplicate_stripe_charge_calls_ingest_twice(self):
        """Duplicate Stripe events both call ingest (idempotency is in PaymentService)."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event()

        await adapter.on_stripe_charge_observed(event)
        await adapter.on_stripe_charge_observed(event)

        assert payment_service.ingest.call_count == 2

    @pytest.mark.asyncio
    async def test_duplicate_qbo_payment_with_idempotency_error_swallowed(self):
        """If PaymentService raises on duplicate, error is swallowed."""
        payment_service = AsyncMock()
        # First call succeeds, second raises (simulating idempotency rejection)
        payment_service.ingest = AsyncMock(
            side_effect=[None, RuntimeError("Idempotency conflict")]
        )

        adapter = _build_adapter(payment_service=payment_service)
        event = _make_qbo_payment_event()

        await adapter.on_qbo_payment_observed(event)
        # Second call should not raise
        await adapter.on_qbo_payment_observed(event)


# ---------------------------------------------------------------------------
# Test: Skips non-succeeded Stripe events
# ---------------------------------------------------------------------------


class TestSkipsNonSucceededStripe:
    """on_stripe_charge_observed skips events where status != 'succeeded'.

    Validates: Design §7 — only process succeeded charges.
    """

    @pytest.mark.asyncio
    async def test_skips_pending_status(self):
        """Stripe event with status='pending' is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event(status="pending")

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_failed_status(self):
        """Stripe event with status='failed' is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event(status="failed")

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_requires_action_status(self):
        """Stripe event with status='requires_action' is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event(status="requires_action")

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_succeeded_status(self):
        """Stripe event with status='succeeded' IS processed."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event(status="succeeded")

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_called_once()


# ---------------------------------------------------------------------------
# Test: Skips events with missing tenant_id or invoice_id
# ---------------------------------------------------------------------------


class TestSkipsMissingFields:
    """Events with missing tenant_id or invoice_id are skipped gracefully.

    Validates: Defensive validation in each handler.
    """

    @pytest.mark.asyncio
    async def test_qbo_payment_skips_missing_tenant_id(self):
        """QBO event without tenant_id is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event()
        del event["tenant_id"]

        await adapter.on_qbo_payment_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_qbo_payment_skips_missing_invoice_id(self):
        """QBO event without matched_invoice_id is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event()
        del event["matched_invoice_id"]

        await adapter.on_qbo_payment_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_qbo_payment_skips_missing_payment_id(self):
        """QBO event without Id is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event()
        del event["Id"]

        await adapter.on_qbo_payment_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_stripe_skips_missing_tenant_id(self):
        """Stripe event without tenant_id (top-level or metadata) is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event()
        del event["tenant_id"]
        event["metadata"]["tenant_id"] = None  # Also clear metadata

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_stripe_skips_missing_invoice_id(self):
        """Stripe event without invoice_id (top-level or metadata) is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event()
        del event["matched_invoice_id"]
        event["metadata"]["invoice_id"] = None

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_stripe_skips_missing_event_id(self):
        """Stripe event without id is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event()
        del event["id"]

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_qbo_payment_skips_zero_amount(self):
        """QBO event with zero amount is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_qbo_payment_event(total_amt=0.0)

        await adapter.on_qbo_payment_observed(event)

        payment_service.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_stripe_skips_zero_amount(self):
        """Stripe event with zero amount is skipped."""
        payment_service = AsyncMock()
        adapter = _build_adapter(payment_service=payment_service)

        event = _make_stripe_event(amount=0)

        await adapter.on_stripe_charge_observed(event)

        payment_service.ingest.assert_not_called()
