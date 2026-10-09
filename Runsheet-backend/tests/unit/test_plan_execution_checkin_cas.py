"""Check-in is compare-and-set on the execution and the plan (freeze rules 9, 11 (b)).

``record_checkin`` used to edit the execution read at the start of the request
and persist it with ``update_document``. A board retire or amend that landed in
between was overwritten. It now persists through ``atomic_update`` with a
transform that re-locates the stop on the stored document and refuses on a
superseded execution, a missing stop or a completed stop. The plan writes on
the check-in path (the ``completed`` status and both cost figures) merge only
their own fields under the row lock, so a board revision written in between
survives.

Interleavings are injected with the in-memory store's hooks: a hook runs
before the named store operation, i.e. after the service's reads and before
its commit.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.support.mvp_endpoints import configure_mvp_endpoints, router
from Agents.support.plan_execution_service import PlanExecutionService
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit._loading_plan_fakes import InMemoryDocumentStore

T = "tenant-1"
PLAN = "bp-load1-r1"
ROUTE = "br-load1-r1"
EXEC = "exec-1"
PLANS = "mvp_load_plans"
EXECS = "mvp_plan_executions"


def _stop(seq, station, status="pending"):
    return {
        "station_id": station,
        "sequence": seq,
        "status": status,
        "planned_eta": "2026-10-06T09:00:00+00:00",
        "actual_arrival": None,
        "planned_quantities": {"DIESEL_2": 1000.0},
        "actual_quantities": {},
        "actual_quantities_unit": "liter",
    }


def _store(*, stops=None, plan_extra=None) -> InMemoryDocumentStore:
    store = InMemoryDocumentStore()
    stops = stops if stops is not None else [_stop(1, "st-a"), _stop(2, "st-b")]
    plan = {
        "plan_id": PLAN,
        "tenant_id": T,
        "truck_id": "truck-1",
        "status": "dispatched",
        "assignments": [{"order_id": "o1", "compartment_id": "c1"}],
    }
    plan.update(plan_extra or {})
    store.seed(PLANS, PLAN, plan)
    store.seed(
        EXECS,
        EXEC,
        {
            "execution_id": EXEC,
            "plan_id": PLAN,
            "route_id": ROUTE,
            "tenant_id": T,
            "stops": stops,
            "completed_stops": sum(1 for s in stops if s["status"] == "completed"),
            "total_stops": len(stops),
            "status": "in_progress",
        },
    )
    return store


def _before_commit(store, index, doc_id, action):
    """Run ``action`` once, just before the first ``atomic_update`` on the doc."""
    seen = []

    def hook(op, idx, did):
        if (op, idx, did) == ("atomic_update", index, doc_id) and not seen:
            seen.append(1)
            action()

    store.hooks.append(hook)


async def _checkin(service, seq=1, station="st-a"):
    return await service.record_checkin(
        plan_id=PLAN,
        route_id=ROUTE,
        station_id=station,
        sequence=seq,
        actual_quantities={"DIESEL_2": 990.0},
        tenant_id=T,
        driver_id=None,
        geotag={"lat": 1.0, "lon": 2.0},
        event_timestamp="2026-10-06T09:05:00+00:00",
    )


# ---------------------------------------------------------------------------
# record_checkin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_writes_through_atomic_update():
    store = _store()
    result = await _checkin(PlanExecutionService(store))
    assert result["completed_stops"] == 1 and result["total_stops"] == 2
    assert result["all_complete"] is False
    assert store.calls("update_document", EXECS) == []
    assert store.write_count("atomic_update", EXECS) == 1
    doc = store.doc(EXECS, EXEC)
    stop = next(s for s in doc["stops"] if s["sequence"] == 1)
    assert stop["status"] == "completed"
    assert stop["actual_quantities"] == {"DIESEL_2": 990.0}
    assert stop["quantity_variance"] == {"DIESEL_2": -10.0}
    assert stop["variance_unit"] == "liter"
    assert doc["status"] == "in_progress"


@pytest.mark.asyncio
async def test_read_then_supersede_then_commit_is_refused():
    store = _store()
    _before_commit(store, EXECS, EXEC, lambda: store.poke(EXECS, EXEC, status="superseded"))
    with pytest.raises(ValueError) as raised:
        await _checkin(PlanExecutionService(store))
    assert "not in 'dispatched' status (current: superseded)" in str(raised.value)
    doc = store.doc(EXECS, EXEC)
    assert doc["status"] == "superseded"
    assert all(s["status"] == "pending" for s in doc["stops"])
    assert doc["completed_stops"] == 0


@pytest.mark.asyncio
async def test_read_then_renumber_then_commit_is_404_and_renumbered_stops_survive():
    store = _store()
    renumbered = [_stop(11, "st-a"), _stop(12, "st-b")]
    _before_commit(store, EXECS, EXEC, lambda: store.poke(EXECS, EXEC, stops=renumbered))
    with pytest.raises(AppException) as raised:
        await _checkin(PlanExecutionService(store))
    assert raised.value.status_code == 404
    assert raised.value.details["sequence"] == 1
    assert store.doc(EXECS, EXEC)["stops"] == renumbered


@pytest.mark.asyncio
async def test_double_checkin_is_409():
    store = _store()
    service = PlanExecutionService(store)
    await _checkin(service)
    with pytest.raises(AppException) as raised:
        await _checkin(service)
    assert raised.value.status_code == 409
    assert raised.value.error_code.value == "STOP_ALREADY_COMPLETED"
    assert store.doc(EXECS, EXEC)["completed_stops"] == 1


@pytest.mark.asyncio
async def test_interleaved_double_checkin_commits_once():
    """Both requests read the stop pending; the second commit sees it completed."""
    store = _store()
    service = PlanExecutionService(store)
    first_done = {}

    async def other_checkin_first(op, idx, did, seen=[]):
        if (op, idx, did) == ("atomic_update", EXECS, EXEC) and not seen:
            seen.append(1)
            store.hooks.clear()
            first_done["result"] = await _checkin(service)

    store.hooks.append(other_checkin_first)
    with pytest.raises(AppException) as raised:
        await _checkin(service)
    assert raised.value.status_code == 409
    assert first_done["result"]["completed_stops"] == 1
    assert store.doc(EXECS, EXEC)["completed_stops"] == 1


@pytest.mark.asyncio
async def test_completed_stops_counts_from_the_stored_document():
    store = _store()
    # Another stop completed after this request's read.
    _before_commit(
        store,
        EXECS,
        EXEC,
        lambda: store.poke(
            EXECS, EXEC, stops=[_stop(1, "st-a"), _stop(2, "st-b", "completed")], completed_stops=1
        ),
    )
    result = await _checkin(PlanExecutionService(store))
    assert result["completed_stops"] == 2
    assert result["all_complete"] is True
    assert store.doc(EXECS, EXEC)["status"] == "completed"
    assert [s["status"] for s in store.doc(EXECS, EXEC)["stops"]] == ["completed", "completed"]


# ---------------------------------------------------------------------------
# Freeze rule 11 (b): plan writes on the check-in path
# ---------------------------------------------------------------------------


def _app(store) -> FastAPI:
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=T, user_id="u-1", has_pii_access=True, roles=["dispatcher"], driver_id=None
    )
    ws = MagicMock()
    ws.broadcast_execution_update = AsyncMock()
    configure_mvp_endpoints(
        pipeline=MagicMock(),
        es_service=store,
        plan_execution_service=PlanExecutionService(store),
        plan_execution_ws_manager=ws,
    )
    return app


@pytest.fixture(autouse=True)
def _no_idempotency_singleton():
    from driver.middleware import idempotency as idem

    original = idem._idempotency_middleware
    idem._idempotency_middleware = None
    yield
    idem._idempotency_middleware = original


def test_board_revision_written_between_read_and_completion_survives():
    store = _store(stops=[_stop(1, "st-a")])
    new_assignments = [
        {"order_id": "o1", "compartment_id": "c1"},
        {"order_id": "o2", "compartment_id": "c2"},
    ]
    # The endpoint has read the plan (inside record_checkin); the board's
    # revisioned plan write commits; then the completion commits.
    _before_commit(
        store,
        PLANS,
        PLAN,
        lambda: store.poke(PLANS, PLAN, revision=2, assignments=new_assignments),
    )
    resp = TestClient(_app(store)).post(
        f"/api/fuel/mvp/plan/{PLAN}/checkin",
        json={
            "route_id": ROUTE,
            "station_id": "st-a",
            "sequence": 1,
            "actual_quantities_gallons": {"DIESEL_2": 100.0},
            "quantity_unit": "us_gallon",
            "geotag": {"lat": 1.0, "lng": 2.0},
            "event_timestamp": "2026-10-06T09:05:00+00:00",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["all_complete"] is True
    plan = store.doc(PLANS, PLAN)
    assert plan["status"] == "completed"
    assert plan["revision"] == 2
    assert plan["assignments"] == new_assignments
    assert store.calls("update_document", PLANS) == []


@pytest.mark.asyncio
async def test_cost_writes_merge_only_their_fields():
    store = _store(plan_extra={"estimated_cost": {"total_estimated_cost": 100.0}})
    store.seed("mvp_cost_configs", T, {"_source": {
        "tenant_id": T, "fuel_consumption_rate": 0.3, "fuel_price_per_liter": 1.0,
        "driver_hourly_rate": 20.0, "currency": "USD",
    }})
    store.seed("mvp_routes", ROUTE, {
        "route_id": ROUTE, "plan_id": PLAN, "tenant_id": T, "distance_km": 10.0,
        "stops": [{"sequence": 1, "station_id": "st-a", "eta": "2026-10-06T09:00:00+00:00"}],
    })
    service = PlanExecutionService(store)
    _before_commit(store, PLANS, PLAN, lambda: store.poke(PLANS, PLAN, revision=3))
    await service.compute_estimated_cost(PLAN, T)
    _before_commit(store, PLANS, PLAN, lambda: store.poke(PLANS, PLAN, supersedes_plan_id="bp-load1-r0"))
    await service.compute_actual_cost(PLAN, T)
    plan = store.doc(PLANS, PLAN)
    assert plan["revision"] == 3
    assert plan["supersedes_plan_id"] == "bp-load1-r0"
    assert "estimated_cost" in plan and "actual_cost" in plan and "cost_variance_pct" in plan
    assert plan["status"] == "dispatched"
    assert store.calls("update_document", PLANS) == []
