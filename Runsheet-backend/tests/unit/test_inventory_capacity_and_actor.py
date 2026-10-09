"""B3: inventory quantity can't be pushed above max_capacity.
B4: the stock-adjustment audit actor is the user, not the tenant.

Before, create/PATCH/adjust stored any quantity regardless of max_capacity,
and adjust recorded ``actor_id = tenant.tenant_id``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import inventory.api.endpoints as endpoints
from errors.exceptions import AppException
from errors.handlers import register_exception_handlers
from inventory.models import (
    CreateInventoryItem,
    InventoryItem,
    StockAdjustment,
    StockAdjustmentResult,
    UpdateInventoryItem,
)
from inventory.service import InventoryService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


def _item(quantity: int, max_capacity: int) -> InventoryItem:
    return InventoryItem(
        item_id="INV_1",
        name="Tire",
        category="tires",
        quantity=quantity,
        unit="units",
        min_threshold=5,
        max_capacity=max_capacity,
        location="Depot A",
        status="in_stock",
        tenant_id="tenant-A",
    )


class _Store:
    def __init__(self) -> None:
        self.updates: list[Dict[str, Any]] = []
        self.indexed: list[Dict[str, Any]] = []

    async def update_document(self, index, doc_id, partial):
        self.updates.append(dict(partial))
        return {"result": "updated"}

    async def index_document(self, index, doc_id, document):
        self.indexed.append(dict(document))
        return {"result": "created"}


def _service(existing: Optional[InventoryItem]) -> tuple[InventoryService, _Store]:
    store = _Store()
    service = InventoryService(store)

    async def _get_item(item_id, tenant_id):
        return existing

    service.get_item = _get_item  # type: ignore[method-assign]
    return service, store


def _create_body(quantity: int, max_capacity: int) -> dict:
    return {
        "name": "Tire",
        "category": "tires",
        "quantity": quantity,
        "unit": "units",
        "min_threshold": 5,
        "max_capacity": max_capacity,
        "location": "Depot A",
    }


# ---------------------------------------------------------------------------
# B3 create
# ---------------------------------------------------------------------------


def test_create_above_capacity_is_a_validation_error():
    with pytest.raises(ValidationError, match="cannot exceed max_capacity"):
        CreateInventoryItem(**_create_body(200, 50))


def test_create_at_capacity_is_allowed():
    assert CreateInventoryItem(**_create_body(50, 50)).quantity == 50


# ---------------------------------------------------------------------------
# B3 PATCH
# ---------------------------------------------------------------------------


async def test_patch_quantity_above_capacity_is_refused():
    service, store = _service(_item(10, 50))

    with pytest.raises(AppException) as exc:
        await service.update_item("INV_1", UpdateInventoryItem(quantity=60), "tenant-A")

    assert exc.value.error_code.value == "VALIDATION_ERROR"
    assert "max_capacity" in exc.value.message
    assert store.updates == []


async def test_patch_max_capacity_below_quantity_is_refused():
    service, store = _service(_item(40, 50))

    with pytest.raises(AppException):
        await service.update_item(
            "INV_1", UpdateInventoryItem(max_capacity=30), "tenant-A"
        )
    assert store.updates == []


async def test_patch_within_capacity_is_applied():
    service, store = _service(_item(10, 50))

    await service.update_item("INV_1", UpdateInventoryItem(quantity=50), "tenant-A")

    assert store.updates[0]["quantity"] == 50


async def test_patch_on_legacy_over_capacity_item_can_lower_quantity_or_rename():
    service, store = _service(_item(200, 50))

    await service.update_item("INV_1", UpdateInventoryItem(quantity=100), "tenant-A")
    await service.update_item("INV_1", UpdateInventoryItem(name="Tire 2"), "tenant-A")

    assert len(store.updates) == 2


# ---------------------------------------------------------------------------
# B3 adjust
# ---------------------------------------------------------------------------


async def test_restock_above_capacity_is_refused():
    service, store = _service(_item(10, 50))

    with pytest.raises(AppException) as exc:
        await service.adjust_stock(
            "INV_1",
            StockAdjustment(quantity_change=1000, reason="restock"),
            "tenant-A",
            "user-1",
        )

    assert exc.value.details["max_capacity"] == 50
    assert store.updates == []


async def test_consumption_on_legacy_over_capacity_item_succeeds():
    service, store = _service(_item(200, 50))

    result = await service.adjust_stock(
        "INV_1",
        StockAdjustment(quantity_change=-10, reason="used_for_maintenance"),
        "tenant-A",
        "user-1",
    )

    assert result.new_quantity == 190
    assert store.updates[0]["quantity"] == 190


# ---------------------------------------------------------------------------
# Endpoint level: B3 create is 422, adjust 400; B4 actor is the user
# ---------------------------------------------------------------------------


class _RecordingService:
    def __init__(self) -> None:
        self.adjust_kwargs: Dict[str, Any] = {}

    async def adjust_stock(self, **kwargs):
        self.adjust_kwargs = kwargs
        return StockAdjustmentResult(
            item_id=kwargs["item_id"],
            previous_quantity=1,
            new_quantity=2,
            previous_status="in_stock",
            new_status="in_stock",
            event_id="EVT_1",
        )

    async def create_item(self, **kwargs):  # pragma: no cover - 422 first
        raise AssertionError("over-capacity create must be refused before the service")


@pytest.fixture
def recording(monkeypatch):
    fake = _RecordingService()
    monkeypatch.setattr(endpoints, "_inventory_service", fake)
    return fake


@pytest.fixture
def client(recording):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(endpoints.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=["admin"],
    )
    return TestClient(app)


def test_create_over_capacity_is_422(client):
    response = client.post("/api/inventory/items", json=_create_body(200, 50))

    assert response.status_code == 422
    assert "max_capacity" in response.text


def test_adjust_records_the_user_as_actor(client, recording):
    response = client.post(
        "/api/inventory/items/INV_1/adjust",
        json={"quantity_change": 1, "reason": "restock"},
    )

    assert response.status_code == 200
    assert recording.adjust_kwargs["actor_id"] == "user-1"
    assert recording.adjust_kwargs["tenant_id"] == "tenant-A"
