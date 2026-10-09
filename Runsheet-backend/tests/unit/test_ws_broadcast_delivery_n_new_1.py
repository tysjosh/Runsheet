"""WS broadcasts reach real managers' clients (N-new-1, R10.5, D1, D2).

Every test here drives a REAL WebSocket manager. The fake socket's
``send_json`` runs ``json.dumps`` on the message, so a payload that isn't
JSON-safe fails the way Starlette's does: the manager treats the client as
dead and drops it.

* ``OrderService._broadcast_status_change`` must go through
  ``OrdersWSManager.broadcast_order_status_changed`` with the full,
  JSON-encoded order (``OrdersPage.handleOrderUpdate`` replaces the row with
  ``message.data``). The old one-dict ``broadcast({...})`` call raised
  TypeError against ``broadcast(event_type, data, tenant_id)`` and was
  swallowed, so nobody ever received ``order_status_changed``.
* The loading-plan executor's transitions go through the same path (R10.5).
* ``SchedulingWebSocketManager.broadcast`` refuses a tenantless call, so the
  job and delay broadcasts must pass ``tenant_id``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fuel.order_repository import FuelOrderRepository
from fuel.services.order_service import OrderService
from fuel.websocket.orders_ws import OrdersWSManager
from scheduling.services.delay_detection_service import DelayDetectionService
from scheduling.services.job_service import JobService
from scheduling.websocket.scheduling_ws import SchedulingWebSocketManager
from tests.unit._loading_plan_fakes import (
    ORDERS,
    ApprovalHarness,
    InMemoryDocumentStore,
    fuel_order_doc,
    order_fixture,
)


def _socket() -> MagicMock:
    """A fake WebSocket whose ``send_json`` really serializes the message."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock(side_effect=lambda data: json.dumps(data))
    return ws


def _received(ws: MagicMock) -> List[Dict[str, Any]]:
    """Messages after the connection handshake (the first send)."""
    return [c.args[0] for c in ws.send_json.call_args_list[1:]]


def _broadcast_failures(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [r.getMessage() for r in caplog.records if "broadcast failed" in r.getMessage()]


# ---------------------------------------------------------------------------
# Orders socket: OrderService -> OrdersWSManager
# ---------------------------------------------------------------------------


async def test_status_change_reaches_only_the_subscribed_tenant_client(caplog):
    caplog.set_level(logging.DEBUG)
    manager = OrdersWSManager()
    subscriber = _socket()
    other_tenant = _socket()
    placed_only = _socket()
    await manager.connect(subscriber, subscriptions=["order_status_changed"], tenant_id="tenant_1")
    await manager.connect(other_tenant, subscriptions=["order_status_changed"], tenant_id="tenant_2")
    await manager.connect(placed_only, subscriptions=["order_placed"], tenant_id="tenant_1")
    try:
        store = InMemoryDocumentStore()
        order = fuel_order_doc("ORD-N1", tenant_id="tenant_1", status="placed")
        store.seed(ORDERS, order["order_id"], order)
        service = OrderService(
            order_repo=FuelOrderRepository(store),
            ws_manager=manager,
            driver_counter_service=AsyncMock(),
            clock=lambda: datetime(2026, 10, 4, 9, 30, tzinfo=timezone.utc),
        )

        updated = await service.apply_status_transition(order, "confirmed")
        assert isinstance(updated["updated_at"], datetime)  # not JSON-safe as is

        messages = _received(subscriber)
        assert [m["type"] for m in messages] == ["order_status_changed"]
        message = messages[0]
        assert message["tenant_id"] == "tenant_1"
        data = message["data"]
        assert data["order_id"] == "ORD-N1"
        assert data["tenant_id"] == "tenant_1"
        assert data["old_status"] == "placed"
        assert data["new_status"] == "confirmed"
        assert data["status"] == "confirmed"
        assert data["updated_at"] == "2026-10-04T09:30:00+00:00"
        # The full order, so OrdersPage can replace the row with message.data.
        for key in ("customer_id", "customer_name", "product_code", "gallons_requested",
                    "delivery_window_start", "delivery_window_end"):
            assert data[key] == order[key], key

        assert _received(other_tenant) == []
        assert _received(placed_only) == []
        assert _broadcast_failures(caplog) == []
        assert manager.get_client_metadata(subscriber) is not None  # not dropped
        assert manager.get_connection_count() == 3
    finally:
        await manager.shutdown()


async def test_one_dict_broadcast_does_not_fit_the_orders_manager():
    """Pins why the old ``broadcast({...})`` call delivered nothing."""
    manager = OrdersWSManager()
    with pytest.raises(TypeError):
        await manager.broadcast({"type": "x"})


async def test_loading_plan_execution_broadcasts_each_scheduled_order(caplog):
    """R10.5: the executor's transitions reach a tenant client of the real manager."""
    caplog.set_level(logging.DEBUG)
    manager = OrdersWSManager()
    tenant_client = _socket()
    other_tenant = _socket()
    await manager.connect(tenant_client, tenant_id="tenant-1")
    await manager.connect(other_tenant, tenant_id="tenant-2")
    try:
        h = ApprovalHarness(
            [order_fixture("o1", tenant_id="tenant-1"), order_fixture("o2", tenant_id="tenant-1")],
            order_ws=manager,
        )
        h.add_plan("A", ["o1", "o2"])
        assert (await h.approve("A"))["status"] == "executed"

        changed = [m for m in _received(tenant_client) if m["type"] == "order_status_changed"]
        assert {m["data"]["order_id"] for m in changed} == {"o1", "o2"}
        assert all(m["data"]["new_status"] == "scheduled" for m in changed)
        assert all(m["data"]["status"] == "scheduled" for m in changed)
        assert all(m["tenant_id"] == "tenant-1" for m in changed)
        assert _received(other_tenant) == []
        assert _broadcast_failures(caplog) == []
        assert manager.get_client_metadata(tenant_client) is not None
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# Scheduling socket: job and delay broadcasts carry tenant_id
# ---------------------------------------------------------------------------


async def test_job_and_delay_broadcasts_reach_only_the_jobs_tenant(caplog):
    caplog.set_level(logging.DEBUG)
    manager = SchedulingWebSocketManager()
    tenant_client = _socket()
    other_tenant = _socket()
    await manager.connect(tenant_client, tenant_id="tenant_1")
    await manager.connect(other_tenant, tenant_id="tenant_2")
    try:
        job = {
            "job_id": "JOB-N1",
            "tenant_id": "tenant_1",
            "job_type": "cargo_transport",
            "status": "scheduled",
            "asset_assigned": "TRUCK-1",
            "origin": "Depot",
            "destination": "Site",
            "estimated_arrival": "2026-10-04T10:00:00+00:00",
        }
        with patch("scheduling.services.job_service.get_settings"):
            jobs = JobService(MagicMock(), redis_url=None)
        jobs._ws_manager = manager
        await jobs._broadcast_job_update("job_created", job)

        delays = DelayDetectionService(MagicMock(), ws_manager=manager)
        await delays._broadcast_delay_alert(job, 25)

        messages = _received(tenant_client)
        assert [m["type"] for m in messages] == ["job_created", "delay_alert"]
        assert messages[0]["data"]["job_id"] == "JOB-N1"
        assert messages[1]["data"]["delay_duration_minutes"] == 25
        assert _received(other_tenant) == []
        assert not [r for r in caplog.records if "refused tenantless" in r.getMessage()]
        assert _broadcast_failures(caplog) == []
    finally:
        await manager.shutdown()
