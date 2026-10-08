"""A check-in and a board execution rewrite on a real PostgreSQL (freeze rule 9).

``record_checkin`` and the board's amend both write ``mvp_plan_executions``
through ``atomic_update`` (``SELECT ... FOR UPDATE``). Run concurrently, they
serialize: either the check-in commits first and the amend sees (and keeps)
the completed stop, or the amend commits first and the check-in is refused
because its stop was renumbered. They never both apply to the same version.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from Agents.support.plan_execution_service import PlanExecutionService
from errors.exceptions import AppException
from tests.postgres.test_loading_plan_executor_postgres import _Namespaced

pytestmark = pytest.mark.asyncio

PLANS = "mvp_load_plans"
EXECS = "mvp_plan_executions"


def _stop(seq, station):
    return {
        "station_id": station,
        "sequence": seq,
        "status": "pending",
        "planned_eta": "2026-10-06T09:00:00+00:00",
        "actual_arrival": None,
        "planned_quantities": {"DIESEL_2": 1000.0},
        "actual_quantities": {},
        "actual_quantities_unit": "liter",
    }


def _amend(current):
    """The board's amend shape: recompute from the stored doc, renumber pending
    stops above the max sequence, keep completed stops as they are (P9)."""
    stops = list(current.get("stops") or [])
    top = max(int(s["sequence"]) for s in stops)
    out = []
    for stop in stops:
        if stop["status"] == "pending":
            top += 1
            stop = {**stop, "sequence": top}
        out.append(stop)
    return {**current, "stops": out, "amended": True}


@pytest.mark.parametrize("attempt", range(6))
async def test_checkin_and_amend_never_both_apply(store, index_name, attempt):
    ns = _Namespaced(store, index_name)
    tenant = f"pytest-checkin-{uuid.uuid4().hex[:10]}"
    plan_id = f"bp-l-{attempt}-r1"
    exec_id = f"exec-{uuid.uuid4().hex[:8]}"
    await ns.index_document(
        PLANS, plan_id, {"plan_id": plan_id, "tenant_id": tenant, "truck_id": "t1", "status": "dispatched"}
    )
    await ns.index_document(
        EXECS,
        exec_id,
        {
            "execution_id": exec_id,
            "plan_id": plan_id,
            "route_id": "r1",
            "tenant_id": tenant,
            "stops": [_stop(1, "st-a"), _stop(2, "st-b")],
            "completed_stops": 0,
            "total_stops": 2,
            "status": "in_progress",
        },
    )
    service = PlanExecutionService(ns)

    async def checkin():
        try:
            return await service.record_checkin(
                plan_id=plan_id,
                route_id="r1",
                station_id="st-a",
                sequence=1,
                actual_quantities={"DIESEL_2": 990.0},
                tenant_id=tenant,
                geotag={"lat": 1.0, "lon": 2.0},
                event_timestamp="2026-10-06T09:05:00+00:00",
            )
        except AppException as exc:
            return exc

    async def amend():
        if attempt % 2:
            await asyncio.sleep(0)
        return await ns.atomic_update(EXECS, exec_id, _amend)

    checkin_result, (_doc, amend_applied) = await asyncio.gather(checkin(), amend())
    assert amend_applied
    final = await ns.get_document(EXECS, exec_id)
    assert final["amended"] is True

    if isinstance(checkin_result, AppException):
        # Amend first: the stop was renumbered, the check-in found nothing.
        assert checkin_result.status_code == 404
        assert final["completed_stops"] == 0
        assert all(s["status"] == "pending" for s in final["stops"])
        assert sorted(s["sequence"] for s in final["stops"]) == [3, 4]
    else:
        # Check-in first: the amend kept the completed stop at its sequence.
        assert final["completed_stops"] == 1
        done = [s for s in final["stops"] if s["status"] == "completed"]
        assert [(s["station_id"], s["sequence"]) for s in done] == [("st-a", 1)]
        assert [s["sequence"] for s in final["stops"] if s["status"] == "pending"] == [3]
