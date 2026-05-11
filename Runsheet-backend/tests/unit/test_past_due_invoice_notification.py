"""
Unit tests for the past_due_invoice notification wiring in InvoiceService.

Validates: Requirement 12.6 — WHEN an invoice transitions to overdue status,
THE Notification_Template_Service SHALL send the past_due_invoice notification
to the account billing contact.

Tests cover:
- Notification fires when invoice transitions to overdue
- Notification does NOT fire when notification_service is not wired
- Notification does NOT fire for already-overdue invoices (idempotent)
- Deduplication: only one notification per invoice
- Notification failure does not break the overdue transition
- Event data payload contains all required template placeholders
- days_past_due is computed correctly from due_date
- amount_due_dollars is computed correctly from remaining_cents
- Correct tenant_id is passed to notify_event
"""

import pytest
from datetime import date, timedelta, datetime, timezone
from unittest.mock import AsyncMock

from commerce.models.invoice import InvoiceStatus
from commerce.services.invoice_service import InvoiceService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_invoice_service(notification_service=None):
    """Create an InvoiceService with mocked dependencies."""
    es_service = AsyncMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es_service.index_document = AsyncMock()
    es_service.update_document = AsyncMock()

    service = InvoiceService(
        es_service=es_service,
        idempotency_service=None,
        account_service=None,
        payment_service=None,
    )

    if notification_service is not None:
        service.set_notification_service(notification_service)

    return service


def _make_invoice_doc(
    invoice_id="inv_test_001",
    tenant_id="tenant-1",
    customer_id="customer-1",
    account_id="acct_001",
    invoice_number="INV-2025-0042",
    status="open",
    remaining_cents=15000,
    due_date=None,
):
    """Create a mock invoice document."""
    if due_date is None:
        due_date = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()

    return {
        "invoice_id": invoice_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": account_id,
        "invoice_number": invoice_number,
        "status": status,
        "total_cents": 20000,
        "remaining_cents": remaining_cents,
        "due_date": due_date,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPastDueInvoiceNotification:
    """Tests for the past_due_invoice notification wiring."""

    @pytest.mark.asyncio
    async def test_fires_when_invoice_transitions_to_overdue(self):
        """Notification fires when mark_overdue transitions an invoice."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(status="open")
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_001",
        )

        notification_service.notify_event.assert_called_once()
        call_kwargs = notification_service.notify_event.call_args[1]
        assert call_kwargs["event_type"] == "past_due_invoice"
        assert call_kwargs["tenant_id"] == "tenant-1"

    @pytest.mark.asyncio
    async def test_does_not_fire_when_notification_service_not_wired(self):
        """No error when notification_service is None."""
        service = _make_invoice_service(notification_service=None)

        invoice_doc = _make_invoice_doc(status="open")
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        result = await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_001",
        )
        assert result["status"] == InvoiceStatus.OVERDUE.value

    @pytest.mark.asyncio
    async def test_does_not_fire_for_already_overdue_invoice(self):
        """Notification does NOT fire for already-overdue invoices."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(status="overdue")
        service.get = AsyncMock(return_value=invoice_doc)

        result = await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_001",
        )

        notification_service.notify_event.assert_not_called()
        assert result["status"] == "overdue"

    @pytest.mark.asyncio
    async def test_deduplication_only_fires_once_per_invoice(self):
        """Only one notification per invoice."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(status="open")
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_001",
        )
        assert notification_service.notify_event.call_count == 1

        # Call _fire directly again — should be deduplicated
        invoice_doc_overdue = _make_invoice_doc(
            status="overdue", invoice_id="inv_test_001"
        )
        await service._fire_past_due_invoice_notification(
            tenant_id="tenant-1",
            invoice=invoice_doc_overdue,
        )
        assert notification_service.notify_event.call_count == 1

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_break_overdue(self):
        """Notification service failure is swallowed gracefully."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(
            side_effect=RuntimeError("Notification service down")
        )
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(status="open")
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        result = await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_001",
        )
        assert result["status"] == InvoiceStatus.OVERDUE.value

    @pytest.mark.asyncio
    async def test_event_data_contains_required_placeholders(self):
        """Event data payload contains all template placeholders."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(
            invoice_id="inv_test_002",
            customer_id="cust-abc",
            invoice_number="INV-2025-0099",
            remaining_cents=25050,
            due_date=(datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat(),
            status="open",
        )
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_002",
        )

        call_kwargs = notification_service.notify_event.call_args[1]
        event_data = call_kwargs["event_data"]

        assert "customer_id" in event_data
        assert "customer_name" in event_data
        assert "invoice_number" in event_data
        assert "amount_due_dollars" in event_data
        assert "days_past_due" in event_data
        assert "payment_link" in event_data

        assert event_data["customer_id"] == "cust-abc"
        assert event_data["invoice_number"] == "INV-2025-0099"
        assert event_data["amount_due_dollars"] == "250.50"
        assert event_data["days_past_due"] == 7
        assert "inv_test_002" in event_data["payment_link"]

    @pytest.mark.asyncio
    async def test_days_past_due_computed_correctly(self):
        """days_past_due is computed from due_date relative to today."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(
            due_date=(datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat(),
            status="open",
        )
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_001",
        )

        call_kwargs = notification_service.notify_event.call_args[1]
        event_data = call_kwargs["event_data"]
        assert event_data["days_past_due"] == 10

    @pytest.mark.asyncio
    async def test_amount_due_dollars_from_remaining_cents(self):
        """amount_due_dollars is remaining_cents / 100 formatted."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(remaining_cents=9999, status="open")
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_001",
        )

        call_kwargs = notification_service.notify_event.call_args[1]
        event_data = call_kwargs["event_data"]
        assert event_data["amount_due_dollars"] == "99.99"

    @pytest.mark.asyncio
    async def test_tenant_id_passed_to_notify_event(self):
        """The correct tenant_id is passed to notify_event."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(tenant_id="my-tenant", status="open")
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        await service.mark_overdue(
            tenant_id="my-tenant",
            invoice_id="inv_test_001",
        )

        call_kwargs = notification_service.notify_event.call_args[1]
        assert call_kwargs["tenant_id"] == "my-tenant"

    @pytest.mark.asyncio
    async def test_missing_due_date_defaults_to_zero_days(self):
        """When due_date is missing, days_past_due defaults to 0."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(status="open")
        invoice_doc["due_date"] = None
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_test_001",
        )

        call_kwargs = notification_service.notify_event.call_args[1]
        event_data = call_kwargs["event_data"]
        assert event_data["days_past_due"] == 0

    @pytest.mark.asyncio
    async def test_missing_invoice_number_falls_back_to_id(self):
        """When invoice_number is None, invoice_id is used."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        invoice_doc = _make_invoice_doc(
            invoice_id="inv_fallback_123",
            invoice_number=None,
            status="open",
        )
        service.get = AsyncMock(return_value=invoice_doc)
        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        await service.mark_overdue(
            tenant_id="tenant-1",
            invoice_id="inv_fallback_123",
        )

        call_kwargs = notification_service.notify_event.call_args[1]
        event_data = call_kwargs["event_data"]
        assert event_data["invoice_number"] == "inv_fallback_123"

    @pytest.mark.asyncio
    async def test_multiple_invoices_each_get_notification(self):
        """Different invoices each get their own notification."""
        notification_service = AsyncMock()
        notification_service.notify_event = AsyncMock(return_value=[])
        service = _make_invoice_service(notification_service=notification_service)

        service._write_invoice_event = AsyncMock(
            return_value={"event_id": "evt_1", "sequence_number": 2}
        )
        service._update_projection = AsyncMock()
        service._broadcast_invoice_ws = AsyncMock()

        invoice_doc_1 = _make_invoice_doc(invoice_id="inv_001", status="open")
        service.get = AsyncMock(return_value=invoice_doc_1)
        await service.mark_overdue(tenant_id="tenant-1", invoice_id="inv_001")

        invoice_doc_2 = _make_invoice_doc(invoice_id="inv_002", status="open")
        service.get = AsyncMock(return_value=invoice_doc_2)
        await service.mark_overdue(tenant_id="tenant-1", invoice_id="inv_002")

        assert notification_service.notify_event.call_count == 2
