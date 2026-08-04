"""Tests for the dispatcher-plan to driver-work bridge."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from driver.services.work_service import DriverWorkService
from errors.exceptions import AppException
from fuel.services.order_service import OrderService
from fuel.services.plan_dispatch_service import FuelPlanDispatchService


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _order(order_id: str = "ord-1", *, status: str = "scheduled") -> dict:
    return {
        "order_id": order_id,
        "tenant_id": "tenant-1",
        "customer_id": "customer-1",
        "customer_tank_id": "tank-1",
        "status": status,
        "assigned_driver_id": None,
        "assigned_asset_id": None,
        "assigned_run_id": None,
        "delivery_window_start": "2026-07-30T08:00:00+00:00",
        "delivery_window_end": "2026-07-30T12:00:00+00:00",
        "source_schema_version": "1.0",
        "trace_id": "trace-1",
        "updated_at": NOW,
        "last_event_timestamp": NOW,
    }


def _plan(*, order_id: str | None = "ord-1") -> dict:
    assignment = {
        "station_id": "tank-1",
        "compartment_id": "c-1",
        "fuel_grade": "DIESEL_2",
        "quantity_liters": 1000,
    }
    if order_id:
        assignment["order_id"] = order_id
    return {
        "plan_id": "plan-1",
        "run_id": "run-1",
        "truck_id": "truck-1",
        "tenant_id": "tenant-1",
        "status": "proposed",
        "assignments": [assignment],
    }


def _route(*, order_ids=None) -> dict:
    return {
        "route_id": "route-1",
        "plan_id": "plan-1",
        "run_id": "run-1",
        "truck_id": "truck-1",
        "tenant_id": "tenant-1",
        "stops": [
            {
                "station_id": "tank-1",
                "order_ids": ["ord-1"] if order_ids is None else order_ids,
                "eta": "2026-07-30T09:00:00+00:00",
                "drop": {"DIESEL_2": 1000},
                "sequence": 0,
            }
        ],
    }


class FakeOrderRepository:
    def __init__(self, orders):
        self.orders = {order["order_id"]: dict(order) for order in orders}
        self.events = []

    async def list_for_tenant(self, tenant_id, *, size):
        return [
            dict(order)
            for order in self.orders.values()
            if order["tenant_id"] == tenant_id
        ]

    async def append_event(self, tenant_id, event):
        self.events.append(dict(event))

    async def upsert_with_last_event_timestamp(self, tenant_id, order):
        self.orders[order["order_id"]] = dict(order)
        return True

    async def search_for_driver(
        self,
        tenant_id,
        driver_id,
        *,
        statuses=(),
        window_start=None,
        window_end=None,
        page=1,
        size=50,
    ):
        matching = [
            dict(order)
            for order in self.orders.values()
            if order["tenant_id"] == tenant_id
            and order.get("assigned_driver_id") == driver_id
            and (not statuses or order.get("status") in statuses)
        ]
        return {
            "orders": matching[(page - 1) * size : page * size],
            "total": len(matching),
            "page": page,
            "size": size,
        }


class FakeES:
    def __init__(self, route, *, existing_execution=None):
        self.route = dict(route)
        self.existing_execution = existing_execution
        self.updates = []

    async def search_documents(self, index, query, size):
        if index == "mvp_routes":
            return {"hits": {"hits": [{"_source": dict(self.route)}]}}
        if index == "mvp_plan_executions" and self.existing_execution:
            return {
                "hits": {
                    "hits": [{"_source": dict(self.existing_execution)}]
                }
            }
        return {"hits": {"hits": []}}

    async def update_document(self, index, doc_id, update):
        self.updates.append((index, doc_id, dict(update)))
        if index == "mvp_routes":
            self.route.update(update)


class FakeDriverRepository:
    def __init__(self, drivers=None):
        self.drivers = (
            [
                {
                    "driver_id": "driver-1",
                    "tenant_id": "tenant-1",
                    "assigned_truck_id": "truck-1",
                    "status": "active",
                }
            ]
            if drivers is None
            else drivers
        )

    async def search(self, tenant_id, **filters):
        return {"drivers": list(self.drivers), "total": len(self.drivers)}


def _service(*, orders=None, route=None, drivers=None, existing_execution=None):
    repo = FakeOrderRepository(orders or [_order()])
    es = FakeES(
        route or _route(), existing_execution=existing_execution
    )
    order_service = OrderService(
        order_repo=repo,
        ws_manager=AsyncMock(),
        clock=lambda: NOW,
    )
    push_subscriber = AsyncMock()
    order_service.subscribe("order.dispatched", push_subscriber)
    execution_service = AsyncMock()
    execution_service.create_execution = AsyncMock(
        return_value={"execution_id": "execution-1"}
    )
    driver_ws = AsyncMock()
    service = FuelPlanDispatchService(
        es_service=es,
        order_repository=repo,
        order_service=order_service,
        driver_repository=FakeDriverRepository(drivers),
        execution_service=execution_service,
        driver_ws_manager=driver_ws,
        clock=lambda: NOW,
    )
    return service, repo, es, execution_service, driver_ws, push_subscriber


@pytest.mark.asyncio
async def test_dispatch_links_transitions_and_notifies():
    service, repo, es, execution, driver_ws, push = _service()

    result = await service.dispatch(
        tenant_id="tenant-1",
        plan_doc=_plan(),
        actor_user_id="dispatcher-1",
    )

    stored = repo.orders["ord-1"]
    assert stored["status"] == "dispatched"
    assert stored["assigned_driver_id"] == "driver-1"
    assert stored["assigned_asset_id"] == "truck-1"
    assert stored["assigned_run_id"] == "run-1"
    assert [event["event_type"] for event in repo.events] == [
        "order_assigned",
        "order_dispatched",
    ]
    push.assert_awaited_once()
    driver_ws.send_assignment.assert_awaited_once_with(
        "driver-1",
        {
            "plan_id": "plan-1",
            "run_id": "run-1",
            "truck_id": "truck-1",
            "route_ids": ["route-1"],
            "order_ids": ["ord-1"],
        },
    )
    execution.create_execution.assert_awaited_once()
    assert result.newly_dispatched == 1
    assert result.order_ids == ["ord-1"]
    assert ("mvp_load_plans", "plan-1") == es.updates[-1][:2]


@pytest.mark.asyncio
async def test_approved_plan_is_visible_in_driver_work_read_model():
    service, repo, es, *_ = _service()

    await service.dispatch(
        tenant_id="tenant-1",
        plan_doc=_plan(),
        actor_user_id="dispatcher-1",
    )
    work = await DriverWorkService(
        es_service=es,
        order_repository=repo,
    ).list_work(
        "tenant-1",
        "driver-1",
        statuses=("dispatched", "in_transit"),
    )

    assert work["pagination"]["total"] == 1
    assert work["data"][0]["order_id"] == "ord-1"
    assert work["data"][0]["status"] == "dispatched"


@pytest.mark.asyncio
async def test_confirmed_order_is_scheduled_before_dispatch():
    service, repo, *_ = _service(orders=[_order(status="confirmed")])

    await service.dispatch(
        tenant_id="tenant-1",
        plan_doc=_plan(),
        actor_user_id="dispatcher-1",
    )

    assert [event["event_type"] for event in repo.events] == [
        "order_assigned",
        "order_scheduled",
        "order_dispatched",
    ]


@pytest.mark.asyncio
async def test_retry_is_idempotent_for_active_order_and_execution():
    active = _order(status="dispatched")
    active.update(
        assigned_driver_id="driver-1",
        assigned_asset_id="truck-1",
        assigned_run_id="run-1",
    )
    service, repo, _, execution, driver_ws, push = _service(
        orders=[active],
        existing_execution={"execution_id": "execution-existing"},
    )

    result = await service.dispatch(
        tenant_id="tenant-1",
        plan_doc=_plan(),
        actor_user_id="dispatcher-1",
    )

    assert result.newly_dispatched == 0
    assert result.already_dispatched == 1
    assert repo.events == []
    push.assert_not_awaited()
    execution.create_execution.assert_not_awaited()
    driver_ws.send_assignment.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_ambiguous_stop_requires_plan_regeneration():
    orders = [_order("ord-1"), _order("ord-2")]
    service, *_ = _service(
        orders=orders,
        route=_route(order_ids=[]),
    )

    with pytest.raises(AppException) as raised:
        await service.dispatch(
            tenant_id="tenant-1",
            plan_doc=_plan(order_id=None),
            actor_user_id="dispatcher-1",
        )

    assert raised.value.status_code == 409
    assert raised.value.details["reason"] == "plan_order_ambiguous"


@pytest.mark.asyncio
async def test_dispatch_requires_exactly_one_active_driver():
    service, *_ = _service(drivers=[])

    with pytest.raises(AppException) as raised:
        await service.dispatch(
            tenant_id="tenant-1",
            plan_doc=_plan(),
            actor_user_id="dispatcher-1",
        )

    assert raised.value.error_code.value == "DRIVER_UNAVAILABLE"
    assert raised.value.details["reason"] == "no_active_driver_for_truck"
