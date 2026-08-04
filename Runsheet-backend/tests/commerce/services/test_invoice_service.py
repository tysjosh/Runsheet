"""Unit tests for InvoiceService.

Tests cover:
- generate_from_order: valid generation, idempotent (same order_id twice)
- finalize_draft: draft→open transition, already-open is idempotent, non-draft raises 409
- apply_payment: open→partial, partial→paid, invalid status raises 409
- mark_overdue: open/partial→overdue, already-overdue is idempotent
- void: no payments → direct void, with payments + force=false → 409,
        with payments + force=true → cascading reversal (source=void_cascade)
- Event sourcing: every method writes an event before updating projection,
  projection rebuild from event log

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, C1, C4, C7
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from commerce.models.events import InvoiceEventType
from commerce.models.invoice import InvoiceStatus, QBOPushState
from commerce.services.invoice_service import InvoiceService
from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_test123"
_CUSTOMER_ID = "cust_abc"
_ACCOUNT_ID = "acct_def"
_ORDER_ID = "order_xyz"
_INVOICE_ID = "inv_test789"
_FIXED_NOW = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

_SAMPLE_LINE_ITEMS = [
    {
        "line_id": "line_001",
        "product_code": "ULSD",
        "quantity_gallons": 500.0,
        "unit_price_cents": 350,
        "subtotal_cents": 175000,
    },
    {
        "line_id": "line_002",
        "product_code": "DEF",
        "quantity_gallons": 100.0,
        "unit_price_cents": 200,
        "subtotal_cents": 20000,
    },
]


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


def _make_invoice_doc(
    *,
    invoice_id: str = _INVOICE_ID,
    tenant_id: str = _TENANT_ID,
    status: str = InvoiceStatus.DRAFT.value,
    total_cents: int = 195000,
    amount_paid_cents: int = 0,
    remaining_cents: int = 195000,
    order_id: str = _ORDER_ID,
) -> Dict[str, Any]:
    """Build an invoice document as returned from ES."""
    return {
        "invoice_id": invoice_id,
        "tenant_id": tenant_id,
        "customer_id": _CUSTOMER_ID,
        "account_id": _ACCOUNT_ID,
        "order_id": order_id,
        "invoice_number": "INV-0001",
        "status": status,
        "total_cents": total_cents,
        "amount_paid_cents": amount_paid_cents,
        "remaining_cents": remaining_cents,
        "tax_cents": 0,
        "subtotal_cents": total_cents,
        "line_items": _SAMPLE_LINE_ITEMS,
        "issued_at": None,
        "due_date": "2026-07-15",
        "finalized_at": None,
        "voided_at": None,
        "void_reason": None,
        "qbo_push_state": QBOPushState.PENDING.value,
        "qbo_push_attempts": 0,
        "qbo_push_last_error": None,
        "external_refs": {},
        "created_at": _FIXED_NOW.isoformat(),
        "updated_at": _FIXED_NOW.isoformat(),
        "_last_applied_seq": 1,
    }


# ---------------------------------------------------------------------------
# Tests: generate_from_order
# ---------------------------------------------------------------------------


class TestGenerateFromOrder:
    """Tests for InvoiceService.generate_from_order."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_generate_creates_invoice_in_draft(self, mock_utcnow):
        """Generates an invoice in draft status with correct totals."""
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=False)

        # Mock: no existing events (sequence starts at 1)
        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"max_seq": {"value": None}})
        )

        service = InvoiceService(es, idemp)

        result = await service.generate_from_order(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            customer_id=_CUSTOMER_ID,
            account_id=_ACCOUNT_ID,
            line_items=_SAMPLE_LINE_ITEMS,
            tax_cents=5000,
            net_terms_days=30,
            actor="system",
        )

        # Verify invoice fields
        assert result["tenant_id"] == _TENANT_ID
        assert result["customer_id"] == _CUSTOMER_ID
        assert result["account_id"] == _ACCOUNT_ID
        assert result["order_id"] == _ORDER_ID
        assert result["status"] == InvoiceStatus.DRAFT.value
        assert result["subtotal_cents"] == 195000  # 175000 + 20000
        assert result["tax_cents"] == 5000
        assert result["total_cents"] == 200000  # 195000 + 5000
        assert result["remaining_cents"] == 200000
        assert result["amount_paid_cents"] == 0
        assert result["invoice_id"].startswith("inv_")

        # Event was written (index_document called for event + projection)
        assert es.index_document.call_count == 2
        # First call is the event
        event_call = es.index_document.call_args_list[0]
        event_doc = event_call[0][2]
        assert event_doc["event_type"] == InvoiceEventType.CREATED.value
        assert event_doc["sequence_number"] == 1
        assert event_doc["payload"]["order_id"] == _ORDER_ID

        # Idempotency was marked
        idemp.mark_processed.assert_called_once()

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_generate_idempotent_same_order_id(self, mock_utcnow):
        """Second call for same order_id returns existing invoice, no new event."""
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=True)

        existing_invoice = _make_invoice_doc(order_id=_ORDER_ID)

        # Mock: idempotency says duplicate, then find existing invoice
        es.search_documents = AsyncMock(
            return_value=_es_search_response([existing_invoice])
        )

        service = InvoiceService(es, idemp)

        result = await service.generate_from_order(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            customer_id=_CUSTOMER_ID,
            account_id=_ACCOUNT_ID,
            line_items=_SAMPLE_LINE_ITEMS,
            actor="system",
        )

        # Returns the existing invoice
        assert result["invoice_id"] == _INVOICE_ID
        assert result["order_id"] == _ORDER_ID

        # No new event or projection written
        es.index_document.assert_not_called()
        es.update_document.assert_not_called()

        # Idempotency was NOT re-marked
        idemp.mark_processed.assert_not_called()

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_generate_writes_event_before_projection(self, mock_utcnow):
        """Event is written to invoice_events before the projection (C7)."""
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=False)

        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"max_seq": {"value": None}})
        )

        call_order: List[str] = []
        original_index = es.index_document

        async def track_index(index, doc_id, doc):
            if "event_type" in doc:
                call_order.append("event")
            else:
                call_order.append("projection")
            return await original_index(index, doc_id, doc)

        es.index_document = AsyncMock(side_effect=track_index)

        service = InvoiceService(es, idemp)

        await service.generate_from_order(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            customer_id=_CUSTOMER_ID,
            account_id=_ACCOUNT_ID,
            line_items=_SAMPLE_LINE_ITEMS,
            actor="system",
        )

        # Event must come before projection
        assert call_order == ["event", "projection"]

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_generate_persists_delivery_snapshot_and_source_refs(
        self, mock_utcnow
    ):
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"max_seq": {"value": None}})
        )
        service = InvoiceService(es)
        delivery_result = {
            "pod_id": "pod-42",
            "actual_gallons": 500.0,
            "delivered_at": "2026-06-15T09:30:00Z",
            "source_system": "legacy-erp",
            "source_record_id": "SO-42",
        }
        line_items = [_SAMPLE_LINE_ITEMS[0]]

        result = await service.generate_from_order(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            customer_id=_CUSTOMER_ID,
            account_id=_ACCOUNT_ID,
            line_items=line_items,
            delivery_result=delivery_result,
        )

        assert result["delivery_result"] == delivery_result
        assert result["pod_id"] == "pod-42"
        assert result["external_refs"] == {
            "source_system": "legacy-erp",
            "source_record_id": "SO-42",
            "pod_id": "pod-42",
        }
        event = es.index_document.call_args_list[0].args[2]
        assert event["payload"]["actual_gallons"] == 500.0

    @pytest.mark.asyncio
    async def test_generate_rejects_invoice_quantity_that_differs_from_pod(self):
        service = InvoiceService(_make_es_service())

        with pytest.raises(AppException) as exc:
            await service.generate_from_order(
                tenant_id=_TENANT_ID,
                order_id=_ORDER_ID,
                customer_id=_CUSTOMER_ID,
                account_id=_ACCOUNT_ID,
                line_items=[_SAMPLE_LINE_ITEMS[0]],
                delivery_result={
                    "pod_id": "pod-42",
                    "actual_gallons": 475.0,
                },
            )

        assert exc.value.error_code.value == "VALIDATION_ERROR"
        assert exc.value.details["invoice_gallons"] == 500.0



# ---------------------------------------------------------------------------
# Tests: finalize_draft
# ---------------------------------------------------------------------------


class TestFinalizeDraft:
    """Tests for InvoiceService.finalize_draft."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_transitions_draft_to_open(self, mock_utcnow):
        """Transitions a draft invoice to open status."""
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.DRAFT.value)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_search_response([]),  # get account for _drain_credit_balance (none found, skip)
                _es_search_response([invoice]),  # re-get invoice after drain
                _es_agg_response({"max_seq": {"value": 1}}),  # next seq for finalized event
            ]
        )

        service = InvoiceService(es)

        result = await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="admin_user",
        )

        assert result["status"] == InvoiceStatus.OPEN.value
        assert result["issued_at"] is not None
        assert result["finalized_at"] is not None

        # Event was written
        event_call = es.index_document.call_args
        event_doc = event_call[0][2]
        assert event_doc["event_type"] == InvoiceEventType.FINALIZED.value
        assert event_doc["payload"]["previous_status"] == "draft"
        assert event_doc["payload"]["new_status"] == "open"

        # Projection was updated
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_idempotent_when_already_open(self, mock_utcnow):
        """Returns existing invoice when already open (idempotent)."""
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.OPEN.value)

        es.search_documents = AsyncMock(
            return_value=_es_search_response([invoice])
        )

        service = InvoiceService(es)

        result = await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="admin_user",
        )

        # Returns the existing open invoice
        assert result["status"] == InvoiceStatus.OPEN.value

        # No new event or projection update
        es.index_document.assert_not_called()
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_raises_409_for_non_draft(self, mock_utcnow):
        """Raises 409 when invoice is not in draft status (and not open)."""
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.PAID.value)

        es.search_documents = AsyncMock(
            return_value=_es_search_response([invoice])
        )

        service = InvoiceService(es)

        with pytest.raises(AppException) as exc_info:
            await service.finalize_draft(
                tenant_id=_TENANT_ID,
                invoice_id=_INVOICE_ID,
                actor="admin_user",
            )

        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_fires_external_sync_callback(self, mock_utcnow):
        """Finalize fires on_invoice_finalized as a non-blocking post-commit callback (Task 9.2)."""
        import asyncio

        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.DRAFT.value)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_search_response([]),  # get account for _drain_credit_balance (none found)
                _es_search_response([invoice]),  # re-get invoice after drain
                _es_agg_response({"max_seq": {"value": 1}}),  # next seq for finalized event
            ]
        )

        external_sync = AsyncMock()
        external_sync.on_invoice_finalized = AsyncMock(return_value=None)

        service = InvoiceService(es, external_sync=external_sync)

        result = await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="admin_user",
        )

        # Allow the fire-and-forget task to complete
        await asyncio.sleep(0.01)

        # External sync was called with the finalized invoice
        external_sync.on_invoice_finalized.assert_called_once()
        call_args = external_sync.on_invoice_finalized.call_args[0][0]
        assert call_args["status"] == InvoiceStatus.OPEN.value
        assert call_args["invoice_id"] == _INVOICE_ID

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_external_sync_failure_does_not_affect_response(self, mock_utcnow):
        """External sync failure is error-isolated — finalize still succeeds (Design §7)."""
        import asyncio

        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.DRAFT.value)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_search_response([]),  # get account for _drain_credit_balance (none found)
                _es_search_response([invoice]),  # re-get invoice after drain
                _es_agg_response({"max_seq": {"value": 1}}),  # next seq for finalized event
            ]
        )

        external_sync = AsyncMock()
        external_sync.on_invoice_finalized = AsyncMock(
            side_effect=RuntimeError("QBO push failed")
        )

        service = InvoiceService(es, external_sync=external_sync)

        # Should NOT raise even though external sync fails
        result = await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="admin_user",
        )

        # Allow the fire-and-forget task to complete (and fail silently)
        await asyncio.sleep(0.01)

        # Finalize still succeeded
        assert result["status"] == InvoiceStatus.OPEN.value
        assert result["finalized_at"] is not None

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_finalize_no_external_sync_when_not_configured(self, mock_utcnow):
        """When external_sync is None, finalize works without calling any callback."""
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.DRAFT.value)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_search_response([]),  # get account for _drain_credit_balance (none found)
                _es_search_response([invoice]),  # re-get invoice after drain
                _es_agg_response({"max_seq": {"value": 1}}),  # next seq for finalized event
            ]
        )

        # No external_sync configured
        service = InvoiceService(es, external_sync=None)

        result = await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="admin_user",
        )

        # Finalize still works
        assert result["status"] == InvoiceStatus.OPEN.value


class TestRetryExternalSync:
    @pytest.mark.asyncio
    async def test_resets_dead_letter_and_executes_connector_retry(self):
        es = _make_es_service()
        external_sync = AsyncMock()
        service = InvoiceService(es, external_sync=external_sync)
        dead_letter = _make_invoice_doc(status=InvoiceStatus.OPEN.value)
        dead_letter.update(
            {
                "qbo_push_state": "dead_letter",
                "qbo_push_attempts": 3,
                "qbo_push_last_error": "QBO unavailable",
            }
        )
        refreshed = {
            **dead_letter,
            "qbo_push_state": "pushed",
            "external_refs": {"qbo": "inv:9876"},
        }
        service.get = AsyncMock(side_effect=[dead_letter, refreshed])

        with patch(
            "commerce.services.commerce_persistence_bridge.mirror_invoice_fields",
            new=AsyncMock(),
        ):
            result = await service.retry_external_sync(
                tenant_id=_TENANT_ID,
                invoice_id=_INVOICE_ID,
            )

        reset = es.update_document.call_args.args[2]
        assert reset["qbo_push_state"] == "pending"
        assert reset["qbo_push_attempts"] == 0
        retried = external_sync.retry_invoice_push.call_args.args[0]
        assert retried["qbo_push_state"] == "pending"
        assert retried["qbo_push_attempts"] == 0
        assert result["external_refs"]["qbo"] == "inv:9876"

    @pytest.mark.asyncio
    async def test_draft_cannot_be_exported(self):
        service = InvoiceService(_make_es_service(), external_sync=AsyncMock())
        service.get = AsyncMock(
            return_value=_make_invoice_doc(status=InvoiceStatus.DRAFT.value)
        )

        with pytest.raises(AppException) as exc:
            await service.retry_external_sync(
                tenant_id=_TENANT_ID,
                invoice_id=_INVOICE_ID,
            )

        assert exc.value.error_code.value == "COMMERCE_INVOICE_INVALID_STATE"



# ---------------------------------------------------------------------------
# Tests: apply_payment
# ---------------------------------------------------------------------------


class TestApplyPayment:
    """Tests for InvoiceService.apply_payment."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_payment_open_to_partial(self, mock_utcnow):
        """Transitions open invoice to partial when partial payment applied."""
        es = _make_es_service()
        invoice = _make_invoice_doc(
            status=InvoiceStatus.OPEN.value,
            total_cents=195000,
            amount_paid_cents=0,
            remaining_cents=195000,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_agg_response({"max_seq": {"value": 2}}),  # next seq
            ]
        )

        service = InvoiceService(es)

        result = await service.apply_payment(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            amount_cents=50000,
            payment_id="pay_001",
            actor="stripe",
        )

        assert result["status"] == InvoiceStatus.PARTIAL.value
        assert result["amount_paid_cents"] == 50000
        assert result["remaining_cents"] == 145000

        # Event was written
        event_call = es.index_document.call_args
        event_doc = event_call[0][2]
        assert event_doc["event_type"] == InvoiceEventType.PAYMENT_APPLIED.value
        assert event_doc["payload"]["amount_cents"] == 50000
        assert event_doc["payload"]["new_status"] == "partial"

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_payment_partial_to_paid(self, mock_utcnow):
        """Transitions partial invoice to paid when full amount reached."""
        es = _make_es_service()
        invoice = _make_invoice_doc(
            status=InvoiceStatus.PARTIAL.value,
            total_cents=195000,
            amount_paid_cents=100000,
            remaining_cents=95000,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_agg_response({"max_seq": {"value": 3}}),  # next seq
            ]
        )

        service = InvoiceService(es)

        result = await service.apply_payment(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            amount_cents=95000,
            payment_id="pay_002",
            actor="stripe",
        )

        assert result["status"] == InvoiceStatus.PAID.value
        assert result["amount_paid_cents"] == 195000
        assert result["remaining_cents"] == 0

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_payment_overdue_to_paid(self, mock_utcnow):
        """Overdue invoice transitions to paid when full amount applied."""
        es = _make_es_service()
        invoice = _make_invoice_doc(
            status=InvoiceStatus.OVERDUE.value,
            total_cents=195000,
            amount_paid_cents=0,
            remaining_cents=195000,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_agg_response({"max_seq": {"value": 3}}),  # next seq
            ]
        )

        service = InvoiceService(es)

        result = await service.apply_payment(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            amount_cents=195000,
            payment_id="pay_003",
            actor="manual",
        )

        assert result["status"] == InvoiceStatus.PAID.value

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_payment_raises_409_for_invalid_status(self, mock_utcnow):
        """Raises 409 when applying payment to draft/paid/void invoice."""
        es = _make_es_service()

        for invalid_status in [
            InvoiceStatus.DRAFT.value,
            InvoiceStatus.PAID.value,
            InvoiceStatus.VOID.value,
        ]:
            invoice = _make_invoice_doc(status=invalid_status)
            es.search_documents = AsyncMock(
                return_value=_es_search_response([invoice])
            )

            service = InvoiceService(es)

            with pytest.raises(AppException) as exc_info:
                await service.apply_payment(
                    tenant_id=_TENANT_ID,
                    invoice_id=_INVOICE_ID,
                    amount_cents=10000,
                    actor="system",
                )

            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_apply_payment_rejects_non_positive_amount(self, mock_utcnow):
        """Raises validation error for zero or negative amount."""
        es = _make_es_service()
        service = InvoiceService(es)

        with pytest.raises(AppException) as exc_info:
            await service.apply_payment(
                tenant_id=_TENANT_ID,
                invoice_id=_INVOICE_ID,
                amount_cents=0,
                actor="system",
            )

        assert exc_info.value.status_code == 400



# ---------------------------------------------------------------------------
# Tests: mark_overdue
# ---------------------------------------------------------------------------


class TestMarkOverdue:
    """Tests for InvoiceService.mark_overdue."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_mark_overdue_from_open(self, mock_utcnow):
        """Transitions open invoice to overdue."""
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.OPEN.value)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_agg_response({"max_seq": {"value": 2}}),  # next seq
            ]
        )

        service = InvoiceService(es)

        result = await service.mark_overdue(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="system",
        )

        assert result["status"] == InvoiceStatus.OVERDUE.value

        # Event was written
        event_call = es.index_document.call_args
        event_doc = event_call[0][2]
        assert event_doc["event_type"] == InvoiceEventType.OVERDUE_MARKED.value
        assert event_doc["payload"]["previous_status"] == "open"

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_mark_overdue_from_partial(self, mock_utcnow):
        """Transitions partial invoice to overdue."""
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.PARTIAL.value)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_agg_response({"max_seq": {"value": 3}}),  # next seq
            ]
        )

        service = InvoiceService(es)

        result = await service.mark_overdue(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="system",
        )

        assert result["status"] == InvoiceStatus.OVERDUE.value

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_mark_overdue_idempotent_when_already_overdue(self, mock_utcnow):
        """Returns existing invoice when already overdue (idempotent)."""
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.OVERDUE.value)

        es.search_documents = AsyncMock(
            return_value=_es_search_response([invoice])
        )

        service = InvoiceService(es)

        result = await service.mark_overdue(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            actor="system",
        )

        assert result["status"] == InvoiceStatus.OVERDUE.value

        # No new event or projection update
        es.index_document.assert_not_called()
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_mark_overdue_raises_409_for_invalid_status(self, mock_utcnow):
        """Raises 409 when marking paid/void/draft invoice as overdue."""
        es = _make_es_service()

        for invalid_status in [
            InvoiceStatus.DRAFT.value,
            InvoiceStatus.PAID.value,
            InvoiceStatus.VOID.value,
        ]:
            invoice = _make_invoice_doc(status=invalid_status)
            es.search_documents = AsyncMock(
                return_value=_es_search_response([invoice])
            )

            service = InvoiceService(es)

            with pytest.raises(AppException) as exc_info:
                await service.mark_overdue(
                    tenant_id=_TENANT_ID,
                    invoice_id=_INVOICE_ID,
                    actor="system",
                )

            assert exc_info.value.status_code == 409



# ---------------------------------------------------------------------------
# Tests: void
# ---------------------------------------------------------------------------


class TestVoid:
    """Tests for InvoiceService.void."""

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_void_no_payments_direct_void(self, mock_utcnow):
        """Voids an invoice with no payments directly."""
        es = _make_es_service()
        invoice = _make_invoice_doc(
            status=InvoiceStatus.OPEN.value,
            amount_paid_cents=0,
        )

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_agg_response({"max_seq": {"value": 2}}),  # next seq for void event
            ]
        )

        service = InvoiceService(es)

        result = await service.void(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            reason="Customer cancelled order",
            actor="admin_user",
            force=False,
        )

        assert result["status"] == InvoiceStatus.VOID.value
        assert result["void_reason"] == "Customer cancelled order"
        assert result["voided_at"] is not None

        # Event was written
        event_call = es.index_document.call_args
        event_doc = event_call[0][2]
        assert event_doc["event_type"] == InvoiceEventType.VOIDED.value
        assert event_doc["payload"]["reason"] == "Customer cancelled order"
        assert event_doc["payload"]["force"] is False

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_void_with_payments_force_false_raises_409(self, mock_utcnow):
        """Raises 409 when voiding invoice with payments and force=false."""
        es = _make_es_service()
        invoice = _make_invoice_doc(
            status=InvoiceStatus.PARTIAL.value,
            amount_paid_cents=50000,
            remaining_cents=145000,
        )

        es.search_documents = AsyncMock(
            return_value=_es_search_response([invoice])
        )

        service = InvoiceService(es)

        with pytest.raises(AppException) as exc_info:
            await service.void(
                tenant_id=_TENANT_ID,
                invoice_id=_INVOICE_ID,
                reason="Want to void",
                actor="admin_user",
                force=False,
            )

        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_void_with_payments_force_true_cascading_reversal(self, mock_utcnow):
        """Force void reverses all applied payments with source=void_cascade."""
        es = _make_es_service()
        invoice = _make_invoice_doc(
            status=InvoiceStatus.PARTIAL.value,
            amount_paid_cents=75000,
            remaining_cents=120000,
        )

        # Applied payments for this invoice
        payment_1 = {
            "payment_id": "pay_aaa",
            "invoice_id": _INVOICE_ID,
            "tenant_id": _TENANT_ID,
            "amount_cents": 50000,
            "source": "stripe",
            "method": "card",
            "status": "applied",
        }
        payment_2 = {
            "payment_id": "pay_bbb",
            "invoice_id": _INVOICE_ID,
            "tenant_id": _TENANT_ID,
            "amount_cents": 25000,
            "source": "manual",
            "method": "check",
            "status": "applied",
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                # _reverse_applied_payments: find applied payments
                _es_search_response([payment_1, payment_2]),
                # seq for payment_reversed event 1
                _es_agg_response({"max_seq": {"value": 2}}),
                # seq for payment_reversed event 2
                _es_agg_response({"max_seq": {"value": 3}}),
                # seq for void event
                _es_agg_response({"max_seq": {"value": 4}}),
            ]
        )

        service = InvoiceService(es)

        result = await service.void(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            reason="Duplicate invoice",
            actor="billing_admin",
            force=True,
        )

        assert result["status"] == InvoiceStatus.VOID.value
        assert result["amount_paid_cents"] == 0

        # Payments were reversed (update_document called for each payment + projection)
        # Each payment gets an update_document call to mark as reversed
        update_calls = es.update_document.call_args_list
        reversed_payment_ids = []
        for call in update_calls:
            args = call[0]
            if args[0] == "payments_current" and args[2].get("status") == "reversed":
                reversed_payment_ids.append(args[1])

        assert "pay_aaa" in reversed_payment_ids
        assert "pay_bbb" in reversed_payment_ids

        # Void event payload includes reversed payment info
        # Find the void event among index_document calls
        void_events = [
            call[0][2]
            for call in es.index_document.call_args_list
            if call[0][2].get("event_type") == InvoiceEventType.VOIDED.value
        ]
        assert len(void_events) == 1
        void_payload = void_events[0]["payload"]
        assert void_payload["force"] is True
        assert void_payload["reversed_payment_count"] == 2
        assert "pay_aaa" in void_payload["reversed_payment_ids"]
        assert "pay_bbb" in void_payload["reversed_payment_ids"]

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_void_already_voided_raises_409(self, mock_utcnow):
        """Raises 409 when invoice is already voided."""
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.VOID.value)

        es.search_documents = AsyncMock(
            return_value=_es_search_response([invoice])
        )

        service = InvoiceService(es)

        with pytest.raises(AppException) as exc_info:
            await service.void(
                tenant_id=_TENANT_ID,
                invoice_id=_INVOICE_ID,
                reason="Try again",
                actor="admin",
            )

        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_void_cascading_reversal_writes_payment_reversed_events(
        self, mock_utcnow
    ):
        """Force void writes payment_reversed events with source=void_cascade."""
        es = _make_es_service()
        invoice = _make_invoice_doc(
            status=InvoiceStatus.OPEN.value,
            amount_paid_cents=30000,
            remaining_cents=165000,
        )

        payment = {
            "payment_id": "pay_ccc",
            "invoice_id": _INVOICE_ID,
            "tenant_id": _TENANT_ID,
            "amount_cents": 30000,
            "source": "stripe",
            "method": "ach",
            "status": "applied",
        }

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_search_response([payment]),  # find applied payments
                _es_agg_response({"max_seq": {"value": 2}}),  # seq for reversal event
                _es_agg_response({"max_seq": {"value": 3}}),  # seq for void event
            ]
        )

        service = InvoiceService(es)

        await service.void(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
            reason="Force void test",
            actor="admin",
            force=True,
        )

        # Find payment_reversed events
        reversal_events = [
            call[0][2]
            for call in es.index_document.call_args_list
            if call[0][2].get("event_type") == InvoiceEventType.PAYMENT_REVERSED.value
        ]
        assert len(reversal_events) == 1
        assert reversal_events[0]["payload"]["payment_id"] == "pay_ccc"
        assert reversal_events[0]["payload"]["source"] == "void_cascade"
        assert reversal_events[0]["payload"]["amount_cents"] == 30000



# ---------------------------------------------------------------------------
# Tests: Event sourcing — projection rebuild
# ---------------------------------------------------------------------------


class TestProjectionRebuild:
    """Tests for event-sourced projection rebuild.

    Verifies that the projection (invoices_current) can be reconstructed
    from the event log alone (Constraint C7).
    """

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_projection_rebuild_from_events(self, mock_utcnow):
        """Events can reconstruct the invoice projection state.

        Simulates: create → finalize → apply_payment → mark_overdue
        Then verifies the final projection state matches what the events
        describe.
        """
        es = _make_es_service()
        idemp = _make_idempotency_service(is_duplicate=False)

        # Track all indexed documents to simulate event log
        indexed_events: List[Dict[str, Any]] = []
        projection_state: Dict[str, Any] = {}

        async def mock_index(index, doc_id, doc):
            if "event_type" in doc:
                indexed_events.append(doc)
            else:
                projection_state.update(doc)

        async def mock_update(index, doc_id, partial):
            projection_state.update(partial)

        es.index_document = AsyncMock(side_effect=mock_index)
        es.update_document = AsyncMock(side_effect=mock_update)

        # Step 1: generate_from_order
        es.search_documents = AsyncMock(
            return_value=_es_agg_response({"max_seq": {"value": None}})
        )
        service = InvoiceService(es, idemp)

        created = await service.generate_from_order(
            tenant_id=_TENANT_ID,
            order_id=_ORDER_ID,
            customer_id=_CUSTOMER_ID,
            account_id=_ACCOUNT_ID,
            line_items=_SAMPLE_LINE_ITEMS,
            tax_cents=5000,
            net_terms_days=30,
        )
        invoice_id = created["invoice_id"]

        # Step 2: finalize_draft
        draft_invoice = {**projection_state}
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([draft_invoice]),  # get invoice
                _es_search_response([]),  # get account for _drain_credit_balance (none found)
                _es_search_response([draft_invoice]),  # re-get invoice after drain
                _es_agg_response({"max_seq": {"value": 1}}),  # next seq for finalized event
            ]
        )

        await service.finalize_draft(
            tenant_id=_TENANT_ID,
            invoice_id=invoice_id,
        )

        # Step 3: apply_payment (partial)
        open_invoice = {**projection_state}
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([open_invoice]),  # get
                _es_agg_response({"max_seq": {"value": 2}}),  # next seq
            ]
        )

        await service.apply_payment(
            tenant_id=_TENANT_ID,
            invoice_id=invoice_id,
            amount_cents=50000,
            payment_id="pay_rebuild_1",
        )

        # Step 4: mark_overdue
        partial_invoice = {**projection_state}
        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([partial_invoice]),  # get
                _es_agg_response({"max_seq": {"value": 3}}),  # next seq
            ]
        )

        await service.mark_overdue(
            tenant_id=_TENANT_ID,
            invoice_id=invoice_id,
        )

        # Verify: events tell the full story
        assert len(indexed_events) == 4
        assert indexed_events[0]["event_type"] == InvoiceEventType.CREATED.value
        assert indexed_events[1]["event_type"] == InvoiceEventType.FINALIZED.value
        assert indexed_events[2]["event_type"] == InvoiceEventType.PAYMENT_APPLIED.value
        assert indexed_events[3]["event_type"] == InvoiceEventType.OVERDUE_MARKED.value

        # Verify: projection matches expected final state
        assert projection_state["status"] == InvoiceStatus.OVERDUE.value
        assert projection_state["amount_paid_cents"] == 50000
        assert projection_state["remaining_cents"] == 150000  # 200000 - 50000
        assert projection_state["total_cents"] == 200000

        # Verify: sequence numbers are monotonically increasing
        seqs = [e["sequence_number"] for e in indexed_events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)  # all unique

    @pytest.mark.asyncio
    @patch("commerce.services.invoice_service.utcnow", return_value=_FIXED_NOW)
    async def test_every_state_change_writes_event_first(self, mock_utcnow):
        """Every state-changing method writes event before projection (C7).

        Verifies the ordering invariant: for each operation, the event
        index_document call precedes the projection update_document call.
        """
        es = _make_es_service()
        invoice = _make_invoice_doc(status=InvoiceStatus.OPEN.value)

        es.search_documents = AsyncMock(
            side_effect=[
                _es_search_response([invoice]),  # get invoice
                _es_agg_response({"max_seq": {"value": 2}}),  # next seq
            ]
        )

        call_log: List[str] = []

        async def track_index(index, doc_id, doc):
            if "event_type" in doc:
                call_log.append("event_write")

        async def track_update(index, doc_id, partial):
            call_log.append("projection_update")

        es.index_document = AsyncMock(side_effect=track_index)
        es.update_document = AsyncMock(side_effect=track_update)

        service = InvoiceService(es)

        await service.mark_overdue(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
        )

        # Event write must come before projection update
        assert call_log.index("event_write") < call_log.index("projection_update")



# ---------------------------------------------------------------------------
# Tests: get and list
# ---------------------------------------------------------------------------


class TestGetAndList:
    """Tests for InvoiceService.get and list."""

    @pytest.mark.asyncio
    async def test_get_returns_invoice_by_id(self):
        """Returns invoice document when found."""
        es = _make_es_service()
        invoice = _make_invoice_doc()

        es.search_documents = AsyncMock(
            return_value=_es_search_response([invoice])
        )

        service = InvoiceService(es)

        result = await service.get(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
        )

        assert result["invoice_id"] == _INVOICE_ID
        assert result["tenant_id"] == _TENANT_ID

    @pytest.mark.asyncio
    async def test_get_raises_404_when_not_found(self):
        """Raises 404 when invoice doesn't exist."""
        es = _make_es_service()
        es.search_documents = AsyncMock(
            return_value=_es_search_response([])
        )

        service = InvoiceService(es)

        with pytest.raises(AppException) as exc_info:
            await service.get(
                tenant_id=_TENANT_ID,
                invoice_id="inv_nonexistent",
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_events_returns_ordered_event_log(self):
        """Returns events ordered by sequence_number."""
        es = _make_es_service()

        events = [
            {
                "event_id": "ievt_1",
                "invoice_id": _INVOICE_ID,
                "tenant_id": _TENANT_ID,
                "event_type": "created",
                "sequence_number": 1,
                "occurred_at": "2026-06-15T10:00:00",
                "actor": "system",
                "payload": {},
            },
            {
                "event_id": "ievt_2",
                "invoice_id": _INVOICE_ID,
                "tenant_id": _TENANT_ID,
                "event_type": "finalized",
                "sequence_number": 2,
                "occurred_at": "2026-06-15T10:05:00",
                "actor": "admin",
                "payload": {},
            },
        ]

        es.search_documents = AsyncMock(
            return_value=_es_search_response(events)
        )

        service = InvoiceService(es)

        result = await service.get_events(
            tenant_id=_TENANT_ID,
            invoice_id=_INVOICE_ID,
        )

        assert len(result) == 2
        assert result[0]["sequence_number"] == 1
        assert result[1]["sequence_number"] == 2
        assert result[0]["event_type"] == "created"
        assert result[1]["event_type"] == "finalized"
