"""Explicit driver and ``notify`` flag on ``FuelPlanDispatchService.dispatch``.

Dispatch-board design K7.4 and freeze rule 8: the board names the lane's
driver instead of relying on ``drivers_current.assigned_truck_id``, and the
redispatch service suppresses the realtime assignment so it can send every
driver message itself after all writes. Existing callers pass neither and get
today's behaviour, including exactly one ``send_assignment`` call.
"""
from __future__ import annotations

import pytest

from errors.codes import ErrorCode
from errors.exceptions import AppException
from tests.unit.test_plan_dispatch_service import (
    FakeDriverRepository,
    Harness,
    T,
    _order,
)

pytestmark = pytest.mark.asyncio

DRIVERS = [
    # The truck's assigned driver: what the default path resolves.
    {"driver_id": "driver-1", "tenant_id": T, "assigned_truck_id": "truck-1", "status": "active"},
    # Paired on the board for the day, assigned to another truck in the roster.
    {"driver_id": "driver-9", "tenant_id": T, "assigned_truck_id": "truck-7", "status": "active"},
    {"driver_id": "driver-off", "tenant_id": T, "assigned_truck_id": None, "status": "off_duty"},
    {"driver_id": "driver-x", "tenant_id": "tenant-2", "assigned_truck_id": "truck-1", "status": "active"},
]


class _DriverRepoWithGet(FakeDriverRepository):
    def __init__(self, drivers):
        super().__init__(drivers)
        self.get_calls: list[tuple[str, str]] = []
        self.search_calls = 0

    async def search(self, tenant_id, **filters):
        self.search_calls += 1
        result = await super().search(tenant_id, **filters)
        drivers = [d for d in result["drivers"] if d["tenant_id"] == tenant_id]
        return {"drivers": drivers, "total": len(drivers)}

    async def get(self, tenant_id, driver_id):
        self.get_calls.append((tenant_id, driver_id))
        for driver in self.drivers:
            # Tenant-scoped like DriverRepository.get: foreign ids read as None.
            if driver["driver_id"] == driver_id and driver["tenant_id"] == tenant_id:
                return dict(driver)
        return None


def _harness() -> tuple[Harness, _DriverRepoWithGet]:
    harness = Harness([_order("ord-1", status="scheduled")])
    repo = _DriverRepoWithGet(DRIVERS)
    harness.service._driver_repository = repo
    return harness, repo


async def _dispatch(harness: Harness, **kwargs):
    return await harness.service.dispatch(
        tenant_id=T, plan_doc=harness.plans[0], actor_user_id="dispatcher-1", **kwargs
    )


async def test_explicit_active_driver_is_dispatched_without_truck_lookup():
    harness, repo = _harness()
    result = await _dispatch(harness, driver_id="driver-9")
    assert result.driver_id == "driver-9"
    assert repo.get_calls == [(T, "driver-9")]
    assert repo.search_calls == 0
    order = harness.order("ord-1")
    assert order["status"] == "dispatched"
    assert order["assigned_driver_id"] == "driver-9"


@pytest.mark.parametrize("driver_id", ["driver-off", "driver-x", "missing", ""])
async def test_unavailable_explicit_driver_is_refused_before_any_write(driver_id):
    harness, _repo = _harness()
    with pytest.raises(AppException) as raised:
        await _dispatch(harness, driver_id=driver_id)
    exc = raised.value
    assert exc.status_code == 409
    assert exc.error_code == ErrorCode.DRIVER_UNAVAILABLE
    assert exc.details["reason"] == "board_driver_unavailable"
    # Foreign tenant and inactive look identical: nothing about tenant-2 leaks.
    assert "driver_id" not in exc.details
    harness.assert_nothing_dispatched()
    harness.assert_unlinked("ord-1")


async def test_notify_false_never_calls_ws_and_result_carries_payload():
    harness, _repo = _harness()
    result = await _dispatch(harness, driver_id="driver-9", notify=False)
    harness.driver_ws.send_assignment.assert_not_awaited()
    assert harness.driver_ws.method_calls == []
    assert result.plan_id == "plan-1"
    assert result.run_id == "run-1"
    assert result.truck_id == "truck-1"
    assert result.route_ids == ["route-1"]
    assert result.order_ids == ["ord-1"]
    assert result.driver_id == "driver-9"
    # Everything else still happened.
    assert harness.order("ord-1")["status"] == "dispatched"
    harness.push.assert_awaited()  # order.dispatched subscriber is unchanged


async def test_default_path_sends_assignment_once_with_todays_payload():
    harness, repo = _harness()
    result = await _dispatch(harness)
    assert result.driver_id == "driver-1"
    assert repo.get_calls == []
    harness.driver_ws.send_assignment.assert_awaited_once_with(
        "driver-1",
        {
            "plan_id": "plan-1",
            "run_id": "run-1",
            "truck_id": "truck-1",
            "route_ids": ["route-1"],
            "order_ids": ["ord-1"],
        },
    )


async def test_explicit_driver_with_notify_sends_to_that_driver():
    harness, _repo = _harness()
    await _dispatch(harness, driver_id="driver-9")
    harness.driver_ws.send_assignment.assert_awaited_once()
    assert harness.driver_ws.send_assignment.await_args.args[0] == "driver-9"
