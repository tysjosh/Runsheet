"""MarginOrderSubscriber: order events -> estimate / delivery / void (design "Stage inputs")."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from commerce.hooks.margin_order_subscriber import (
    MarginOrderSubscriber,
    register_margin_order_subscribers,
)
from commerce.services.margin_repository import RecordFilters

from ._service_support import SweepStore, build_service, order_doc
from .conftest import TENANT_A

UTC = timezone.utc
CREATED = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
DELIVERED = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)


class FakeOrderService:
    """The OrderService subscription helper and its notify loop."""

    def __init__(self) -> None:
        self.subscribers: Dict[str, List[Any]] = {}

    def subscribe(self, event_name: str, handler: Any) -> None:
        self.subscribers.setdefault(event_name, []).append(handler)

    async def notify(self, order: Dict[str, Any], status: str) -> None:
        for handler in self.subscribers.get(f"order.{status}", []):
            await handler(order)


async def _records(repo, stage):
    return (await repo.list_records(TENANT_A, RecordFilters(stage=stage, status="all"), limit=10)).items


def test_registers_one_handler_per_event():
    orders = FakeOrderService()
    handlers = register_margin_order_subscribers(orders, MagicMock())
    assert sorted(orders.subscribers) == ["order.cancelled", "order.delivered", "order.dispatched", "order.failed"]
    assert len(handlers) == 4
    with pytest.raises(ValueError):
        MarginOrderSubscriber(MagicMock(), "order.placed")


async def test_subscriber_swallows_hook_errors():
    hook = MagicMock()
    hook.order_event.side_effect = RuntimeError("boom")
    await MarginOrderSubscriber(hook, "order.delivered")({"order_id": "O", "tenant_id": TENANT_A})
    hook.order_event.assert_called_once()


async def test_order_lifecycle_records(repo, flag_on):
    service = build_service(repo, SweepStore())
    orders = FakeOrderService()
    register_margin_order_subscribers(orders, service.hook)

    # Events are drained one at a time: the in-memory SQLite engine shares
    # one connection, so concurrent sessions are not isolated there (the
    # Postgres suite covers concurrent writers).
    async def emit(order, status):
        await orders.notify(order, status)
        await service.drain()

    dispatched = order_doc("ORD-1", created_at=CREATED)
    await emit(dispatched, "dispatched")
    (estimate,) = await _records(repo, "order_estimate")
    assert estimate["status"] == "active" and estimate["alert_state"] == "none"  # estimates never alert

    delivered = order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED)
    await emit(delivered, "delivered")
    await emit(delivered, "delivered")  # reconcile_delivery_result replay
    (delivery,) = await _records(repo, "delivery")
    assert delivery["as_of"] == DELIVERED and delivery["version"] == 1
    assert delivery["alert_state"] == "pending"  # missing cost on a live delivery

    cancelled = order_doc("ORD-2", created_at=CREATED)
    await emit(cancelled, "dispatched")
    await emit(dict(cancelled, status="cancelled"), "cancelled")
    failed = order_doc("ORD-3", created_at=CREATED)
    await emit(failed, "dispatched")
    await emit(dict(failed, status="failed"), "failed")
    statuses = {r["order_id"]: r["status"] for r in await _records(repo, "order_estimate")}
    assert statuses == {"ORD-1": "active", "ORD-2": "void", "ORD-3": "void"}


async def test_delivered_without_pod_writes_nothing(repo, flag_on):
    service = build_service(repo, SweepStore())
    orders = FakeOrderService()
    register_margin_order_subscribers(orders, service.hook)
    await orders.notify(order_doc("ORD-1", created_at=CREATED, status="delivered"), "delivered")
    await service.drain()
    assert await _records(repo, "delivery") == []
    assert (await repo.skipped_sources(TENANT_A))["count"] == 0  # no_inputs is not stored


async def test_flag_off_subscriber_writes_nothing(repo):
    service = build_service(repo, SweepStore())
    orders = FakeOrderService()
    register_margin_order_subscribers(orders, service.hook)
    await orders.notify(order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED), "delivered")
    assert service.pending_tasks == 0
    assert await _records(repo, "delivery") == []
