"""Every ``.broadcast(`` call in the backend fits its manager's signature (D4).

The WS managers don't share one ``broadcast`` signature:
``BaseWSManager.broadcast(message)``, but ``OrdersWSManager`` and
``SchedulingWebSocketManager`` override it as
``broadcast(event_type, data, tenant_id="")``, and the scheduling one refuses
a call without ``tenant_id``. Callers hold the managers as untyped attributes
and wrap the call in ``try/except``, so a mismatch is a swallowed TypeError
(or a logged refusal) and the event reaches nobody (N-new-1).

This test walks the source with ``ast``, pins every ``<receiver>.broadcast(``
call to a manager class through :data:`CALL_SITES`, and binds the call's
arguments against that class's real ``broadcast``. A new call site fails
until it's registered here.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Dict, List, Tuple, Type

from Agents.agent_ws_manager import AgentActivityWSManager
from Agents.support.plan_execution_ws_manager import PlanExecutionWSManager
from driver.ws.driver_ws_manager import DriverWSManager
from fuel.services.fuel_planning_ws_manager import FuelPlanningWSManager
from fuel.websocket.orders_ws import OrdersWSManager
from notifications.ws.notification_ws_manager import NotificationWSManager
from scheduling.websocket.scheduling_ws import SchedulingWebSocketManager
from websocket.connection_manager import ConnectionManager

BACKEND = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {"venv", ".venv", "tests", "node_modules"}

#: (path relative to Runsheet-backend, ast.unparse of the receiver) -> manager.
CALL_SITES: Dict[Tuple[str, str], Type] = {
    # Orders socket (fuel)
    ("fuel/api/feature_flag_admin_endpoints.py", "_orders_ws_manager"): OrdersWSManager,
    ("fuel/services/order_creation_service.py", "self._ws_manager"): OrdersWSManager,
    ("fuel/services/order_intake_pipeline.py", "self._ws_manager"): OrdersWSManager,
    # fuel/services/order_service.py uses broadcast_order_status_changed (N-new-1);
    # a .broadcast( there would fail registration until it's reviewed and added.
    # Scheduling socket (scheduling + driver services)
    ("scheduling/api/driver_endpoints.py", "_scheduling_ws_manager"): SchedulingWebSocketManager,
    ("scheduling/services/cargo_service.py", "self._ws_manager"): SchedulingWebSocketManager,
    ("scheduling/services/delay_detection_service.py", "self._ws"): SchedulingWebSocketManager,
    ("scheduling/services/job_reroute_service.py", "self._ws_manager"): SchedulingWebSocketManager,
    ("scheduling/services/job_service.py", "self._ws_manager"): SchedulingWebSocketManager,
    ("driver/services/exception_service.py", "self._scheduling_ws_manager"): SchedulingWebSocketManager,
    ("driver/services/inspection_service.py", "self._scheduling_ws_manager"): SchedulingWebSocketManager,
    ("driver/services/message_service.py", "self._scheduling_ws_manager"): SchedulingWebSocketManager,
    ("driver/services/order_transition_service.py", "self._scheduling_ws_manager"): SchedulingWebSocketManager,
    ("driver/services/pod_service.py", "self._scheduling_ws_manager"): SchedulingWebSocketManager,
    # inventory/service.py sends alerts with broadcast_to_tenant; a plain
    # .broadcast( there would reach every tenant and fails registration.
    # Each manager calling its own broadcast
    ("websocket/connection_manager.py", "self"): ConnectionManager,
    ("fuel/services/fuel_planning_ws_manager.py", "self"): FuelPlanningWSManager,
    ("scheduling/websocket/scheduling_ws.py", "self"): SchedulingWebSocketManager,
    ("driver/ws/driver_ws_manager.py", "self"): DriverWSManager,
    ("Agents/agent_ws_manager.py", "self"): AgentActivityWSManager,
    ("Agents/support/plan_execution_ws_manager.py", "self"): PlanExecutionWSManager,
    ("notifications/ws/notification_ws_manager.py", "self"): NotificationWSManager,
}


def _broadcast_calls() -> List[Tuple[str, int, str, ast.Call]]:
    """``(relpath, line, receiver, call)`` for every ``X.broadcast(...)`` call."""
    found = []
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if _SKIP_DIRS.intersection(rel.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "broadcast"
            ):
                found.append((rel.as_posix(), node.lineno, ast.unparse(node.func.value), node))
    return found


def test_the_walk_finds_call_sites():
    sites = _broadcast_calls()
    assert len(sites) >= len(CALL_SITES)
    assert any(rel == "scheduling/services/job_service.py" for rel, *_ in sites)
    assert any(rel == "fuel/api/feature_flag_admin_endpoints.py" for rel, *_ in sites)


def test_every_broadcast_call_site_is_registered():
    unregistered = [
        f"{rel}:{line} ({receiver}.broadcast)"
        for rel, line, receiver, _call in _broadcast_calls()
        if (rel, receiver) not in CALL_SITES
    ]
    assert unregistered == [], (
        "Register these .broadcast( call sites in CALL_SITES with the manager "
        "class they reach: " + ", ".join(unregistered)
    )


def test_every_broadcast_call_binds_to_its_managers_signature():
    sentinel = object()
    mismatched = []
    for rel, line, receiver, call in _broadcast_calls():
        cls = CALL_SITES.get((rel, receiver))
        if cls is None:
            continue  # reported by the registration test
        if any(isinstance(a, ast.Starred) for a in call.args) or any(
            k.arg is None for k in call.keywords
        ):
            mismatched.append(f"{rel}:{line} uses *args/**kwargs; pass arguments explicitly")
            continue
        try:
            inspect.signature(cls.broadcast).bind(
                object(),
                *[sentinel] * len(call.args),
                **{k.arg: sentinel for k in call.keywords},
            )
        except TypeError as exc:
            mismatched.append(f"{rel}:{line} {cls.__name__}.broadcast: {exc}")
    assert mismatched == [], "\n".join(mismatched)


#: ``broadcast_event`` call sites whose payload is a variable rather than a
#: dict literal, reviewed to always carry ``tenant_id`` (OI-01).
#: (path relative to Runsheet-backend, payload variable name).
BROADCAST_EVENT_PAYLOAD_VARS = {
    ("Agents/support/fuel_distribution_pipeline.py", "event_data"),
    ("fuel/services/fuel_planning_ws_manager.py", "payload"),
}


def _broadcast_event_calls() -> List[Tuple[str, int, ast.Call]]:
    """``(relpath, line, call)`` for every ``X.broadcast_event(...)`` call.

    Matches the attribute ``broadcast_event`` exactly, so the private
    ``_broadcast_event`` helpers in orders_ws.py/ops_ws.py aren't included.
    """
    found = []
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if _SKIP_DIRS.intersection(rel.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "broadcast_event"
            ):
                found.append((rel.as_posix(), node.lineno, node))
    return found


def _payload_arg(call: ast.Call):
    if len(call.args) >= 2:
        return call.args[1]
    for kw in call.keywords:
        if kw.arg == "data":
            return kw.value
    return None


def test_the_broadcast_event_walk_finds_call_sites():
    rels = {rel for rel, _line, _call in _broadcast_event_calls()}
    assert "Agents/autonomous/delay_response_agent.py" in rels
    assert "Agents/support/fuel_distribution_pipeline.py" in rels


def test_every_broadcast_event_payload_carries_tenant_id():
    """broadcast_event drops a payload without tenant_id (OI-01), so every
    call must pass a dict literal with a ``"tenant_id"`` key, or a reviewed
    variable listed in BROADCAST_EVENT_PAYLOAD_VARS."""
    bad = []
    for rel, line, call in _broadcast_event_calls():
        payload = _payload_arg(call)
        if isinstance(payload, ast.Dict):
            keys = {
                k.value for k in payload.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if "tenant_id" not in keys:
                bad.append(f"{rel}:{line} payload dict has no 'tenant_id' key")
        elif isinstance(payload, ast.Name):
            if (rel, payload.id) not in BROADCAST_EVENT_PAYLOAD_VARS:
                bad.append(
                    f"{rel}:{line} payload variable {payload.id!r} isn't in "
                    "BROADCAST_EVENT_PAYLOAD_VARS"
                )
        else:
            bad.append(f"{rel}:{line} payload must be a dict literal or a reviewed name")
    assert bad == [], "\n".join(bad)


def test_every_scheduling_broadcast_passes_tenant_id():
    tenantless = []
    for rel, line, receiver, call in _broadcast_calls():
        if CALL_SITES.get((rel, receiver)) is not SchedulingWebSocketManager:
            continue
        has_tenant = len(call.args) >= 3 or any(k.arg == "tenant_id" for k in call.keywords)
        if not has_tenant:
            tenantless.append(f"{rel}:{line}")
    assert tenantless == [], (
        "SchedulingWebSocketManager.broadcast refuses a call without tenant_id: "
        + ", ".join(tenantless)
    )
