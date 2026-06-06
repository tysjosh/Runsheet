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
authenticate the WebSocket handshake against a **SuperTokens session** and
derive ``tenant_id`` (and ``driver_id`` for the per-driver channel) from the
verified, server-signed access-token payload (Req 7.1, 7.3). They branch on the
``auth_provider`` Migration_Controller flag exactly like the HTTP
Session_Verifier (``ops.middleware.tenant_guard.get_tenant_context``):

* ``"supertokens"`` — verify a SuperTokens session only.
* ``"dual"`` — prefer a SuperTokens session; fall back to a legacy JWT when no
  SuperTokens session is present on the handshake.
* ``"legacy"`` (and any unrecognized value) — the pre-migration legacy JWT path.

The SuperTokens session credential is taken **cookie-first** from the handshake
(``sAccessToken``); where a browser cannot attach the cookie to a WS handshake,
a short-lived session token may be supplied via the ``token`` query parameter
(Req 7.5). Missing, malformed, expired, or incomplete credentials are rejected
for every environment with the existing ``4001 Authentication required`` close
code (Req 7.2).

The session-token value is **never** written to application logs: log lines emit
only ``tenant_id`` and the endpoint path, never the credential (Req 7.4, 7.5).

Logging: the module emits warnings and errors via the ``main`` logger
so tests that ``patch("main.logger")`` continue to see every WS-layer
log line. The lookup is performed via :func:`_logger` at call time so
the patch applies even when it lands after ``register_websocket_routes``
has run.

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

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
# Auth helpers (WebSocket_Authenticator — Req 7.1–7.5)
# ---------------------------------------------------------------------------
#
# WebSocket connections are authenticated against a verified SuperTokens
# session whose ``tenant_id`` / ``roles`` / ``has_pii_access`` (and ``driver_id``
# for driver users) claims are signed by the managed core. The credential is
# taken cookie-first from the handshake (``sAccessToken``); where a browser
# cannot attach the cookie a short-lived session token may be supplied via the
# ``token`` query parameter (Req 7.5). The token value is never logged (Req 7.4).
#
# A ``WSSessionVerifier`` seam mirrors the HTTP Session_Verifier seam in
# ``ops.middleware.tenant_guard`` so tests can inject a fake verifier without a
# live managed core. ``None`` means "use the default SDK-backed verifier".


# Verifier seam: ``async (access_token, anti_csrf_token) -> Optional[claims]``.
WSSessionVerifier = Callable[
    [str, Optional[str]], Awaitable[Optional[Dict[str, Any]]]
]

_ws_session_verifier: Optional[WSSessionVerifier] = None


def configure_ws_session_verifier(verifier: Optional[WSSessionVerifier]) -> None:
    """Install the verifier used to validate a SuperTokens session on a WS handshake.

    Passing ``None`` resets to the default SDK-backed verifier. This is the seam
    tests use to exercise the ``supertokens`` / ``dual`` branching without a live
    managed core.
    """
    global _ws_session_verifier
    _ws_session_verifier = verifier


def _extract_session_credential(
    websocket: WebSocket,
) -> Tuple[str, Optional[str]]:
    """Return ``(access_token, anti_csrf_token)`` from the WS handshake.

    Cookie-first (``sAccessToken`` on the handshake); falls back to the ``token``
    query parameter where the browser cannot attach the cookie (Req 7.5). The
    anti-CSRF token, when present, is read from the ``anti-csrf`` header. The
    credential value is intentionally never logged here (Req 7.4).
    """
    access_token = ""
    cookie_header = websocket.headers.get("cookie", "") or ""
    if cookie_header:
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sAccessToken" and value:
                access_token = value
                break
    if not access_token:
        # Fallback transport: short-lived session token on the query string.
        access_token = websocket.query_params.get("token", "") or ""

    anti_csrf = websocket.headers.get("anti-csrf") or None
    return access_token, anti_csrf


async def _default_ws_verify(
    access_token: str, anti_csrf_token: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Default SuperTokens-backed WS session verification.

    Returns the verified access-token payload (claims) for a valid session, or
    ``None`` when the credential is missing or fails verification. Imports the
    SDK lazily so importing this module never forces the SuperTokens dependency
    to load.
    """
    if not access_token:
        return None
    from supertokens_python.recipe.session.asyncio import (
        get_session_without_request_response,
    )

    try:
        session = await get_session_without_request_response(
            access_token,
            anti_csrf_token,
            anti_csrf_check=False,
            session_required=False,
        )
    except Exception as exc:  # noqa: BLE001 — any verification failure → reject
        # Never log the credential value (Req 7.4); log only the failure reason.
        _logger().debug("WebSocket SuperTokens session verification failed: %s", exc)
        return None
    if session is None:
        return None
    return dict(session.get_access_token_payload() or {})


def _legacy_ws_claims(websocket: WebSocket) -> Optional[Dict[str, Any]]:
    """Legacy homegrown-JWT verification for the ``token`` query param.

    Used by the ``legacy`` branch and as the ``dual`` fallback when no
    SuperTokens session is present on the handshake. Returns the decoded claims
    or ``None``. The token value is never logged (Req 7.4).
    """
    from jose import JWTError, jwt as jose_jwt
    from config.settings import get_settings

    settings = get_settings()
    token = websocket.query_params.get("token", "") or ""
    if not token:
        return None
    try:
        return jose_jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except JWTError:
        return None


async def _resolve_ws_claims(websocket: WebSocket) -> Optional[Dict[str, Any]]:
    """Resolve verified session claims for a WS handshake, branching on auth_provider.

    Mirrors ``get_tenant_context``:

      * ``"supertokens"`` — verify a SuperTokens session only.
      * ``"dual"`` — prefer a SuperTokens session; fall back to the legacy JWT
        only when no SuperTokens session is present on the handshake.
      * ``"legacy"`` / anything else — the legacy JWT path.

    Returns the verified claims mapping, or ``None`` when the connection cannot
    be associated with a verified session (Req 7.1, 7.2).
    """
    from config.settings import get_settings

    provider = getattr(get_settings(), "auth_provider", "legacy")
    verifier = _ws_session_verifier or _default_ws_verify

    if provider in ("supertokens", "dual"):
        access_token, anti_csrf = _extract_session_credential(websocket)
        claims = await verifier(access_token, anti_csrf)
        if claims is not None:
            return claims
        if provider == "supertokens":
            return None
        # dual: fall back to legacy only when no SuperTokens session was present.
        return _legacy_ws_claims(websocket)

    # "legacy" (and any unrecognized value): pre-migration behavior.
    return _legacy_ws_claims(websocket)


async def _authenticate_tenant(websocket: WebSocket) -> Optional[str]:
    """Authenticate the handshake and return the verified ``tenant_id``.

    Derives ``tenant_id`` exclusively from the verified session claims (Req 7.1,
    7.3). Returns ``None`` when the connection cannot be associated with a
    verified session, so the caller closes it with ``4001`` (Req 7.2).
    """
    claims = await _resolve_ws_claims(websocket)
    if not claims:
        return None
    tenant_id = claims.get("tenant_id") or ""
    return tenant_id if tenant_id else None


async def _authenticate_driver(websocket: WebSocket) -> Optional[Tuple[str, str]]:
    """Authenticate the handshake and return ``(tenant_id, driver_id)``.

    Both ``tenant_id`` and ``driver_id`` are derived from the verified session
    (Req 7.3). Returns ``None`` when either is absent or the session cannot be
    verified, so the caller closes the connection with ``4001`` (Req 7.2).
    """
    claims = await _resolve_ws_claims(websocket)
    if not claims:
        return None
    tenant_id = claims.get("tenant_id") or ""
    driver_id = claims.get("driver_id") or ""
    return (tenant_id, driver_id) if (tenant_id and driver_id) else None


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
        tenant_id = await _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        mgr = _container(websocket.app).ops_ws_manager
        await mgr.connect(websocket, tenant_id=tenant_id)
        await _ws_loop(websocket, mgr, "/ws/ops", tenant_id, check_connected=True)

    @app.websocket("/ws/scheduling")
    async def scheduling_live_websocket(websocket: WebSocket):
        tenant_id = await _authenticate_tenant(websocket)
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
        tenant_id = await _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        subs = websocket.query_params.get("subscriptions", "")
        subs_list = [s.strip() for s in subs.split(",") if s.strip()] if subs else None
        mgr = _container(websocket.app).orders_ws_manager
        await mgr.connect(websocket, subscriptions=subs_list, tenant_id=tenant_id)
        await _ws_loop(websocket, mgr, "/ws/orders", tenant_id)

    @app.websocket("/ws/notifications")
    async def notifications_live_websocket(websocket: WebSocket):
        tenant_id = await _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        mgr = _container(websocket.app).notification_ws_manager
        await mgr.connect(websocket, tenant_id=tenant_id)
        ep = "/ws/notifications"
        handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
        await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

    @app.websocket("/ws/agent-activity")
    async def agent_activity_websocket(websocket: WebSocket):
        tenant_id = await _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        mgr = _container(websocket.app).agent_ws_manager
        await mgr.connect(websocket, tenant_id=tenant_id)
        ep = "/ws/agent-activity"
        handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
        await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

    @app.websocket("/api/fleet/live")
    async def fleet_live_websocket(websocket: WebSocket):
        tenant_id = await _authenticate_tenant(websocket)
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
        auth = await _authenticate_driver(websocket)
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
        tenant_id = await _authenticate_tenant(websocket)
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
        tenant_id = await _authenticate_tenant(websocket)
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

    @app.websocket("/ws/commerce/invoices")
    async def commerce_invoices_websocket(websocket: WebSocket):
        """Real-time invoice state updates. Design §6."""
        tenant_id = await _authenticate_tenant(websocket)
        if not tenant_id:
            return await _reject(websocket)
        from commerce.websocket.commerce_ws import (
            get_commerce_invoice_ws_manager,
        )
        mgr = get_commerce_invoice_ws_manager()
        await mgr.connect(websocket, tenant_id=tenant_id)
        ep = "/ws/commerce/invoices"
        handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
        await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)
