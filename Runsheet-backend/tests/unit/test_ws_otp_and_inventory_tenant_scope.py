"""WS payloads never carry the delivery OTP, and inventory alerts stay in their tenant.

* ``/ws/orders`` sends the stored order document, and every dispatcher socket in
  the tenant receives it. The POD OTP (``pod_otp``) is the customer's
  proof-of-delivery code. REST and the driver API strip it (R5.26), so the
  socket must too, on every order event type.
* Inventory stock alerts went out with ``ConnectionManager.broadcast`` on the
  shared fleet socket, which reaches every connected client in all tenants.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

from fuel.order_repository import FuelOrderRepository
from fuel.services.order_service import OrderService
from fuel.websocket.orders_ws import NEVER_BROADCAST_FIELDS, OrdersWSManager
from inventory.service import InventoryService
from tests.unit._loading_plan_fakes import ORDERS, InMemoryDocumentStore, fuel_order_doc
from websocket.connection_manager import ConnectionManager


def _socket() -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock(side_effect=lambda data: json.dumps(data))
    return ws


def _received(ws: MagicMock) -> List[Dict[str, Any]]:
    return [c.args[0] for c in ws.send_json.call_args_list[1:]]


async def test_status_change_broadcast_drops_the_pod_otp():
    manager = OrdersWSManager()
    client = _socket()
    await manager.connect(client, tenant_id="tenant_1")
    try:
        store = InMemoryDocumentStore()
        order = fuel_order_doc(
            "ORD-OTP", tenant_id="tenant_1", status="placed",
            pod_otp="482913", pod_otp_generated_at="2026-10-04T09:00:00+00:00",
        )
        store.seed(ORDERS, order["order_id"], order)
        service = OrderService(
            order_repo=FuelOrderRepository(store),
            ws_manager=manager,
            driver_counter_service=AsyncMock(),
            clock=lambda: datetime(2026, 10, 4, 9, 30, tzinfo=timezone.utc),
        )
        await service.apply_status_transition(order, "confirmed")

        (message,) = _received(client)
        assert message["type"] == "order_status_changed"
        assert "pod_otp" not in message["data"]
        assert "482913" not in json.dumps(message)
        # The rest of the order still goes out (OrdersPage replaces the row).
        assert message["data"]["customer_id"] == order["customer_id"]
        assert message["data"]["pod_otp_generated_at"].startswith("2026-10-04T09:00:00")
    finally:
        await manager.shutdown()


async def test_every_order_event_type_drops_otp_fields():
    manager = OrdersWSManager()
    client = _socket()
    await manager.connect(client, tenant_id="tenant_1")
    try:
        doc = {"order_id": "O1", "tenant_id": "tenant_1", "status": "dispatched",
               **{k: "secret-" + k for k in NEVER_BROADCAST_FIELDS}}
        await manager.broadcast_order_placed(dict(doc))
        await manager.broadcast_order_status_changed(dict(doc))
        await manager.broadcast_order_assigned(dict(doc))
        await manager.broadcast("feature_flag_changed", dict(doc), tenant_id="tenant_1")

        messages = _received(client)
        assert len(messages) == 4
        for m in messages:
            assert not NEVER_BROADCAST_FIELDS & set(m["data"]), m["type"]
            assert "secret-" not in json.dumps(m)
            assert m["data"]["order_id"] == "O1"
        assert doc["pod_otp"] == "secret-pod_otp"  # caller's dict untouched
    finally:
        await manager.shutdown()


async def test_inventory_alert_reaches_only_the_items_tenant():
    manager = ConnectionManager()
    same_tenant = _socket()
    other_tenant = _socket()
    await manager.connect(same_tenant, tenant_id="tenant_1")
    await manager.connect(other_tenant, tenant_id="tenant_2")
    service = InventoryService(MagicMock(), ws_manager=manager)

    await service._broadcast_stock_alert(
        item_id="ITEM-1", name="Filter", category="filters", location="Depot",
        new_status="low_stock", quantity=2, min_threshold=5, tenant_id="tenant_1",
    )

    (message,) = _received(same_tenant)
    assert message["type"] == "inventory_alert"
    assert message["data"]["item_id"] == "ITEM-1"
    assert _received(other_tenant) == []


async def test_inventory_alert_without_tenant_reaches_nobody():
    manager = ConnectionManager()
    client = _socket()
    await manager.connect(client, tenant_id="tenant_1")
    service = InventoryService(MagicMock(), ws_manager=manager)

    await service._broadcast_stock_alert(
        item_id="ITEM-2", name="Hose", category="parts", location="Depot",
        new_status="out_of_stock", quantity=0, min_threshold=1, tenant_id="",
    )

    assert _received(client) == []
