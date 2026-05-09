"""
WebSocket endpoint registration for the FastAPI app.

Extracted from ``main.py`` so the entrypoint stays under its 350-line
budget (Req 1.6). Every endpoint lives here and is attached to the
shared :class:`FastAPI` instance via :func:`register_websocket_routes`.

Endpoints registered:

* ``/ws/ops``                — OpsWSManager (shipment updates)
* ``/ws/scheduling``         — SchedulingWSManager (job lifecycle)
* ``/ws/orders``             — OrdersWSManager (order + driver updates, Req 4.1)
* ``/ws/notifications``      — NotificationWSManager
* ``/ws/agent-activity``     — AgentActivityWSManager
* ``/api/fleet/live``        — FleetWSManager
* ``/ws/driver``             — DriverWSManager (per-driver channel)
* ``/ws/plan-execution``     — PlanExecutionWSManager (Req 3.6, 3.9)
* ``/ws/fuel-planning``      — FuelPlanningWSManager (Req 1.6.4)

Auth helpers (:func:`_authenticate_tenant` / :func:`_authenticate_driver`)
extract the tenant/driver ID from a JWT supplied via the ``token`` query
parameter. In ``development`` mode, a missing token yields a dev
default so local smoke tests work without JWT provisioning.

Logging: the module emits warnings and errors via the ``main`` logger
so tests that ``patch("main.logger")`` continue to see every WS-layer
log line. The lookup is performed via :func:`_logger` at call time so
the patch applies even when it lands after ``register_websocket_routes``
has run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


def _logger() -> logging.Logger:
    """Return the logger to use for WebSocket events.

    Resolves through ``main.logger`` when available so tests that
    patch the ``main`` module logger continue to observe warnings and
    errors emitted from here. Falls back to this module's own logger
    during bootstrap (before ``main`` has been imported) or in unit
    tests that exercise the helpers directly.
    """

    import sys

    main_module = sys.modules.get("main")
    if main_module is not None:
        candidate = getattr(main_module, "logger", None)
        if candidate is not None:
            return candidate
    return logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _authenticate_tenant(websocket: WebSocket) -> Optional[str]:
    """Extract tenant_id from a ``token`` query parameter."""
    from jose import JWTError, jwt as jose_jwt
    from config.settings import get_settings

    settings = get_settings()
    token = websocket.query_params.get("token", "")
    if not token:
        return "dev-tenant" if settings.environment.value == "development" else None
    try:
        payload = jose_jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        tenant_id = payload.get("tenant_id", "")
        return tenant_id if tenant_id else None
    except JWTError:
        return None


def _authenticate_driver(websocket: WebSocket) -> Optional[Tuple[str, str]]:
    """Extract ``(tenant_id, driver_id)`` from the JWT token. Req 9.1, 9.2."""
    from jose import JWTError, jwt as jose_jwt
    from config.settings import get_settings

    settings = get_settings()
    token = websocket.query_params.get("token", "")
    if not token:
        if settings.environment.value == "development":
            return ("dev-tenant", "dev-driver")
        return None
    try:
        payload = jose_jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        tenant_id = payload.get("tenant_id", "")
        driver_id = payload.get("driver_id", "")
        return (tenant_id, driver_id) if (tenant_id and driver_id) else None
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# Shared loop + JSON echo handler
# ---------------------------------------------------------------------------


async def _ws_loop(websocket, mgr, endpoint, tenant_id, handler=None,
                   check_connected=False):
    """Shared WebSocket receive loop with disconnect + error handling."""
    if check_connected and websocket not in mgr._clients:
        return
    try:
        while True:
            raw = await websocket.receive_text()
            if handler:
                await handler(websocket, raw)
            else:
                await mgr.handle_client_message(websocket, raw)
    except WebSocketDisconnect:
        _logger().debug(
            "WebSocket client disconnected normally from %s (tenant_id=%s)",
            endpoint, tenant_id,
        )
    except Exception as exc:  # noqa: BLE001
        _logger().error(
            "Unexpected WebSocket error on %s: tenant_id=%s error=%s",
            endpoint, tenant_id, str(exc),
        )
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception as close_err:  # noqa: BLE001
            _logger().debug(
                "Failed to close WebSocket on %s: %s", endpoint, close_err
            )
    finally:
        await mgr.disconnect(websocket)


async def _json_echo_handler(websocket, raw, endpoint, tenant_id, extra_types=None):
    """Handle ping/pong and optional additional message types for JSON WS endpoints."""
    try:
        msg = json.loads(raw)
        if msg.get("type") == "ping":
            await websocket.send_json(
                {"type": "pong", "timestamp": datetime.utcnow().isoformat() + "Z"}
            )
        elif extra_types and msg.get("type") in extra_types:
            await websocket.send_json(extra_types[msg["type"]])
    except json.JSONDecodeError:
        _logger().warning(
            f"Malformed JSON received on {endpoint} (tenant_id=%s): %s",
            tenant_id, raw,
        )
        await websocket.send_json({"type": "error", "message": "Invalid JSON"})


# ---------------------------------------------------------------------------
# Endpoint registration
# ---------------------------------------------------------------------------


def _container(app: FastAPI):
    return app.state.container


async def _reject(websocket: WebSocket) -> None:
    await websocket.close(code=4001, reason="Authentication required")


def register_websocket_routes(app: FastAPI) -> None:
    """Attach every WebSocket endpoint to ``app``.

    Called from ``main.py`` after the :class:`FastAPI` instance has
    been created and the service container has been wired up.
    """

    @app.websocket("/ws/ops")
    async def ops_live_websocket(websocket: WebSocket):
        tenant_id = _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        mgr = _container(websocket.app).ops_ws_manager
        await mgr.connect(websocket, tenant_id=tenant_id)
        await _ws_loop(websocket, mgr, "/ws/ops", tenant_id, check_connected=True)

    @app.websocket("/ws/scheduling")
    async def scheduling_live_websocket(websocket: WebSocket):
        tenant_id = _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        subs = websocket.query_params.get("subscriptions", "")
        subs_list = [s.strip() for s in subs.split(",") if s.strip()] if subs else None
        mgr = _container(websocket.app).scheduling_ws_manager
        await mgr.connect(websocket, subscriptions=subs_list, tenant_id=tenant_id)
        await _ws_loop(websocket, mgr, "/ws/scheduling", tenant_id)

    @app.websocket("/ws/orders")
    async def orders_live_websocket(websocket: WebSocket):
        """Real-time order and driver updates. Req 4.1."""
        tenant_id = _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        subs = websocket.query_params.get("subscriptions", "")
        subs_list = [s.strip() for s in subs.split(",") if s.strip()] if subs else None
        mgr = _container(websocket.app).orders_ws_manager
        await mgr.connect(websocket, subscriptions=subs_list, tenant_id=tenant_id)
        await _ws_loop(websocket, mgr, "/ws/orders", tenant_id)

    @app.websocket("/ws/notifications")
    async def notifications_live_websocket(websocket: WebSocket):
        tenant_id = _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        mgr = _container(websocket.app).notification_ws_manager
        await mgr.connect(websocket, tenant_id=tenant_id)
        ep = "/ws/notifications"
        handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
        await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

    @app.websocket("/ws/agent-activity")
    async def agent_activity_websocket(websocket: WebSocket):
        tenant_id = _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        mgr = _container(websocket.app).agent_ws_manager
        await mgr.connect(websocket, tenant_id=tenant_id)
        ep = "/ws/agent-activity"
        handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
        await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

    @app.websocket("/api/fleet/live")
    async def fleet_live_websocket(websocket: WebSocket):
        tenant_id = _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        mgr = _container(websocket.app).fleet_ws_manager
        await mgr.connect(websocket, tenant_id=tenant_id)
        ep = "/api/fleet/live"
        extras = {"subscribe": {
            "type": "subscribed",
            "message": "Subscribed to all fleet updates",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }}
        handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id,
                                                      extra_types=extras)
        await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

    @app.websocket("/ws/driver")
    async def driver_live_websocket(websocket: WebSocket):
        """Per-driver WebSocket channel. Req 9.1–9.6."""
        auth = _authenticate_driver(websocket)
        if not auth:
            return await _reject(websocket)
        tenant_id, driver_id = auth
        mgr = _container(websocket.app).driver_ws_manager
        await mgr.connect_driver(websocket, driver_id=driver_id, tenant_id=tenant_id)
        handler = lambda ws, raw: mgr.handle_driver_message(ws, raw)
        await _ws_loop(websocket, mgr, "/ws/driver", tenant_id,
                       handler=handler, check_connected=True)

    @app.websocket("/ws/plan-execution")
    async def plan_execution_websocket(websocket: WebSocket):
        """Real-time plan-execution updates. Req 3.6, 3.9."""
        tenant_id = _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        from Agents.support.plan_execution_ws_manager import (
            get_plan_execution_ws_manager,
        )
        mgr = get_plan_execution_ws_manager()
        await mgr.connect(websocket, tenant_id=tenant_id)
        ep = "/ws/plan-execution"
        handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
        await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

    @app.websocket("/ws/fuel-planning")
    async def fuel_planning_websocket(websocket: WebSocket):
        """Fuel-planning events (per-tank forecasts, emergency stops,
        replan diffs, sourcing). Req 1.6.4."""
        tenant_id = _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        from fuel.services.fuel_planning_ws_manager import (
            get_fuel_planning_ws_manager,
        )
        mgr = get_fuel_planning_ws_manager()
        await mgr.connect(websocket, tenant_id=tenant_id)
        ep = "/ws/fuel-planning"
        handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
        await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)
