"""Board plans in existing MVP surfaces (dispatch-board K7.6, R12.10).

Board plans live in ``mvp_load_plans`` / ``mvp_routes`` next to agent plans.
The MVP list hides them unless asked, approve / reject / replan refuse them with
409 ``BOARD_OWNED_PLAN`` before any write, and the exception-replanning agent's
snapshot never picks one up.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.overlay.exception_replanning_agent import ExceptionReplanningAgent
from Agents.support.mvp_endpoints import configure_mvp_endpoints, router
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit._loading_plan_fakes import InMemoryDocumentStore

T = "tenant-1"
PLANS = "mvp_load_plans"
ROUTES = "mvp_routes"


def _plan(plan_id, *, source=None, status="draft", created_at="2026-10-06T06:00:00+00:00"):
    doc = {
        "plan_id": plan_id,
        "tenant_id": T,
        "truck_id": "truck-1",
        "status": status,
        "created_at": created_at,
        "assignments": [],
    }
    if source:
        doc["source"] = source
    return doc


def _store() -> InMemoryDocumentStore:
    store = InMemoryDocumentStore()
    store.seed(PLANS, "agent-1", _plan("agent-1", created_at="2026-10-06T05:00:00+00:00"))
    store.seed(PLANS, "agent-2", _plan("agent-2", created_at="2026-10-06T06:00:00+00:00"))
    store.seed(PLANS, "bp-l1-r1", _plan("bp-l1-r1", source="dispatch_board",
                                        created_at="2026-10-06T07:00:00+00:00"))
    return store


@pytest.fixture
def harness():
    store = _store()
    agent = MagicMock()
    agent._on_signal = AsyncMock()
    agent.monitor_cycle = AsyncMock()
    dispatch = MagicMock()
    dispatch.dispatch = AsyncMock()
    configure_mvp_endpoints(
        pipeline=MagicMock(),
        es_service=store,
        exception_replanning_agent=agent,
        plan_dispatch_service=dispatch,
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=T, user_id="disp-1", has_pii_access=True, roles=["dispatcher"]
    )
    yield TestClient(app), store, agent, dispatch
    configure_mvp_endpoints(pipeline=None, es_service=None)


def _ids(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body.get("items", body.get("data"))
    return [p["plan_id"] for p in items]


def test_list_hides_board_plans_by_default(harness):
    client, *_ = harness
    assert _ids(client.get("/api/fuel/mvp/plans")) == ["agent-2", "agent-1"]


def test_list_with_source_returns_only_board_plans(harness):
    client, *_ = harness
    assert _ids(client.get("/api/fuel/mvp/plans?source=dispatch_board")) == ["bp-l1-r1"]


def test_list_rejects_other_source_values(harness):
    client, *_ = harness
    assert client.get("/api/fuel/mvp/plans?source=agent").status_code == 422


@pytest.mark.parametrize("action", ["approve", "reject", "replan"])
def test_actions_on_board_plan_are_409_and_write_nothing(harness, action):
    client, store, agent, dispatch = harness
    before = len(store.ops)
    resp = client.post(f"/api/fuel/mvp/plan/bp-l1-r1/{action}", json={})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "BOARD_OWNED_PLAN"
    assert body["message"] == "Manage this plan on the Dispatch Board"
    assert body["details"] == {"plan_id": "bp-l1-r1"}
    assert [op for op in store.ops[before:] if op[3]] == []
    dispatch.dispatch.assert_not_awaited()
    agent._on_signal.assert_not_awaited()
    agent.monitor_cycle.assert_not_awaited()
    assert store.doc(PLANS, "bp-l1-r1")["status"] == "draft"


def test_reject_still_works_for_agent_plans(harness):
    client, store, *_ = harness
    resp = client.post("/api/fuel/mvp/plan/agent-1/reject", json={})
    assert resp.status_code == 200, resp.text
    assert store.doc(PLANS, "agent-1")["status"] == "rejected"


def test_replan_still_triggers_for_agent_plans(harness):
    client, _store, agent, _ = harness
    resp = client.post("/api/fuel/mvp/plan/agent-1/replan", json={})
    assert resp.status_code == 200, resp.text
    agent.monitor_cycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_snapshot_ignores_a_newer_board_plan():
    store = InMemoryDocumentStore()
    store.seed(PLANS, "agent-1", _plan("agent-1", status="dispatched",
                                       created_at="2026-10-06T05:00:00+00:00"))
    store.seed(PLANS, "bp-l1-r1", _plan("bp-l1-r1", source="dispatch_board", status="dispatched",
                                        created_at="2026-10-06T09:00:00+00:00"))
    store.seed(ROUTES, "route-agent", {"route_id": "route-agent", "plan_id": "agent-1", "tenant_id": T,
                                       "status": "dispatched", "timestamp": "2026-10-06T05:00:00+00:00"})
    store.seed(ROUTES, "br-l1-r1", {"route_id": "br-l1-r1", "plan_id": "bp-l1-r1", "tenant_id": T,
                                    "status": "dispatched", "source": "dispatch_board",
                                    "timestamp": "2026-10-06T09:00:00+00:00"})
    agent = ExceptionReplanningAgent.__new__(ExceptionReplanningAgent)
    agent._es = store
    snapshot = await agent._load_plan_snapshot(T)
    assert snapshot["loading_plan"]["plan_id"] == "agent-1"
    assert snapshot["route_plan"]["route_id"] == "route-agent"
