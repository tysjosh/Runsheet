"""Driver manifest resolved by run and status (dispatch-board K12, R25.1, R25.2).

``DriverWorkService._fetch_loading_plan`` used to return the newest plan for
the truck in any status. A truck with several loads in a day, or with a newer
``proposed`` agent plan, then showed the driver the wrong manifest. These tests
run the real query against ``persistence.document_matcher`` through the shared
in-memory store, so the filter itself is exercised, not a canned response.
"""
from __future__ import annotations

import pytest

from Agents.support.mvp_es_mappings import MVP_LOAD_PLANS_INDEX
from driver.services.work_service import DriverWorkService
from tests.unit._loading_plan_fakes import InMemoryDocumentStore

TENANT = "t1"
TRUCK = "truck-1"


def _plan(plan_id, *, status, created_at, run_id=None, truck_id=TRUCK, tenant_id=TENANT):
    doc = {
        "plan_id": plan_id,
        "tenant_id": tenant_id,
        "truck_id": truck_id,
        "status": status,
        "created_at": created_at,
        "assignments": [{"compartment_id": "c1", "order_id": "o1"}],
    }
    if run_id is not None:
        doc["run_id"] = run_id
    return doc


def _service(*plans) -> DriverWorkService:
    store = InMemoryDocumentStore()
    for plan in plans:
        store.seed(MVP_LOAD_PLANS_INDEX, plan["plan_id"], plan)
    return DriverWorkService(es_service=store)


@pytest.mark.asyncio
async def test_proposed_plan_is_ignored():
    service = _service(
        _plan("p-old", status="dispatched", created_at="2026-10-01T08:00:00Z", run_id="run-1"),
        _plan("p-new", status="proposed", created_at="2026-10-01T09:00:00Z", run_id="run-1"),
    )
    plan = await service._fetch_loading_plan(TENANT, TRUCK, "run-1")
    assert plan["plan_id"] == "p-old"


@pytest.mark.asyncio
async def test_superseded_plan_is_ignored():
    service = _service(
        _plan("bp-l1-r1", status="dispatched", created_at="2026-10-01T08:00:00Z"),
        _plan("bp-l1-r2", status="superseded", created_at="2026-10-01T09:00:00Z"),
    )
    assert (await service._fetch_loading_plan(TENANT, TRUCK, "bp-l1-r1"))["plan_id"] == "bp-l1-r1"
    # The superseded revision is never returned, even when it is the run asked for.
    assert await service._fetch_loading_plan(TENANT, TRUCK, "bp-l1-r2") is None


@pytest.mark.asyncio
async def test_run_match_beats_newer_plan_on_same_truck():
    service = _service(
        _plan("p-load1", status="dispatched", created_at="2026-10-01T06:00:00Z", run_id="run-a"),
        _plan("p-load2", status="dispatched", created_at="2026-10-01T11:00:00Z", run_id="run-b"),
    )
    plan = await service._fetch_loading_plan(TENANT, TRUCK, "run-a")
    assert plan["plan_id"] == "p-load1"


@pytest.mark.asyncio
async def test_run_id_matches_plan_id_when_plan_has_no_run_id():
    # Board plans use run_id == plan_id; an executor run id may be the plan id.
    service = _service(
        _plan("bp-l9-r1", status="scheduled", created_at="2026-10-01T06:00:00Z"),
        _plan("p-other", status="dispatched", created_at="2026-10-01T11:00:00Z", run_id="run-z"),
    )
    plan = await service._fetch_loading_plan(TENANT, TRUCK, "bp-l9-r1")
    assert plan["plan_id"] == "bp-l9-r1"


@pytest.mark.asyncio
async def test_completed_plan_still_resolves():
    service = _service(
        _plan("p-done", status="completed", created_at="2026-10-01T06:00:00Z", run_id="run-1"),
    )
    assert (await service._fetch_loading_plan(TENANT, TRUCK, "run-1"))["plan_id"] == "p-done"


@pytest.mark.asyncio
async def test_empty_run_keeps_newest_by_truck_with_status_filter():
    service = _service(
        _plan("p-1", status="scheduled", created_at="2026-10-01T06:00:00Z", run_id="r1"),
        _plan("p-2", status="dispatched", created_at="2026-10-01T07:00:00Z", run_id="r2"),
        _plan("p-3", status="proposed", created_at="2026-10-01T08:00:00Z", run_id="r3"),
        _plan("p-4", status="dispatched", created_at="2026-10-01T09:00:00Z", truck_id="truck-2"),
    )
    plan = await service._fetch_loading_plan(TENANT, TRUCK, "")
    assert plan["plan_id"] == "p-2"


@pytest.mark.asyncio
async def test_no_asset_means_no_plan_and_no_read():
    store = InMemoryDocumentStore()
    service = DriverWorkService(es_service=store)
    assert await service._fetch_loading_plan(TENANT, "", "run-1") is None
    assert store.calls("search_documents") == []


@pytest.mark.asyncio
async def test_foreign_tenant_plan_is_never_returned():
    service = _service(
        _plan("p-foreign", status="dispatched", created_at="2026-10-01T06:00:00Z",
              run_id="run-1", tenant_id="t2"),
    )
    assert await service._fetch_loading_plan(TENANT, TRUCK, "run-1") is None


@pytest.mark.asyncio
async def test_detail_resolution_passes_the_orders_run():
    service = DriverWorkService(es_service=InMemoryDocumentStore())
    seen = {}

    async def _spy(tenant_id, asset_id, run_id=""):
        seen["args"] = (tenant_id, asset_id, run_id)
        return None

    service._fetch_loading_plan = _spy  # type: ignore[method-assign]
    await service._resolve_bundle_uncached(
        TENANT, {"assigned_asset_id": TRUCK, "assigned_run_id": "run-7"}
    )
    assert seen["args"] == (TENANT, TRUCK, "run-7")
