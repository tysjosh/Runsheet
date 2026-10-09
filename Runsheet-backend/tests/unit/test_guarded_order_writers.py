"""Guarded order writers (OI-41).

``PATCH /api/orders/{id}/status``, the driver app's
``POST /api/driver/orders/{id}/status`` and the AI ``assign_driver_to_order``
tool used to write a stale read back unguarded, so a loading-plan executor
write landing between their read and their write (``scheduled`` plus run and
truck links) could be overwritten. They now write with the K5a guard, re-read
once on a refusal, and answer 409 ``ORDER_CHANGED_CONCURRENTLY`` if the retry
is refused too.

Real ``FuelOrderRepository`` + ``OrderService`` over the in-memory document
store; a store hook injects the concurrent executor write just before the
handler's guarded ``atomic_update``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.codes import ERROR_CODE_STATUS_MAP, ErrorCode
from errors.handlers import register_exception_handlers
from fuel.order_repository import FuelOrderRepository
from fuel.services.order_service import OrderService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit._loading_plan_fakes import ORDERS, InMemoryDocumentStore, fuel_order_doc

T = "t1"
OID = "ord-1"
EXECUTOR_TS = "2026-07-29T12:30:00+00:00"
EXECUTOR_WRITE = {
    "status": "scheduled",
    "assigned_run_id": "RUN-X",
    "assigned_asset_id": "T-1",
    "last_event_timestamp": EXECUTOR_TS,
}


class World:
    def __init__(self, *, status: str = "confirmed", **order_fields: Any) -> None:
        self.store = InMemoryDocumentStore()
        self.store.seed(ORDERS, OID, fuel_order_doc(OID, tenant_id=T, status=status, **order_fields))
        self.repo = FuelOrderRepository(self.store)
        self.service = OrderService(
            order_repo=self.repo, ws_manager=AsyncMock(), driver_counter_service=AsyncMock()
        )
        self.injected = 0

    def order(self) -> Dict[str, Any]:
        return self.store.doc(ORDERS, OID)

    def race(self, *writes: Dict[str, Any]) -> None:
        """Apply ``writes[n]`` just before the handler's n-th guarded write."""
        pending = list(writes)

        def hook(op, index, doc_id):
            if (op, index, doc_id) == ("atomic_update", ORDERS, OID) and pending:
                self.injected += 1
                self.store.poke(ORDERS, OID, **pending.pop(0))

        self.store.hooks.append(hook)


def _bump(ts: str) -> Dict[str, Any]:
    """A same-status write by someone else: only the timestamp moves."""
    return {"last_event_timestamp": ts}


def test_new_code_is_a_409():
    assert ERROR_CODE_STATUS_MAP[ErrorCode.ORDER_CHANGED_CONCURRENTLY] == 409


# ---------------------------------------------------------------------------
# PATCH /api/orders/{id}/status
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_client():
    from fuel.api import order_endpoints

    def build(world: World) -> TestClient:
        order_endpoints.configure_order_endpoints(
            order_intake_pipeline=MagicMock(),
            order_repository=world.repo,
            order_service=world.service,
        )
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(order_endpoints.router)
        app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
            tenant_id=T, user_id="dispatcher-1", has_pii_access=False,
            roles=["dispatcher"],
        )
        return TestClient(app, raise_server_exceptions=False)

    return build


def _patch(client: TestClient, new_status: str, **body: Any):
    return client.patch(f"/api/orders/{OID}/status", json={"new_status": new_status, **body})


def test_patch_cancel_racing_the_executor_retries_and_keeps_its_links(patch_client):
    world = World(status="confirmed")
    world.race(EXECUTOR_WRITE)
    resp = _patch(patch_client(world), "cancelled", reason="customer called")
    assert resp.status_code == 200, resp.text
    stored = world.order()
    assert stored["status"] == "cancelled"
    # The retry wrote from the fresh read: the executor's links survive.
    assert (stored["assigned_run_id"], stored["assigned_asset_id"]) == ("RUN-X", "T-1")
    assert world.injected == 1


def test_patch_confirm_racing_the_executor_is_refused_and_overwrites_nothing(patch_client):
    world = World(status="placed")
    world.race(EXECUTOR_WRITE)
    resp = _patch(patch_client(world), "confirmed")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "INVALID_STATUS_TRANSITION"
    stored = world.order()
    assert stored["status"] == "scheduled"
    assert (stored["assigned_run_id"], stored["assigned_asset_id"]) == ("RUN-X", "T-1")
    assert stored["last_event_timestamp"] == EXECUTOR_TS


def test_patch_refused_twice_is_order_changed_concurrently(patch_client):
    world = World(status="confirmed")
    world.race(_bump("2026-07-29T12:10:00+00:00"), _bump("2026-07-29T12:20:00+00:00"))
    resp = _patch(patch_client(world), "cancelled", reason="customer called")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "ORDER_CHANGED_CONCURRENTLY"
    assert body["details"] == {"order_id": OID}
    assert world.order()["status"] == "confirmed"
    assert world.store.events(OID) == []


def test_patch_without_a_race_still_succeeds(patch_client):
    world = World(status="placed")
    resp = _patch(patch_client(world), "confirmed")
    assert resp.status_code == 200, resp.text
    assert world.order()["status"] == "confirmed"
    assert len(world.store.events(OID)) == 1


# ---------------------------------------------------------------------------
# Driver app: POST /api/driver/orders/{id}/status
# ---------------------------------------------------------------------------

DRIVER = "drv_1"


@pytest.fixture
def driver_client(monkeypatch):
    from driver.api.transition_endpoints import router as transition_router
    from driver.services.order_transition_service import configure_transition_endpoints
    from tests.support.auth_seam import auth_headers, install_test_auth
    from tests.unit.test_driver_transition_endpoint import FakeQualificationService

    def build(world: World):
        configure_transition_endpoints(
            order_repository=world.repo,
            order_service=world.service,
            driver_qualification_service=FakeQualificationService(),
            inspection_service=None,
            feature_flag_service=None,
            hos_advisory_service=None,
        )
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(transition_router)
        install_test_auth(app)
        client = TestClient(app, raise_server_exceptions=False)
        headers = auth_headers(T, sub=DRIVER, roles=["driver"], driver_id=DRIVER)
        return lambda status: client.post(
            f"/api/driver/orders/{OID}/status", json={"status": status}, headers=headers
        )

    return build


def _dispatched_world() -> World:
    return World(
        status="dispatched", assigned_driver_id=DRIVER,
        assigned_run_id="RUN-1", assigned_asset_id="T-1",
    )


def test_driver_transition_racing_a_concurrent_write_retries(driver_client):
    world = _dispatched_world()
    # Another writer touches the order (same status) between read and write.
    world.race({"last_event_timestamp": "2026-07-29T12:10:00+00:00", "gallons_requested": 640.0})
    resp = driver_client(world)("in_transit")
    assert resp.status_code == 200, resp.text
    stored = world.order()
    assert stored["status"] == "in_transit"
    assert stored["gallons_requested"] == 640.0  # not overwritten by the stale read
    assert (stored["assigned_run_id"], stored["assigned_asset_id"]) == ("RUN-1", "T-1")


def test_driver_transition_refused_twice_is_order_changed_concurrently(driver_client):
    world = _dispatched_world()
    world.race(_bump("2026-07-29T12:10:00+00:00"), _bump("2026-07-29T12:20:00+00:00"))
    resp = driver_client(world)("in_transit")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "ORDER_CHANGED_CONCURRENTLY"
    assert world.order()["status"] == "dispatched"


def test_driver_transition_without_a_race_still_succeeds(driver_client):
    world = _dispatched_world()
    resp = driver_client(world)("in_transit")
    assert resp.status_code == 200, resp.text
    assert world.order()["status"] == "in_transit"


# ---------------------------------------------------------------------------
# AI tool: assign_driver_to_order
# ---------------------------------------------------------------------------


@pytest.fixture
def assign_tool(monkeypatch):
    import Agents.tools.order_mutation_tools as tools

    def build(world: World):
        driver_repo = MagicMock()
        driver_repo.get = AsyncMock(return_value=SimpleNamespace(status="active"))
        driver_repo.increment_counters = AsyncMock()
        protocol = MagicMock()
        protocol.process_mutation = AsyncMock(return_value=SimpleNamespace(
            executed=True, risk_level="medium", confirmation_method="immediate",
            approval_id=None,
        ))
        autonomy = MagicMock()
        autonomy.get_level = AsyncMock(return_value="full-auto")
        monkeypatch.setattr(tools, "get_current_tenant", lambda: T)
        monkeypatch.setattr(tools, "_get_order_repo", lambda: world.repo)
        monkeypatch.setattr(tools, "_get_driver_repo", lambda: driver_repo)
        monkeypatch.setattr(tools, "_get_confirmation_protocol", lambda: protocol)
        monkeypatch.setattr(tools, "_get_autonomy_config_service", lambda: autonomy)

        async def call() -> Dict[str, Any]:
            raw = await tools.assign_driver_to_order(order_id=OID, driver_id="drv-9")
            return json.loads(raw)

        return call, driver_repo

    return build


async def test_assign_racing_the_executor_keeps_the_links_and_sets_the_driver(assign_tool):
    world = World(status="confirmed")
    world.race(EXECUTOR_WRITE)
    call, driver_repo = assign_tool(world)
    result = await call()
    assert result.get("action") == "executed", result
    stored = world.order()
    assert stored["assigned_driver_id"] == "drv-9"
    assert stored["status"] == "scheduled"
    assert (stored["assigned_run_id"], stored["assigned_asset_id"]) == ("RUN-X", "T-1")
    driver_repo.increment_counters.assert_awaited_once()


async def test_assign_refused_twice_returns_the_error_and_counts_nothing(assign_tool):
    world = World(status="confirmed")
    world.race(_bump("2026-07-29T12:10:00+00:00"), _bump("2026-07-29T12:20:00+00:00"))
    call, driver_repo = assign_tool(world)
    result = await call()
    assert result["error_code"] == "order_changed_concurrently"
    assert world.order().get("assigned_driver_id") in (None, "")
    driver_repo.increment_counters.assert_not_awaited()
