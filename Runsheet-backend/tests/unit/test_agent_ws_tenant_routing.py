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
