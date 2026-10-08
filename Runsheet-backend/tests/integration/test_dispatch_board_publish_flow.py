"""Dispatch Board end to end, in process (plan task 17; R12.4, R12.7, R12.9, R13.4,
R13.8, R13.10, R13.11, R25.1).

One FastAPI app with the real routers (board, driver work, driver transition,
MVP check-in) over one in-memory document store, with the real executor,
dispatch, plan execution, driver work and order services behind them and fake
WS managers. No staging, network or Redis: the driver work cache is an
in-memory fake, so the test also proves the re-publish invalidates it.

Flow: snapshot → commands → publish → work reads → re-publish (a dispatched
stop moved into a never-published lane, only that lane published) → a second
re-publish during which the driver starts an order between retire and relink
through the real transition endpoint (gate and guard), which rolls back.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import Agents.support.mvp_endpoints as mvp
import driver.middleware.idempotency as idempotency_module
from driver.api import work_endpoints
from driver.api.transition_endpoints import router as transition_router
from driver.middleware.idempotency import configure_idempotency_middleware
from driver.services.order_transition_service import configure_transition_endpoints
from errors.handlers import register_exception_handlers
from fuel.api import dispatch_board_endpoints as board_api
from tests.support.auth_seam import auth_headers, install_test_auth
from tests.unit._dispatch_board_fakes import ORDERS, T, TODAY, FakeFlags
from tests.unit._dispatch_board_publish_world import PLANS, FakeRedis, World

BASE = f"/api/fuel/board/{TODAY.isoformat()}"
DISPATCHER = auth_headers(T, sub="user-1", roles=["dispatcher"])


def driver_headers(driver_id: str) -> Dict[str, str]:
    return auth_headers(T, sub=driver_id, roles=["driver"], driver_id=driver_id)


class _IdempotencyStore:
    def __init__(self) -> None:
        self.docs: Dict[str, Any] = {}

    async def index_document(self, index, doc_id, document):
        self.docs[doc_id] = document

    async def get_document(self, index, doc_id):
        return self.docs.get(doc_id)


@pytest.fixture
def env():
    w = World()
    store = w.store
    flags = FakeFlags({T: "active_gated"})
    for truck, (lat, lon) in {"T1": (41.85, -87.65), "T2": (41.80, -87.70)}.items():
        store.seed("truck_telemetry", f"tel-{truck}", {"tenant_id": T, "truck_id": truck, "location_lat": lat, "location_lon": lon, "recorded_at": "2026-10-08T14:00:00+00:00"})
    for driver_id, truck in (("d1", "T1"), ("d2", "T2")):
        store.seed("drivers_current", driver_id, {"tenant_id": T, "driver_id": driver_id, "driver_name": f"Driver {driver_id}", "assigned_truck_id": truck, "status": "active"})
    for i, order_id in enumerate(("o1", "o2", "o3")):
        w.seed(order_id)
        store.seed("customer_tanks", f"tank-{order_id}", {"tenant_id": T, "customer_tank_id": f"tank-{order_id}", "location_lat": 41.90 + i / 100, "location_lon": -87.60 - i / 100})

    previous_mvp = {name: getattr(mvp, name) for name in ("_pipeline", "_es_service", "_plan_execution_service", "_plan_execution_ws_manager", "_plan_dispatch_service")}
    previous_idem = idempotency_module.get_idempotency_middleware()
    configure_idempotency_middleware(es_service=_IdempotencyStore())
    mvp.configure_mvp_endpoints(pipeline=None, es_service=store, plan_execution_service=w.executions, plan_execution_ws_manager=AsyncMock())
    work_endpoints.configure_work_endpoints(es_service=store, order_repository=w.repo, redis_client=FakeRedis())
    w.work = work_endpoints.get_work_service()  # the redispatch invalidator now drops the live cache
    configure_transition_endpoints(order_repository=w.repo, order_service=w.order_service, board_plan_reader=store.get_document)
    board_api.configure_dispatch_board_endpoints(board_service=w.h.service, feature_flag_service=flags, publish_service=w.publish)

    app = FastAPI()
    register_exception_handlers(app)
    for router in (board_api.router, work_endpoints.router, transition_router, mvp.router):
        app.include_router(router)
    install_test_auth(app)
    try:
        yield w, flags, app
    finally:
        board_api.configure_dispatch_board_endpoints(board_service=None, feature_flag_service=None)
        configure_transition_endpoints()
        work_endpoints.configure_work_endpoints()
        mvp.configure_mvp_endpoints(
            pipeline=previous_mvp["_pipeline"], es_service=previous_mvp["_es_service"],
            plan_execution_service=previous_mvp["_plan_execution_service"],
            plan_execution_ws_manager=previous_mvp["_plan_execution_ws_manager"],
            plan_dispatch_service=previous_mvp["_plan_dispatch_service"],
        )
        idempotency_module._idempotency_middleware = previous_idem


async def _command(client, type_: str, versions: Dict[str, int], **fields) -> Dict[str, Any]:
    body = {"type": type_, "client_command_id": str(uuid.uuid4()), "expected_lane_versions": versions, **fields}
    resp = await client.post(f"{BASE}/commands", json=body, headers=DISPATCHER)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _versions(w: World, *trucks: str) -> Dict[str, int]:
    draft = w.h.draft()
    return {t: (draft.lanes[t].version if t in draft.lanes else 0) for t in trucks}


async def _publish(client, w: World, *trucks: str) -> Dict[str, Any]:
    lanes = [{"truck_id": t, "expected_version": v} for t, v in _versions(w, *trucks).items()]
    preview = await client.post(f"{BASE}/publish", json={"dry_run": True, "lanes": lanes}, headers=DISPATCHER)
    assert preview.status_code == 200, preview.text
    reasons = {x["warning_id"]: "Checked with the driver." for x in preview.json()["data"]["open_warnings"]}
    resp = await client.post(
        f"{BASE}/publish",
        json={"client_request_id": str(uuid.uuid4()), "lanes": lanes, "warning_reasons": reasons},
        headers=DISPATCHER,
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]


async def _work(client, driver_id: str, order_id: str):
    return await client.get(f"/api/driver/work/{order_id}", headers=driver_headers(driver_id))


def _assert_full_stops(data: Dict[str, Any], n_assignments: int) -> None:
    assert data["manifest_available"] is True and data["route_available"] is True
    assert len(data["compartment_manifest"]) == n_assignments
    for stop in data["stops"]:
        assert stop["lat"] is not None and stop["lon"] is not None, stop
        assert stop["planned_arrival"] is not None, stop
        assert stop["planned_gallons_by_grade"], stop


async def _checkin(client, driver_id: str, plan_id: str, route: Dict[str, Any], order_id: str):
    stop = next(s for s in route["stops"] if order_id in s["order_ids"])
    body = {
        "route_id": route["route_id"],
        "station_id": stop["station_id"],
        "sequence": stop["sequence"],
        "actual_quantities_gallons": {"DIESEL_2": 480.0},
        "quantity_unit": "us_gallon",
        "geotag": {"lat": 41.9, "lng": -87.6},
        "event_timestamp": "2026-10-08T16:00:00+00:00",
        "order_id": order_id,
    }
    return await client.post(f"/api/fuel/mvp/plan/{plan_id}/checkin", json=body, headers=driver_headers(driver_id))


async def test_snapshot_commands_publish_republish_and_rollback_in_process(env):
    w, flags, app = env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://board") as client:
        # -- snapshot and commands -------------------------------------------
        snap = await client.get(BASE, headers=DISPATCHER)
        assert snap.status_code == 200
        assert {o["order_id"] for o in snap.json()["data"]["trays"]["orders"]} == {"o1", "o2", "o3"}
        await _command(client, "add_lane", {"T1": 0}, truck_id="T1")
        await _command(client, "pair_driver", _versions(w, "T1"), truck_id="T1", driver_id="d1")
        await _command(client, "assign_orders", _versions(w, "T1"), order_ids=["o1", "o2"], truck_id="T1", target={"load_id": "new"})
        await _command(client, "add_lane", {"T2": 0}, truck_id="T2")
        await _command(client, "pair_driver", _versions(w, "T2"), truck_id="T2", driver_id="d2")

        # -- publish (E15: the flag goes off mid-publish, the worker drains) ---
        accepted = await _publish(client, w, "T1")
        assert accepted["lanes"] == [{"truck_id": "T1", "state": "queued"}]
        flags.states[T] = "disabled"
        await w.publish.wait_idle()
        refused = await client.post(f"{BASE}/publish", json={"dry_run": True, "lanes": [{"truck_id": "T1", "expected_version": 0}]}, headers=DISPATCHER)
        assert refused.status_code == 404
        flags.states[T] = "active_gated"
        status = await client.get(f"{BASE}/publish/{accepted['publish_id']}", headers=DISPATCHER)
        assert status.json()["data"]["done"] is True and status.json()["data"]["lanes"][0]["state"] == "published"

        a1 = w.plan_of("T1")
        for order_id in ("o1", "o2"):
            order = w.order(order_id)
            assert order["status"] == "dispatched" and w.links(order_id) == (a1.run_id, "T1", "d1")
            assert any(e["event_type"] == "order_dispatched" for e in w.store.events(order_id))
        assert w.plan(a1.plan_id)["status"] == "dispatched" and w.route(a1.route_id)["status"] == "dispatched"
        assert len(w.executions_of(a1.plan_id)) == 1
        for order_id in ("o1", "o2"):
            resp = await _work(client, "d1", order_id)
            assert resp.status_code == 200, resp.text
            _assert_full_stops(resp.json()["data"], 2)
        assert [c[0] for c in w.driver_ws.calls] == ["assignment"]

        # -- re-publish: o1 moves into never-published T2; only T2 is published --
        await _command(client, "move_stops", _versions(w, "T1", "T2"), order_ids=["o1"], truck_id="T2", target={"load_id": "new"})
        w.driver_ws.calls.clear()
        accepted = await _publish(client, w, "T2")
        assert accepted["groups"] == [{"truck_ids": ["T2", "T1"], "kind": "redispatch", "added_lanes": ["T1"]}]
        await w.publish.wait_idle()
        b1, a2 = w.plan_of("T2"), w.plan_of("T1")
        assert w.links("o1") == (b1.run_id, "T2", "d2") and w.links("o2") == (a2.run_id, "T1", "d1")
        assert a2.revision == 2 and w.plan(a1.plan_id)["status"] == "superseded"
        assert any(e["event_type"] == "order_reassigned" for e in w.store.events("o1"))
        assert (await _work(client, "d1", "o1")).status_code == 404  # the order left A's driver
        resp = await _work(client, "d2", "o1")
        assert resp.status_code == 200
        _assert_full_stops(resp.json()["data"], 1)
        resp = await _work(client, "d1", "o2")  # was cached under the old run: invalidated
        data = resp.json()["data"]
        _assert_full_stops(data, 1)
        assert len(data["stops"]) == 1
        assert [c[0] for c in w.driver_ws.calls] == ["assignment_revoked", "assignment", "assignment"]

        # -- second re-publish: the driver starts o2 between retire and relink ---
        await _command(client, "move_stops", _versions(w, "T1", "T2"), order_ids=["o2"], truck_id="T2", target={"load_id": w.lane_doc("T2").loads[0].load_id})
        at_write, retired = asyncio.Event(), asyncio.Event()
        paused = {"done": False}

        async def pause_driver_write(op, index, doc_id):
            if (op, index, doc_id) == ("atomic_update", ORDERS, "o2") and not paused["done"] and at_write.is_set() is False and request_task is not None:
                paused["done"] = True
                at_write.set()
                await retired.wait()

        request_task = None
        w.store.hooks.append(pause_driver_write)
        superseded_checkin: Dict[str, Any] = {}

        async def on_retire():
            nonlocal request_task
            request_task = asyncio.ensure_future(
                client.post("/api/driver/orders/o2/status", json={"status": "in_transit"}, headers=driver_headers("d1"))
            )
            await at_write.wait()  # the gate read the plan as dispatched; the write waits

        async def on_stage():
            # A check-in against a retired execution gets today's "not dispatched" response.
            resp = await _checkin(client, "d2", b1.plan_id, w.route(b1.route_id), "o1")
            superseded_checkin["status"], superseded_checkin["body"] = resp.status_code, resp.json()
            retired.set()
            superseded_checkin["driver"] = await request_task

        hooks = {"retire": on_retire, "stage_relink": on_stage}

        async def on_phase(name, info):
            if name in hooks:
                await hooks.pop(name)()

        w.redispatch.on_phase = on_phase
        w.driver_ws.calls.clear()
        await _publish(client, w, "T2")
        await w.publish.wait_idle()
        w.redispatch.on_phase = None
        w.store.hooks.clear()

        assert superseded_checkin["driver"].status_code == 200, superseded_checkin["driver"].text
        assert w.order("o2")["status"] == "in_transit"
        assert superseded_checkin["status"] == 409 and "superseded" in superseded_checkin["body"]["message"]
        for truck in ("T1", "T2"):
            result = w.lane_doc(truck).publish.last_result
            assert (result.stage, result.rolled_back, result.order_id) == ("relink", True, "o2")
        assert w.plan(a2.plan_id)["status"] == "dispatched" and w.route(a2.route_id)["status"] == "dispatched"
        assert w.plan(b1.plan_id)["status"] == "dispatched"
        assert w.executions_of(a2.plan_id)[0]["status"] == "in_progress"
        assert w.links("o2") == (a2.run_id, "T1", "d1") and w.links("o1") == (b1.run_id, "T2", "d2")
        assert w.driver_ws.calls == []
        resp = await _work(client, "d1", "o2")
        assert resp.status_code == 200 and resp.json()["data"]["manifest_available"] is True
        checked = await _checkin(client, "d1", a2.plan_id, w.route(a2.route_id), "o2")
        assert checked.status_code == 200, checked.text
        assert checked.json()["all_complete"] is True
        # The refused check-in's driver recovers from the work read: the restored route.
        resp = await _work(client, "d2", "o1")
        assert resp.status_code == 200 and resp.json()["data"]["manifest_available"] is True
        assert [s["status"] for s in resp.json()["data"]["stops"]] == ["pending"]
        assert w.store.docs["agent_approval_queue"] == {}
        assert w.plan(a2.plan_id)["source"] == "dispatch_board" and PLANS
