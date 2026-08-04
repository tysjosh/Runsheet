"""Tests for recovery after POD persistence wins but the order write loses."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from driver.services.pod_transition_reconciler import PODTransitionReconciler


def _pod(**overrides) -> dict:
    value = {
        "pod_id": "pod-1",
        "tenant_id": "tenant-1",
        "order_id": "order-1",
        "driver_id": "driver-1",
        "delivered_gallons": 487.5,
        "delivered_gallons_source": "manual",
        "delivered_at": "2026-07-29T14:30:00Z",
        "timestamp": "2026-07-29T14:30:00Z",
        "recipient_name": "Sam Receiver",
        "signature_ref": "tenants/tenant-1/signature/sig.png",
        "photo_refs": ["tenants/tenant-1/photo/drop.jpg"],
        "meter_ticket_ref": "tenants/tenant-1/meter_ticket/ticket.jpg",
        "pod_hash": "a" * 64,
        "geotag": {"lat": 39.7684, "lon": -86.1581},
        "otp_verified": True,
        "location_mismatch": False,
        "refused_delivery": False,
        "pod_status_transition": "pending",
    }
    value.update(overrides)
    return value


def _order(**overrides) -> dict:
    value = {
        "order_id": "order-1",
        "tenant_id": "tenant-1",
        "status": "in_transit",
        "intake_metadata": {
            "source_system": "legacy-erp",
            "source_record_id": "SO-1042",
        },
    }
    value.update(overrides)
    return value


class FakeES:
    def __init__(self, pods: list[dict], bol: dict | None = None):
        self.pods = pods
        self.bol = bol
        self.updates: list[tuple[str, str, dict]] = []
        self.queries: list[tuple[str, dict]] = []

    async def search_documents(self, index, query, size):
        self.queries.append((index, query))
        if index == "proof_of_delivery":
            return {
                "hits": {
                    "hits": [
                        {"_id": pod["pod_id"], "_source": pod}
                        for pod in self.pods
                    ]
                }
            }
        if index == "bill_of_lading" and self.bol:
            return {"hits": {"hits": [{"_source": self.bol}]}}
        return {"hits": {"hits": []}}

    async def update_document(self, index, doc_id, fields):
        self.updates.append((index, doc_id, dict(fields)))


class FakeOrders:
    def __init__(self, order):
        self.order = order

    async def get(self, tenant_id, order_id):
        if self.order is None:
            return None
        return dict(self.order)


def _order_service() -> MagicMock:
    service = MagicMock()

    async def apply(**kwargs):
        order = kwargs["order"]
        order["status"] = kwargs["new_status"]
        return order

    async def reconcile(**kwargs):
        order = kwargs["order"]
        order["delivery_result"] = kwargs["delivery_result"]
        return order

    service.apply_status_transition = AsyncMock(side_effect=apply)
    service.reconcile_delivery_result = AsyncMock(side_effect=reconcile)
    return service


@pytest.mark.asyncio
async def test_repairs_pending_delivery_with_actual_gallons_and_bol():
    es = FakeES(
        [_pod()],
        bol={"bol_id": "bol-1", "file_ref": "tenants/tenant-1/bol/bol.pdf"},
    )
    orders = FakeOrders(_order())
    service = _order_service()
    reconciler = PODTransitionReconciler(
        es_service=es,
        order_repository=orders,
        order_service=service,
    )

    result = await reconciler.repair_pending()

    assert result == {
        "examined": 1,
        "repaired": 1,
        "failed": 0,
        "skipped_locked": 0,
    }
    kwargs = service.apply_status_transition.call_args.kwargs
    assert kwargs["new_status"] == "delivered"
    delivery = kwargs["order"]["delivery_result"]
    assert delivery["actual_gallons"] == 487.5
    assert delivery["bol_id"] == "bol-1"
    assert delivery["source_record_id"] == "SO-1042"
    assert es.updates[-1][2] == {
        "pod_status_transition": "completed",
        "pod_status_transition_error": None,
    }


@pytest.mark.asyncio
async def test_already_delivered_order_gets_missing_snapshot_without_x_to_x():
    es = FakeES([_pod()])
    service = _order_service()
    reconciler = PODTransitionReconciler(
        es_service=es,
        order_repository=FakeOrders(_order(status="delivered")),
        order_service=service,
    )

    result = await reconciler.repair_pending()

    assert result["repaired"] == 1
    service.apply_status_transition.assert_not_awaited()
    service.reconcile_delivery_result.assert_awaited_once()
    snapshot = service.reconcile_delivery_result.call_args.kwargs[
        "delivery_result"
    ]
    assert snapshot["pod_id"] == "pod-1"


@pytest.mark.asyncio
async def test_refusal_repairs_to_failed_without_positive_delivery_snapshot():
    es = FakeES(
        [
            _pod(
                delivered_gallons=0,
                delivered_gallons_source="refused",
                refused_delivery=True,
                refusal_reason_code="unsafe_site",
            )
        ]
    )
    service = _order_service()
    reconciler = PODTransitionReconciler(
        es_service=es,
        order_repository=FakeOrders(_order()),
        order_service=service,
    )

    await reconciler.repair_pending()

    kwargs = service.apply_status_transition.call_args.kwargs
    assert kwargs["new_status"] == "failed"
    assert kwargs["reason"] == "unsafe_site"
    assert "delivery_result" not in kwargs["order"]


@pytest.mark.asyncio
async def test_missing_order_stays_pending_with_diagnostic():
    es = FakeES([_pod()])
    reconciler = PODTransitionReconciler(
        es_service=es,
        order_repository=FakeOrders(None),
        order_service=_order_service(),
    )

    result = await reconciler.repair_pending()

    assert result["failed"] == 1
    assert es.updates[-1][2]["pod_status_transition"] == "pending"
    assert "LookupError" in es.updates[-1][2]["pod_status_transition_error"]
