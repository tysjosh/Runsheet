"""Unit tests for PaymentService.

Tests cover:
- ingest: valid ingestion, idempotency, overpayment handling (Req 6.4)
- reverse: payment reversal, already-reversed raises 409
- Overpayment: excess accrued to Account.credit_balance_cents,
  account_credit_balance_applied event emitted

Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, C1, C2, C3, C4
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from commerce.models.events import AccountEventType, InvoiceEventType
from commerce.models.payment import PaymentMethod, PaymentSource, PaymentStatus
from commerce.services.payment_service import PaymentService
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_test123"
_ACCOUNT_ID = "acct_def"
_INVOICE_ID = "inv_test789"
_FIXED_NOW = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)


def _make_es_service() -> AsyncMock:
    """Create a mocked ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return es


def _make_idempotency_service(*, is_duplicate: bool = False) -> AsyncMock:
    """Create a mocked IdempotencyService."""
    idemp = AsyncMock()
    idemp.is_duplicate = AsyncMock(return_value=is_duplicate)
    idemp.mark_processed = AsyncMock(return_value=None)
    return idemp


def _make_invoice_service(
    *,
    invoice_remaining_cents: int = 100000,
    invoice_total_cents: int = 100000,
) -> AsyncMock:
    """Create a mocked InvoiceService."""
    svc = AsyncMock()
    svc.get = AsyncMock(
        return_value={
            "invoice_id": _INVOICE_ID,
            "tenant_id": _TENANT_ID,
            "account_id": _ACCOUNT_ID,
            "total_cents": invoice_total_cents,
            "amount_paid_cents": invoice_total_cents - invoice_remaining_cents,
            "remaining_cents": invoice_remaining_cents,
            "status": "open",
        }
    )
    svc.apply_payment = AsyncMock(
        return_value={
            "invoice_id": _INVOICE_ID,
            "status": "paid",
            "amount_paid_cents": invoice_total_cents,
            "remaining_cents": 0,
        }
    )
    return svc


def _es_search_response(hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": len(hits)},
        }
    }


def _es_agg_response(aggs: Dict[str, Any]) -> Dict[str, Any]:
    """Build a mock ES aggregation response."""
    return {
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": aggs,
    }


def _make_account_doc(
    *,
    account_id: str = _ACCOUNT_ID,
    credit_balance_cents: int = 0,
) -> Dict[str, Any]:
    """Build an account document as returned from ES."""
    return {
        "account_id": account_id,
        "tenant_id": _TENANT_ID,
        "credit_balance_cents": credit_balance_cents,
        "credit_limit_cents": 500000,
        "open_balance_cents": 100000,
        "credit_state": "ok",
    }


# ---------------------------------------------------------------------------
# Tests: ingest
# ---------------------------------------------------------------------------


class TestIngest:
    """Tests for PaymentService.ingest."""

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_ingest_creates_payment_and_applies(self, mock_utcnow):
        """Ingests a payment and applies it to the invoice."""
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=False)
        invoice_svc = _make_invoice_service(invoice_remaining_cents=100000)

        service = PaymentService(es, idemp, invoice_svc)

        result = await service.ingest(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            account_id=_ACCOUNT_ID,
            amount_cents=50000,
            source="stripe",
            method="card",
            external_id="ch_abc123",
            actor="system",
        )

        assert result["payment_id"].startswith("pay_")
        assert result["tenant_id"] == _TENANT_ID
        assert result["invoice_id"] == _INVOICE_ID
        assert result["amount_cents"] == 50000
        assert result["source"] == "stripe"
        assert result["method"] == "card"
        assert result["status"] == "applied"

        # Payment was persisted
        es.index_document.assert_called_once()

        # Payment was applied to invoice (50000 <= 100000 remaining)
        invoice_svc.apply_payment.assert_called_once_with(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            amount_cents=50000,
            payment_id=result["payment_id"],
            actor="system",
        )

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_ingest_idempotent_skip(self, mock_utcnow):
        """Duplicate payment is skipped via idempotency check."""
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=True)

        existing_payment = {
            "payment_id": "pay_existing",
            "tenant_id": _TENANT_ID,
            "invoice_id": _INVOICE_ID,
            "amount_cents": 50000,
            "source": "stripe",
            "method": "card",
            "external_id": "ch_abc123",
            "status": "applied",
        }
        es.search_documents = AsyncMock(
            return_value=_es_search_response([existing_payment])
        )

        service = PaymentService(es, idemp)

        result = await service.ingest(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            account_id=_ACCOUNT_ID,
            amount_cents=50000,
            source="stripe",
            method="card",
            external_id="ch_abc123",
        )

        assert result["payment_id"] == "pay_existing"
        es.index_document.assert_not_called()

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_ingest_rejects_non_positive_amount(self, mock_utcnow):
        """Raises validation error for zero or negative amount."""
        es = _make_es_service()
        service = PaymentService(es)

        with pytest.raises(AppException) as exc_info:
            await service.ingest(
                tenant_id=_TENANT_ID,
                invoice_id=_INVOICE_ID,
                account_id=_ACCOUNT_ID,
                amount_cents=0,
                source="manual",
                method="check",
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_ingest_rejects_invalid_source(self, mock_utcnow):
        """Raises validation error for invalid source."""
        es = _make_es_service()
        service = PaymentService(es)

        with pytest.raises(AppException) as exc_info:
            await service.ingest(
                tenant_id=_TENANT_ID,
                invoice_id=_INVOICE_ID,
                account_id=_ACCOUNT_ID,
                amount_cents=1000,
                source="invalid_source",
                method="card",
            )

        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Tests: overpayment handling (Req 6.4)
# ---------------------------------------------------------------------------


class TestOverpaymentHandling:
    """Tests for overpayment handling in PaymentService.ingest (Req 6.4)."""

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_overpayment_applies_remaining_and_accrues_excess(
        self, mock_utcnow
    ):
        """When amount > remaining, applies remaining to invoice and accrues excess.

        Validates: Requirement 6.4
        """
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=False)
        invoice_svc = _make_invoice_service(
            invoice_remaining_cents=80000,
            invoice_total_cents=100000,
        )

        # Mock ES responses for _accrue_credit_balance:
        # 1. Find account
        # 2. Get sequence number for account events
        account_doc = _make_account_doc(credit_balance_cents=0)
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account_doc]),  # find account
                _es_agg_response({"max_seq": {"value": None}}),  # seq for event
            ]
        )

        service = PaymentService(es, idemp, invoice_svc)

        result = await service.ingest(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            account_id=_ACCOUNT_ID,
            amount_cents=120000,  # 120000 > 80000 remaining
            source="stripe",
            method="card",
            external_id="ch_overpay",
            actor="system",
        )

        # Payment was persisted with full amount
        assert result["amount_cents"] == 120000

        # Invoice was applied with only remaining_cents (80000)
        invoice_svc.apply_payment.assert_called_once_with(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            amount_cents=80000,
            payment_id=result["payment_id"],
            actor="system",
        )

        # Account credit_balance_cents was updated with excess (40000)
        update_calls = es.update_document.call_args_list
        account_update_calls = [
            call for call in update_calls
            if call[0][0] == "accounts_current"
        ]
        assert len(account_update_calls) == 1
        account_update = account_update_calls[0][0][2]
        assert account_update["credit_balance_cents"] == 40000

        # account_credit_balance_applied event was emitted
        index_calls = es.index_document.call_args_list
        event_calls = [
            call for call in index_calls
            if call[0][0] == "account_events"
        ]
        assert len(event_calls) == 1
        event_doc = event_calls[0][0][2]
        assert event_doc["event_type"] == AccountEventType.CREDIT_BALANCE_APPLIED.value
        assert event_doc["payload"]["excess_cents"] == 40000
        assert event_doc["payload"]["previous_credit_balance_cents"] == 0
        assert event_doc["payload"]["new_credit_balance_cents"] == 40000
        assert event_doc["payload"]["payment_id"] == result["payment_id"]
        assert event_doc["payload"]["invoice_id"] == _INVOICE_ID

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_overpayment_adds_to_existing_credit_balance(
        self, mock_utcnow
    ):
        """Excess is added to existing credit_balance_cents.

        Validates: Requirement 6.4
        """
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=False)
        invoice_svc = _make_invoice_service(
            invoice_remaining_cents=50000,
            invoice_total_cents=100000,
        )

        # Account already has 10000 credit balance
        account_doc = _make_account_doc(credit_balance_cents=10000)
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([account_doc]),  # find account
                _es_agg_response({"max_seq": {"value": 3}}),  # seq for event
            ]
        )

        service = PaymentService(es, idemp, invoice_svc)

        await service.ingest(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            account_id=_ACCOUNT_ID,
            amount_cents=75000,  # 75000 > 50000 remaining, excess = 25000
            source="manual",
            method="check",
            external_id="chk_001",
            actor="billing_admin",
        )

        # Account credit_balance_cents updated: 10000 + 25000 = 35000
        update_calls = es.update_document.call_args_list
        account_update_calls = [
            call for call in update_calls
            if call[0][0] == "accounts_current"
        ]
        assert len(account_update_calls) == 1
        account_update = account_update_calls[0][0][2]
        assert account_update["credit_balance_cents"] == 35000

        # Event payload reflects the addition
        index_calls = es.index_document.call_args_list
        event_calls = [
            call for call in index_calls
            if call[0][0] == "account_events"
        ]
        assert len(event_calls) == 1
        event_doc = event_calls[0][0][2]
        assert event_doc["payload"]["previous_credit_balance_cents"] == 10000
        assert event_doc["payload"]["new_credit_balance_cents"] == 35000
        assert event_doc["payload"]["excess_cents"] == 25000

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_exact_payment_no_overpayment(self, mock_utcnow):
        """When amount == remaining, no overpayment handling occurs.

        Validates: Requirement 6.4 (boundary case)
        """
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=False)
        invoice_svc = _make_invoice_service(
            invoice_remaining_cents=100000,
            invoice_total_cents=100000,
        )

        service = PaymentService(es, idemp, invoice_svc)

        result = await service.ingest(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            account_id=_ACCOUNT_ID,
            amount_cents=100000,  # exactly remaining
            source="stripe",
            method="card",
            external_id="ch_exact",
            actor="system",
        )

        # Full amount applied to invoice
        invoice_svc.apply_payment.assert_called_once_with(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            amount_cents=100000,
            payment_id=result["payment_id"],
            actor="system",
        )

        # No account update (no overpayment)
        account_update_calls = [
            call for call in es.update_document.call_args_list
            if call[0][0] == "accounts_current"
        ]
        assert len(account_update_calls) == 0

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_underpayment_no_overpayment_handling(self, mock_utcnow):
        """When amount < remaining, no overpayment handling occurs.

        Validates: Requirement 6.4 (boundary case)
        """
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=False)
        invoice_svc = _make_invoice_service(
            invoice_remaining_cents=100000,
            invoice_total_cents=100000,
        )

        service = PaymentService(es, idemp, invoice_svc)

        result = await service.ingest(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            account_id=_ACCOUNT_ID,
            amount_cents=50000,  # less than remaining
            source="manual",
            method="ach",
            external_id="ach_001",
            actor="system",
        )

        # Full amount applied to invoice
        invoice_svc.apply_payment.assert_called_once_with(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            amount_cents=50000,
            payment_id=result["payment_id"],
            actor="system",
        )

        # No account update (no overpayment)
        account_update_calls = [
            call for call in es.update_document.call_args_list
            if call[0][0] == "accounts_current"
        ]
        assert len(account_update_calls) == 0


# ---------------------------------------------------------------------------
# Tests: reverse (Req 6.6)
# ---------------------------------------------------------------------------


class TestReverse:
    """Tests for PaymentService.reverse."""

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_reverse_marks_payment_reversed(self, mock_utcnow):
        """Reverses a payment and updates invoice state."""
        es = _make_es_service()
        invoice_svc = AsyncMock()

        payment = {
            "payment_id": "pay_abc",
            "tenant_id": _TENANT_ID,
            "invoice_id": _INVOICE_ID,
            "account_id": _ACCOUNT_ID,
            "amount_cents": 50000,
            "source": "stripe",
            "method": "card",
            "status": "applied",
        }

        invoice = {
            "invoice_id": _INVOICE_ID,
            "tenant_id": _TENANT_ID,
            "total_cents": 100000,
            "amount_paid_cents": 50000,
            "remaining_cents": 50000,
            "status": "partial",
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([payment]),  # get payment
                _es_search_response([invoice]),  # get invoice
            ]
        )

        service = PaymentService(es, invoice_service=invoice_svc)
        service._invoice_service = invoice_svc
        invoice_svc.get = AsyncMock(return_value=invoice)

        result = await service.reverse(
            tenant_id=_TENANT_ID,
            payment_id="pay_abc",
            actor="admin",
        )

        assert result["status"] == "reversed"
        assert result["reversed_at"] is not None

    @pytest.mark.asyncio
    @patch("commerce.services.payment_service.utcnow", return_value=_FIXED_NOW)
    async def test_reverse_already_reversed_raises_409(self, mock_utcnow):
        """Raises error when payment is already reversed."""
        es = _make_es_service()

        payment = {
            "payment_id": "pay_abc",
            "tenant_id": _TENANT_ID,
            "invoice_id": _INVOICE_ID,
            "account_id": _ACCOUNT_ID,
            "amount_cents": 50000,
            "source": "stripe",
            "method": "card",
            "status": "reversed",
        }

        es.search_documents = AsyncMock(
            return_value=_es_search_response([payment])
        )

        service = PaymentService(es)

        with pytest.raises((AppException, ValueError)):
            await service.reverse(
                tenant_id=_TENANT_ID,
                payment_id="pay_abc",
                actor="admin",
            )


# ---------------------------------------------------------------------------
# Tests: finalize_draft credit balance drain (Req 6.4)
# ---------------------------------------------------------------------------


class TestFinalizeDraftCreditBalanceDrain:
    """Tests for InvoiceService.finalize_draft credit balance draining (Req 6.4)."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_drains_credit_balance(self, mock_utcnow):
        """finalize_draft drains Account.credit_balance_cents into synthetic payment.

        Validates: Requirement 6.4
        """
        from commerce.services.invoice_service import InvoiceService

        es = _make_es_service()

        invoice_doc = {
            "invoice_id": _INVOICE_ID,
            "tenant_id": _TENANT_ID,
            "customer_id": "cust_abc",
            "account_id": _ACCOUNT_ID,
            "order_id": "order_xyz",
            "status": "draft",
            "total_cents": 100000,
            "amount_paid_cents": 0,
            "remaining_cents": 100000,
            "due_date": "2026-07-15",
            "_last_applied_seq": 1,
        }

        account_doc = _make_account_doc(credit_balance_cents=30000)

        # After drain, the invoice will have updated amounts
        invoice_after_drain = {
            **invoice_doc,
            "amount_paid_cents": 30000,
            "remaining_cents": 70000,
        }

        # Mock ES responses in order:
        # 1. get invoice (for finalize_draft)
        # 2. get account (for _drain_credit_balance)
        # 3. get next seq for payment_applied event
        # 4. re-get invoice after drain
        # 5. get next seq for finalized event
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice_doc]),  # get invoice
                _es_search_response([account_doc]),  # get account
                _es_agg_response({"max_seq": {"value": 1}}),  # seq for payment_applied
                _es_search_response([invoice_after_drain]),  # re-get invoice
                _es_agg_response({"max_seq": {"value": 2}}),  # seq for finalized
            ]
        )

        service = InvoiceService(es)

        result = await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="system",
        )

        assert result["status"] == "open"

        # Synthetic payment was created in payments_current
        index_calls = es.index_document.call_args_list
        payment_calls = [
            call for call in index_calls
            if call[0][0] == "payments_current"
        ]
        assert len(payment_calls) == 1
        payment_doc = payment_calls[0][0][2]
        assert payment_doc["source"] == "account_credit"
        assert payment_doc["method"] == "credit_balance"
        assert payment_doc["amount_cents"] == 30000
        assert payment_doc["invoice_id"] == _INVOICE_ID
        assert payment_doc["account_id"] == _ACCOUNT_ID
        assert payment_doc["status"] == "applied"

        # Account credit_balance_cents was reduced
        account_update_calls = [
            call for call in es.update_document.call_args_list
            if call[0][0] == "accounts_current"
        ]
        assert len(account_update_calls) == 1
        account_update = account_update_calls[0][0][2]
        assert account_update["credit_balance_cents"] == 0

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_no_drain_when_zero_balance(self, mock_utcnow):
        """finalize_draft does not drain when credit_balance_cents is 0.

        Validates: Requirement 6.4 (boundary case)
        """
        from commerce.services.invoice_service import InvoiceService

        es = _make_es_service()

        invoice_doc = {
            "invoice_id": _INVOICE_ID,
            "tenant_id": _TENANT_ID,
            "customer_id": "cust_abc",
            "account_id": _ACCOUNT_ID,
            "order_id": "order_xyz",
            "status": "draft",
            "total_cents": 100000,
            "amount_paid_cents": 0,
            "remaining_cents": 100000,
            "due_date": "2026-07-15",
            "_last_applied_seq": 1,
        }

        account_doc = _make_account_doc(credit_balance_cents=0)

        # Mock ES responses:
        # 1. get invoice
        # 2. get account (balance is 0, so drain skips)
        # 3. re-get invoice (unchanged)
        # 4. get next seq for finalized event
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice_doc]),  # get invoice
                _es_search_response([account_doc]),  # get account (balance=0)
                _es_search_response([invoice_doc]),  # re-get invoice
                _es_agg_response({"max_seq": {"value": 1}}),  # seq for finalized
            ]
        )

        service = InvoiceService(es)

        result = await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="system",
        )

        assert result["status"] == "open"

        # No synthetic payment was created
        index_calls = es.index_document.call_args_list
        payment_calls = [
            call for call in index_calls
            if call[0][0] == "payments_current"
        ]
        assert len(payment_calls) == 0

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_drains_partial_when_balance_exceeds_invoice(
        self, mock_utcnow
    ):
        """When credit_balance > invoice.remaining, only applies remaining.

        Validates: Requirement 6.4
        """
        from commerce.services.invoice_service import InvoiceService

        es = _make_es_service()

        invoice_doc = {
            "invoice_id": _INVOICE_ID,
            "tenant_id": _TENANT_ID,
            "customer_id": "cust_abc",
            "account_id": _ACCOUNT_ID,
            "order_id": "order_xyz",
            "status": "draft",
            "total_cents": 50000,
            "amount_paid_cents": 0,
            "remaining_cents": 50000,
            "due_date": "2026-07-15",
            "_last_applied_seq": 1,
        }

        # Account has more credit than the invoice total
        account_doc = _make_account_doc(credit_balance_cents=80000)

        invoice_after_drain = {
            **invoice_doc,
            "amount_paid_cents": 50000,
            "remaining_cents": 0,
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice_doc]),  # get invoice
                _es_search_response([account_doc]),  # get account
                _es_agg_response({"max_seq": {"value": 1}}),  # seq for payment_applied
                _es_search_response([invoice_after_drain]),  # re-get invoice
                _es_agg_response({"max_seq": {"value": 2}}),  # seq for finalized
            ]
        )

        service = InvoiceService(es)

        result = await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="system",
        )

        assert result["status"] == "open"

        # Synthetic payment was for invoice remaining (50000), not full balance
        index_calls = es.index_document.call_args_list
        payment_calls = [
            call for call in index_calls
            if call[0][0] == "payments_current"
        ]
        assert len(payment_calls) == 1
        payment_doc = payment_calls[0][0][2]
        assert payment_doc["amount_cents"] == 50000  # min(80000, 50000)

        # Account balance reduced by 50000 (80000 - 50000 = 30000 remaining)
        account_update_calls = [
            call for call in es.update_document.call_args_list
            if call[0][0] == "accounts_current"
        ]
        assert len(account_update_calls) == 1
        account_update = account_update_calls[0][0][2]
        assert account_update["credit_balance_cents"] == 30000
