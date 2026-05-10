"""
Unit tests for :class:`compliance.hooks.bol_signed_subscriber.BOLSignedSubscriber`.

Validates Task 14.7 of the fuel-compliance-backbone spec:

* Requirement 12.8 — WHEN a signed BOL is generated after delivery, THE
  Notification_Template_Service SHALL send the e_bol_delivery notification
  to the customer's designated BOL recipient email.

Tests verify:
- Notification is triggered with correct event_type and event_data
- PDF attachment reference (file_ref) is included in event_data
- Template placeholders (customer_name, load_number, product, gross_gallons,
  net_gallons, terminal, driver) are correctly extracted
- Deduplication: same bol_id does not fire twice
- Missing customer_id skips notification gracefully
- Missing notification_service skips gracefully
- Notification failures are non-blocking (logged, not raised)
"""

import pytest

from compliance.hooks.bol_signed_subscriber import BOLSignedSubscriber


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeNotificationService:
    """Captures notify_event calls for assertion."""

    def __init__(self, *, raises: Exception | None = None):
        self.calls: list[dict] = []
        self._raises = raises

    async def notify_event(
        self, event_type: str, event_data: dict, tenant_id: str
    ) -> list[dict]:
        if self._raises:
            raise self._raises
        call = {
            "event_type": event_type,
            "event_data": event_data,
            "tenant_id": tenant_id,
        }
        self.calls.append(call)
        return [{"notification_id": "notif-1"}]


def _bol_document(
    *,
    bol_id: str = "bol-abc-123",
    tenant_id: str = "tenant-a",
    pod_id: str = "pod-xyz",
    order_id: str = "order-001",
    file_ref: str = "tenants/tenant-a/bol/2025/01/15/signed.pdf",
    customer_id: str = "cust-42",
    fields: dict | None = None,
) -> dict:
    """Build a minimal BOL document dict for testing."""
    default_fields = {
        "bol_number": "BOL-TENA-20250115143000-pod",
        "product_code": "DIESEL_2",
        "product_name": "Diesel #2",
        "gross_gallons": 2500.0,
        "net_gallons": 2480.5,
        "origin_name": "Houston Terminal",
        "terminal_name": "Houston Terminal",
        "destination_name": "Acme Corp",
        "driver_name": "John Smith",
        "driver_id": "drv-001",
        "customer_id": customer_id,
        "customer_name": "Acme Corp",
    }
    if fields is not None:
        default_fields.update(fields)

    return {
        "bol_id": bol_id,
        "tenant_id": tenant_id,
        "pod_id": pod_id,
        "order_id": order_id,
        "file_ref": file_ref,
        "hash": "deadbeef" * 8,
        "status": "generated",
        "generated_at": "2025-01-15T14:30:00+00:00",
        "fields": default_fields,
        "customer_id": customer_id,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBOLSignedSubscriber:
    """Tests for BOLSignedSubscriber.__call__."""

    @pytest.mark.asyncio
    async def test_fires_e_bol_delivery_notification(self):
        """Happy path: fires e_bol_delivery with correct event_type."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        await subscriber(_bol_document())

        assert len(ns.calls) == 1
        assert ns.calls[0]["event_type"] == "e_bol_delivery"
        assert ns.calls[0]["tenant_id"] == "tenant-a"

    @pytest.mark.asyncio
    async def test_event_data_contains_template_placeholders(self):
        """Event data includes all e_bol_delivery template placeholders."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        await subscriber(_bol_document())

        event_data = ns.calls[0]["event_data"]
        assert event_data["customer_name"] == "Acme Corp"
        assert event_data["load_number"] == "BOL-TENA-20250115143000-pod"
        assert event_data["product"] == "Diesel #2"
        assert event_data["gross_gallons"] == "2500.0"
        assert event_data["net_gallons"] == "2480.5"
        assert event_data["terminal"] == "Houston Terminal"
        assert event_data["driver"] == "John Smith"

    @pytest.mark.asyncio
    async def test_event_data_contains_attachment_ref(self):
        """Event data includes the PDF attachment reference (file_ref)."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        await subscriber(_bol_document(
            file_ref="tenants/tenant-a/bol/2025/01/15/signed.pdf"
        ))

        event_data = ns.calls[0]["event_data"]
        assert event_data["attachment_ref"] == "tenants/tenant-a/bol/2025/01/15/signed.pdf"
        assert event_data["attachment_type"] == "signed_bol_pdf"

    @pytest.mark.asyncio
    async def test_event_data_contains_customer_id(self):
        """Event data includes customer_id for preference resolution."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        await subscriber(_bol_document(customer_id="cust-99"))

        event_data = ns.calls[0]["event_data"]
        assert event_data["customer_id"] == "cust-99"

    @pytest.mark.asyncio
    async def test_deduplication_same_bol_id(self):
        """Same bol_id does not fire notification twice."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        doc = _bol_document(bol_id="bol-dup")
        await subscriber(doc)
        await subscriber(doc)

        assert len(ns.calls) == 1

    @pytest.mark.asyncio
    async def test_different_bol_ids_fire_separately(self):
        """Different bol_ids each fire their own notification."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        await subscriber(_bol_document(bol_id="bol-1"))
        await subscriber(_bol_document(bol_id="bol-2"))

        assert len(ns.calls) == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_customer_id(self):
        """Skips notification when customer_id is missing."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        doc = _bol_document(customer_id="")
        # Also clear customer_id from fields
        doc["fields"]["customer_id"] = ""
        doc["customer_id"] = ""
        await subscriber(doc)

        assert len(ns.calls) == 0

    @pytest.mark.asyncio
    async def test_skips_when_no_notification_service(self):
        """Skips gracefully when notification_service is None."""
        subscriber = BOLSignedSubscriber(notification_service=None)

        # Should not raise
        await subscriber(_bol_document())

    @pytest.mark.asyncio
    async def test_notification_failure_is_non_blocking(self):
        """Notification failures are logged but do not raise."""
        ns = _FakeNotificationService(raises=RuntimeError("SendGrid down"))
        subscriber = BOLSignedSubscriber(notification_service=ns)

        # Should not raise
        await subscriber(_bol_document())

        # Should NOT be marked as notified (dedup set not updated on failure)
        assert "bol-abc-123" not in subscriber._notified_bols

    @pytest.mark.asyncio
    async def test_product_code_fallback_to_name_mapping(self):
        """When product_name is missing, falls back to product_code mapping."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        doc = _bol_document(fields={
            "product_code": "PROPANE",
            "product_name": "",
            "bol_number": "BOL-001",
            "gross_gallons": 1000.0,
            "net_gallons": 990.0,
            "terminal_name": "Dallas Terminal",
            "driver_name": "Jane Doe",
            "customer_id": "cust-1",
            "customer_name": "Test Customer",
        })
        await subscriber(doc)

        event_data = ns.calls[0]["event_data"]
        assert event_data["product"] == "Propane"

    @pytest.mark.asyncio
    async def test_bol_id_included_in_event_data(self):
        """Event data includes bol_id for tracking."""
        ns = _FakeNotificationService()
        subscriber = BOLSignedSubscriber(notification_service=ns)

        await subscriber(_bol_document(bol_id="bol-track-123"))

        event_data = ns.calls[0]["event_data"]
        assert event_data["bol_id"] == "bol-track-123"
