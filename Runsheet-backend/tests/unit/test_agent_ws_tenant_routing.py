"""Agent-activity and fuel-planning WS events are tenant-scoped (OI-01).

``broadcast_event`` on both managers used to call ``broadcast`` and reach
every tenant's sockets. It now reads ``tenant_id`` from the payload, sends
only to that tenant, and drops a tenantless payload with a WARNING.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.agent_ws_manager import AgentActivityWSManager
from fuel.services.fuel_planning_ws_manager import FuelPlanningWSManager


def _make_websocket():
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


def _received(ws):
    """Messages after the connection handshake."""
    return [c[0][0] for c in ws.send_json.call_args_list[1:]]


async def _two_tenants(manager):
    ws_a = _make_websocket()
    ws_b = _make_websocket()
    await manager.connect(ws_a, tenant_id="tenant-A")
    await manager.connect(ws_b, tenant_id="tenant-B")
    return ws_a, ws_b


@pytest.mark.parametrize("manager_cls", [AgentActivityWSManager, FuelPlanningWSManager])
async def test_tenant_a_event_reaches_only_tenant_a(manager_cls):
    manager = manager_cls()
    ws_a, ws_b = await _two_tenants(manager)

    count = await manager.broadcast_event(
        "delay_alert", {"job_id": "J-1", "tenant_id": "tenant-A"}
    )

    assert count == 1
    assert [m["type"] for m in _received(ws_a)] == ["delay_alert"]
    assert _received(ws_b) == []


@pytest.mark.parametrize("manager_cls", [AgentActivityWSManager, FuelPlanningWSManager])
async def test_event_without_tenant_reaches_nobody(manager_cls, caplog):
    manager = manager_cls()
    ws_a, ws_b = await _two_tenants(manager)

    with caplog.at_level(logging.WARNING):
        count = await manager.broadcast_event("fuel_alert", {"station_id": "S-1"})

    assert count == 0
    assert _received(ws_a) == [] and _received(ws_b) == []
    assert "fuel_alert without tenant_id dropped" in caplog.text


@pytest.mark.parametrize("manager_cls", [AgentActivityWSManager, FuelPlanningWSManager])
async def test_non_dict_payload_reaches_nobody(manager_cls):
    manager = manager_cls()
    ws_a, ws_b = await _two_tenants(manager)

    assert await manager.broadcast_event("x", None) == 0
    assert _received(ws_a) == [] and _received(ws_b) == []


async def test_forecast_ready_helper_reaches_only_its_tenant():
    manager = FuelPlanningWSManager()
    ws_a, ws_b = await _two_tenants(manager)

    count = await manager.broadcast_customer_tank_forecast_ready(
        run_id="run-1",
        tenant_id="tenant-B",
        customer_tank_id="CT-1",
        fuel_type="propane",
        runout_risk_24h=0.4,
        model_name="degree_day",
    )

    assert count == 1
    assert _received(ws_a) == []
    msgs = _received(ws_b)
    assert [m["type"] for m in msgs] == ["customer_tank_forecast_ready"]
    assert msgs[0]["data"]["tenant_id"] == "tenant-B"


async def test_helper_extra_cannot_override_tenant():
    manager = FuelPlanningWSManager()
    ws_a, ws_b = await _two_tenants(manager)

    await manager.broadcast_replan_diff_ready(
        event_id="E-1",
        diff_id="D-1",
        tenant_id="tenant-A",
        summary={"added": 1},
        extra={"tenant_id": "tenant-B"},
    )

    assert len(_received(ws_a)) == 1
    assert _received(ws_b) == []


# ---------------------------------------------------------------------------
# L2: broadcast_activity is tenant-only and redacted
# ---------------------------------------------------------------------------


async def test_activity_without_tenant_reaches_nobody(caplog):
    manager = AgentActivityWSManager()
    ws_a, ws_b = await _two_tenants(manager)
    with caplog.at_level(logging.WARNING):
        count = await manager.broadcast_activity(
            {"action_type": "plan", "details": {"goal": "secret prompt"}}
        )
    assert count == 0
    assert _received(ws_a) == [] and _received(ws_b) == []
    assert "without tenant_id dropped" in caplog.text


async def test_activity_reaches_only_its_tenant_with_a_redacted_payload():
    manager = AgentActivityWSManager()
    ws_a, ws_b = await _two_tenants(manager)
    entry = {
        "log_id": "L1",
        "agent_id": "orchestrator",
        "action_type": "monitoring_cycle",
        "tool_name": "search_orders",
        "parameters": {"message": "what did customer X order?"},
        "tenant_id": "tenant-A",
        "user_id": "user-1",
        "session_id": "sess-1",
        "details": {
            "goal": "what did customer X order?",
            "step_results": [{"output": "..."}],
            "result": "raw",
            "detection_count": 3,
            "action_count": 1,
            "plan_id": "p1",
        },
    }
    assert await manager.broadcast_activity(entry) == 1
    assert _received(ws_b) == []
    (msg,) = _received(ws_a)
    data = msg["data"]
    assert msg["type"] == "agent_activity"
    for dropped in ("parameters", "user_id", "session_id"):
        assert dropped not in data
    for dropped in ("goal", "step_results", "result"):
        assert dropped not in data["details"]
    assert data["action_type"] == "monitoring_cycle"
    assert data["tool_name"] == "search_orders"
    assert data["details"] == {"detection_count": 3, "action_count": 1, "plan_id": "p1"}
    assert "customer X" not in repr(msg)
    # The caller's entry (persisted copy) is untouched.
    assert entry["details"]["goal"] == "what did customer X order?"


async def test_plan_created_from_tenant_a_never_reaches_tenant_b():
    """End to end: planner -> real ActivityLogService -> real WS manager."""
    from Agents.activity_log_service import ActivityLogService
    from Agents.execution_planner import ExecutionPlanner

    manager = AgentActivityWSManager()
    ws_a, ws_b = await _two_tenants(manager)
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    planner = ExecutionPlanner(ActivityLogService(es, ws_manager=manager))

    await planner.create_plan(
        "tenant A's private question", ["fuel"], tenant_id="tenant-A", user_id="u1"
    )

    assert _received(ws_b) == []
    (msg,) = _received(ws_a)
    assert "private question" not in repr(msg)
    persisted = es.index_document.await_args.args[2]
    assert persisted["tenant_id"] == "tenant-A"
    assert persisted["details"]["goal"] == "tenant A's private question"

    # A plan with no tenant is persisted but pushed to nobody.
    await planner.create_plan("tenantless", ["fuel"])
    assert len(_received(ws_a)) == 1 and _received(ws_b) == []
