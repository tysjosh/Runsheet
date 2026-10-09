"""Driver transitions serialized with board writes (dispatch-board K8.6).

Freeze rule 10's two existing-code changes on the driver path:

* the status write is guarded (``guard_stored_state=True``), so a driver write
  built from a read taken before a board relink is refused with 409
  ``ORDER_CHANGED_CONCURRENTLY`` and the relink survives; the refusal is not
  stored for idempotency, so a same-key retry runs again;
* gate 0 refuses ``in_transit`` on a ``bp-`` run whose board plan is not
  ``dispatched`` (or ``completed``) with 409 ``BOARD_ROUTE_UPDATING``, fails
  closed on a read error or a missing reader (freeze rule 11 (a)), and never
  reads an agent run.

The endpoint runs over the real ``FuelOrderRepository`` and ``OrderService``
on the shared in-memory store, so the K5a guard is the real one.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import driver.middleware.idempotency as idempotency_module
from driver.api.transition_endpoints import router as transition_router
from driver.middleware.idempotency import configure_idempotency_middleware
from driver.services.order_transition_service import (
    DriverTransitionGateStack,
    configure_transition_endpoints,
    get_gate_stack,
)
from errors.exceptions import AppException
from errors.handlers import register_exception_handlers
from fuel.order_repository import FuelOrderRepository
from fuel.services.order_service import OrderService
from tests.support.auth_seam import auth_headers, install_test_auth
from tests.unit._loading_plan_fakes import ORDERS, InMemoryDocumentStore, fuel_order_doc

T = "tenant-1"
DRIVER = "drv-1"
ORDER = "ord-1"
PLANS = "mvp_load_plans"
BOARD_RUN = "bp-load1-r1"
AGENT_RUN = "run-agent-1"


class _IdempotencyStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def index_document(self, index, doc_id, document):
        self.docs[doc_id] = document

    async def get_document(self, index, doc_id):
        return self.docs.get(doc_id)


@pytest.fixture(autouse=True)
def idempotency_store():
    previous = idempotency_module.get_idempotency_middleware()
    store = _IdempotencyStore()
    configure_idempotency_middleware(es_service=store)
    try:
        yield store
    finally:
        idempotency_module._idempotency_middleware = previous


@pytest.fixture(autouse=True)
def _restore_transition_wiring():
    yield
    configure_transition_endpoints()


class _Reader:
    """Spy around the store's ``get_document``; optionally raises."""

    def __init__(self, store: InMemoryDocumentStore, *, fail: bool = False) -> None:
        self.store = store
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, index, doc_id):
        self.calls.append((index, doc_id))
        if self.fail:
            raise RuntimeError("store unavailable")
        return await self.store.get_document(index, doc_id)


def _harness(*, run_id: str = AGENT_RUN, plan_status: Optional[str] = None,
             reader: Any = "store", fail_read: bool = False, status: str = "dispatched"):
    store = InMemoryDocumentStore()
    store.seed(
        ORDERS,
        ORDER,
        fuel_order_doc(
            ORDER,
            tenant_id=T,
            status=status,
            assigned_driver_id=DRIVER,
            assigned_asset_id="truck-1",
            assigned_run_id=run_id,
        ),
    )
    if plan_status is not None:
        store.seed(
            PLANS,
            run_id,
            {
                "plan_id": run_id,
                "run_id": run_id,
                "tenant_id": T,
                "truck_id": "truck-1",
                "status": plan_status,
                "source": "dispatch_board",
            },
        )
    repo = FuelOrderRepository(store)
    order_service = OrderService(order_repo=repo, ws_manager=AsyncMock())
    spy = _Reader(store, fail=fail_read) if reader == "store" else reader
    configure_transition_endpoints(
        order_repository=repo,
        order_service=order_service,
        board_plan_reader=spy,
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(transition_router)
    install_test_auth(app)
    return app, store, spy


def _post(app, status="in_transit", **headers):
    hdrs = auth_headers(T, sub=DRIVER, roles=["driver"], driver_id=DRIVER)
    hdrs.update(headers)
    return TestClient(app).post(
        f"/api/driver/orders/{ORDER}/status", json={"status": status}, headers=hdrs
    )


def _relink_before_write(store: InMemoryDocumentStore, *, times: int = 1):
    """Simulate the board relinking the order between the driver's read and write."""
    seen = []

    def hook(op, index, doc_id):
        if (op, index, doc_id) == ("atomic_update", ORDERS, ORDER) and len(seen) < times:
            seen.append(1)
            store.poke(
                ORDERS,
                ORDER,
                assigned_run_id="bp-load2-r1",
                assigned_asset_id="truck-2",
                assigned_driver_id="drv-2",
                last_event_timestamp="2099-01-01T00:00:00+00:00",
            )

    store.hooks.append(hook)


# ---------------------------------------------------------------------------
# Guarded write
# ---------------------------------------------------------------------------


def _bump_before_write(store: InMemoryDocumentStore, *, times: int):
    """A same-driver edit that only moves the timestamp, before each of ``times`` writes."""
    seen = []

    def hook(op, index, doc_id):
        if (op, index, doc_id) == ("atomic_update", ORDERS, ORDER) and len(seen) < times:
            seen.append(1)
            store.poke(ORDERS, ORDER, last_event_timestamp=f"2026-07-29T12:3{len(seen)}:00+00:00")

    store.hooks.append(hook)


def test_stale_read_after_relink_to_another_driver_is_403_and_relink_survives():
    # The guarded write refuses, the one in-request re-read (OI-41) finds the
    # order on another driver, and the resolver answers today's 403.
    app, store, _ = _harness()
    _relink_before_write(store)
    resp = _post(app)
    assert resp.status_code == 403, resp.text
    doc = store.doc(ORDERS, ORDER)
    assert doc["status"] == "dispatched"
    assert (doc["assigned_run_id"], doc["assigned_driver_id"]) == ("bp-load2-r1", "drv-2")
    assert store.events() == []


def test_one_concurrent_write_is_retried_in_the_request(idempotency_store):
    app, store, _ = _harness()
    _bump_before_write(store, times=1)
    resp = _post(app, **{"X-Idempotency-Key": "k-2"})
    assert resp.status_code == 200, resp.text
    assert store.doc(ORDERS, ORDER)["status"] == "in_transit"
    assert [e["event_type"] for e in store.events()] == ["order_in_transit"]


def test_refused_twice_is_409_not_stored_and_the_same_key_runs_again(idempotency_store):
    app, store, _ = _harness()
    _bump_before_write(store, times=2)
    first = _post(app, **{"X-Idempotency-Key": "k-1"})
    assert first.status_code == 409, first.text
    assert first.json()["error_code"] == "ORDER_CHANGED_CONCURRENTLY"
    assert first.json()["details"] == {"order_id": ORDER}
    assert idempotency_store.docs == {}
    assert store.doc(ORDERS, ORDER)["status"] == "dispatched"
    second = _post(app, **{"X-Idempotency-Key": "k-1"})
    assert second.status_code == 200, second.text
    assert store.doc(ORDERS, ORDER)["status"] == "in_transit"


def test_retry_after_relink_onto_a_board_run_being_reissued_runs_gate_0_again():
    """Merge of OI-41 and freeze rule 10: the in-request retry re-evaluates the
    gates on the fresh read, so a relink (same driver) onto a ``bp-`` run whose
    plan is still ``draft`` is refused with BOARD_ROUTE_UPDATING, never started."""
    app, store, reader = _harness()
    store.seed(
        PLANS,
        BOARD_RUN,
        {"plan_id": BOARD_RUN, "run_id": BOARD_RUN, "tenant_id": T,
         "truck_id": "truck-2", "status": "draft", "source": "dispatch_board"},
    )
    seen = []

    def hook(op, index, doc_id):
        if (op, index, doc_id) == ("atomic_update", ORDERS, ORDER) and not seen:
            seen.append(1)
            store.poke(ORDERS, ORDER, assigned_run_id=BOARD_RUN, assigned_asset_id="truck-2",
                       last_event_timestamp="2099-01-01T00:00:00+00:00")

    store.hooks.append(hook)
    resp = _post(app)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "BOARD_ROUTE_UPDATING"
    assert store.doc(ORDERS, ORDER)["status"] == "dispatched"
    assert store.events() == []


class _Metrics:
    def __init__(self) -> None:
        self.metrics: list[tuple[str, float, dict]] = []

    def record_metric(self, name, value, tags=None):
        self.metrics.append((name, value, dict(tags or {})))


def test_stale_base_read_keeps_refusing_and_is_counted(monkeypatch):
    """Phase 0 review issue 1 / Phase 1 P1-5: if the base read ever serves a
    copy older than the stored document the guard compares against, every
    retry refuses, and each refusal is counted so the stuck case shows up.
    Since P1-5 the base read is ``get_current``, so this is simulated by
    stubbing it."""
    import driver.api.transition_endpoints as endpoints
    import telemetry.service as telemetry_service
    from driver.services.order_transition_service import get_work_ref_resolver

    app, store, _ = _harness()
    stale = dict(store.doc(ORDERS, ORDER))
    store.poke(ORDERS, ORDER, last_event_timestamp="2026-07-29T12:30:00+00:00")
    repo = get_work_ref_resolver()._order_repository

    async def stale_read(tenant_id, order_id):
        return dict(stale)

    monkeypatch.setattr(repo, "get_current", stale_read)
    sink = _Metrics()
    monkeypatch.setattr(telemetry_service, "get_telemetry_service", lambda: sink)
    for _ in range(3):
        resp = _post(app)
        assert resp.status_code == 409, resp.text
        assert resp.json()["error_code"] == "ORDER_CHANGED_CONCURRENTLY"
    assert store.doc(ORDERS, ORDER)["status"] == "dispatched"
    counted = [m for m in sink.metrics if m[0] == endpoints.GUARD_REFUSAL_METRIC]
    assert len(counted) == 3
    assert counted[0][2] == {
        "tenant_id": T,
        "reason": "OrderChangedConcurrentlyError",
        "read_source": "documents",
    }
    # A fresh read heals it.
    monkeypatch.undo()
    assert _post(app).status_code == 200


def test_transition_after_board_relink_succeeds_first_try_with_stale_projection(monkeypatch):
    """P1-5 option A: the board relinks the order onto this driver's new run and
    the best-effort mirror fails, so the projection (``get``) still holds the
    pre-relink copy. The endpoint reads ``get_current`` and the guard compares
    against the same document, so the first attempt succeeds with no refusal
    and the projection is never read."""
    import asyncio

    import telemetry.service as telemetry_service
    from driver.services.order_transition_service import get_work_ref_resolver

    app, store, _ = _harness()
    repo = get_work_ref_resolver()._order_repository
    projection = dict(store.doc(ORDERS, ORDER))
    verdict = asyncio.run(
        repo.relink_dispatched_assignment(
            T,
            ORDER,
            from_run_id=AGENT_RUN,
            from_asset_id="truck-1",
            from_driver_id=DRIVER,
            to_run_id=BOARD_RUN,
            to_asset_id="truck-2",
            to_driver_id=DRIVER,
            claim_id="claim-relink-1",
        )
    )
    assert verdict == "relinked"
    relinked_ts = store.doc(ORDERS, ORDER)["last_event_timestamp"]
    assert relinked_ts != projection["last_event_timestamp"]
    # The board plan for the new run is published, so gate 0 lets it start.
    store.seed(
        PLANS,
        BOARD_RUN,
        {"plan_id": BOARD_RUN, "run_id": BOARD_RUN, "tenant_id": T,
         "truck_id": "truck-2", "status": "dispatched", "source": "dispatch_board"},
    )

    projection_reads: list[str] = []

    async def stale_projection(tenant_id, order_id):
        projection_reads.append(order_id)
        return dict(projection)

    monkeypatch.setattr(repo, "get", stale_projection)
    sink = _Metrics()
    monkeypatch.setattr(telemetry_service, "get_telemetry_service", lambda: sink)

    resp = _post(app)
    assert resp.status_code == 200, resp.text
    doc = store.doc(ORDERS, ORDER)
    assert doc["status"] == "in_transit"
    assert (doc["assigned_run_id"], doc["assigned_asset_id"]) == (BOARD_RUN, "truck-2")
    assert [e["event_type"] for e in store.events()] == ["order_in_transit"]
    assert projection_reads == []
    from driver.api.transition_endpoints import GUARD_REFUSAL_METRIC

    assert [m for m in sink.metrics if m[0] == GUARD_REFUSAL_METRIC] == []


def test_unguarded_happy_path_still_transitions():
    app, store, _ = _harness()
    resp = _post(app)
    assert resp.status_code == 200, resp.text
    assert store.doc(ORDERS, ORDER)["status"] == "in_transit"
    assert [e["event_type"] for e in store.events()] == ["order_in_transit"]


# ---------------------------------------------------------------------------
# Gate 0: board plan dispatched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plan_status", ["draft", "scheduled", "superseded"])
def test_board_run_with_unpublished_plan_is_409(plan_status):
    app, store, reader = _harness(run_id=BOARD_RUN, plan_status=plan_status)
    resp = _post(app)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "BOARD_ROUTE_UPDATING"
    assert body["message"] == "Your dispatcher is updating this route. Try again in a moment."
    assert reader.calls == [(PLANS, BOARD_RUN)]
    assert store.doc(ORDERS, ORDER)["status"] == "dispatched"


@pytest.mark.parametrize("plan_status", ["dispatched", "completed"])
def test_board_run_with_dispatched_or_completed_plan_succeeds(plan_status):
    app, store, reader = _harness(run_id=BOARD_RUN, plan_status=plan_status)
    resp = _post(app)
    assert resp.status_code == 200, resp.text
    assert store.doc(ORDERS, ORDER)["status"] == "in_transit"


def test_agent_run_is_never_read():
    app, store, reader = _harness(run_id=AGENT_RUN)
    assert _post(app).status_code == 200
    assert reader.calls == []


def test_board_run_read_error_is_409(caplog):
    app, store, reader = _harness(run_id=BOARD_RUN, plan_status="dispatched", fail_read=True)
    with caplog.at_level(logging.WARNING, logger="driver.services.order_transition_service"):
        resp = _post(app)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "BOARD_ROUTE_UPDATING"
    assert any("read failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("target, start", [("failed", "dispatched"), ("delivered", "in_transit")])
def test_failed_and_delivered_are_never_gated(target, start):
    app, store, reader = _harness(
        run_id=BOARD_RUN, plan_status="superseded", status=start, fail_read=True
    )
    resp = _post(app, status=target)
    assert resp.status_code == 200, resp.text
    assert reader.calls == []


def test_non_board_plan_on_bp_prefix_passes():
    # Only a plan written by the board is gated.
    app, store, reader = _harness(run_id=BOARD_RUN)
    store.seed(PLANS, BOARD_RUN, {"plan_id": BOARD_RUN, "tenant_id": T, "status": "proposed"})
    assert _post(app).status_code == 200


def test_foreign_tenant_plan_is_ignored():
    app, store, reader = _harness(run_id=BOARD_RUN)
    store.seed(
        PLANS,
        BOARD_RUN,
        {"plan_id": BOARD_RUN, "tenant_id": "tenant-2", "status": "draft", "source": "dispatch_board"},
    )
    assert _post(app).status_code == 200


# ---------------------------------------------------------------------------
# Freeze rule 11 (a): the gate can't be skipped by wiring
# ---------------------------------------------------------------------------


def _order(run_id: str) -> dict:
    return {"order_id": ORDER, "assigned_run_id": run_id, "assigned_asset_id": "truck-1"}


@pytest.mark.asyncio
async def test_stack_without_reader_refuses_board_run(caplog):
    stack = DriverTransitionGateStack()
    with caplog.at_level(logging.WARNING, logger="driver.services.order_transition_service"):
        with pytest.raises(AppException) as raised:
            await stack.evaluate(
                tenant_id=T, driver_id=DRIVER, order=_order(BOARD_RUN), target_status="in_transit"
            )
    assert raised.value.status_code == 409
    assert raised.value.error_code.value == "BOARD_ROUTE_UPDATING"
    assert any("no reader" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_stack_without_reader_allows_agent_run():
    stack = DriverTransitionGateStack()
    evaluation = await stack.evaluate(
        tenant_id=T, driver_id=DRIVER, order=_order(AGENT_RUN), target_status="in_transit"
    )
    assert evaluation.allowed
    # The four existing gates are unchanged; gate 0 records no outcome.
    assert [o.gate for o in evaluation.outcomes] == [
        "asset_out_of_service", "pretrip_inspection", "dispatch_eligibility", "hos",
    ]


@pytest.mark.asyncio
async def test_bootstrap_wires_a_board_plan_reader():
    import bootstrap.driver as bootstrap_driver
    from bootstrap.container import ServiceContainer

    class _Store:
        async def get_document(self, index, doc_id):  # pragma: no cover - not called
            return None

    container = ServiceContainer()
    container.es_service = _Store()
    await bootstrap_driver.initialize(FastAPI(), container)

    stack = get_gate_stack()
    assert stack is not None
    assert stack._board_plan_reader is not None
    assert stack._board_plan_reader == container.es_service.get_document
