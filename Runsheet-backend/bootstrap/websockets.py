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
* ``/ws/commerce/invoices``  — CommerceInvoiceWSManager

Auth helpers (:func:`_authenticate_tenant` / :func:`_authenticate_driver`)
authenticate the WebSocket handshake against a **SuperTokens session** and
derive ``tenant_id`` (and ``driver_id`` for the per-driver channel) from the
verified, server-signed access-token payload (Req 7.1, 7.3). The homegrown
HS256 legacy/dual paths were removed once the SuperTokens hard cutover
completed.

The SuperTokens session credential is read from the handshake in the order
``Authorization: Bearer`` header → ``sAccessToken`` cookie → ``token`` query
parameter. Native mobile clients that can set handshake headers keep the
credential out of the URL (driver-mobile-app Req 14.1, 15.1); browsers use the
cookie; the short-lived ``token`` query parameter remains for clients that can
do neither (Req 7.5). Missing, malformed, expired, or incomplete credentials are
rejected for every environment with the existing ``4001 Authentication
required`` close code (Req 7.2, 14.2).

Before any credential is read, a handshake whose ``Origin`` is present and is
neither in ``CORS_ORIGINS`` nor same-origin with ``Host`` is rejected the same
way (Cross-Site WebSocket Hijacking, staging finding F2). See
:func:`_handshake_origin_allowed`.

An open socket re-checks its session handle every ``WS_SESSION_RECHECK_SECONDS``
and is closed with ``4001 Session ended`` after sign-out or revoke (OI-11). See
:func:`_watch_session`.

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

import asyncio
import contextlib
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
# for driver users) claims are signed by the managed core. The credential is read
# from the handshake in the order ``Authorization: Bearer`` header →
# ``sAccessToken`` cookie → ``token`` query parameter (Req 7.5, 14.1). The token
# value is never logged (Req 7.4, 15.1).
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
    tests use to exercise SuperTokens session verification without a live
    managed core.
    """
    global _ws_session_verifier
    _ws_session_verifier = verifier


def _extract_session_credential(
    websocket: WebSocket,
) -> Tuple[str, Optional[str]]:
    """Return ``(access_token, anti_csrf_token)`` from the WS handshake.

    Transport order is header → cookie → query parameter (Req 14.1, 14.2, 15.1):

    1. ``Authorization: Bearer <access token>`` on the handshake — the mobile
       transport, where a native client can set handshake headers and keep the
       credential out of the URL.
    2. ``sAccessToken`` handshake cookie — the browser transport.
    3. the short-lived ``token`` query parameter, for clients that can do
       neither (Req 7.5).

    The addition of the header read is additive: both pre-existing transports
    keep working unchanged. The anti-CSRF token, when present, is read from the
    ``anti-csrf`` header. The credential value is intentionally never logged
    here (Req 7.4, 15.1).
    """
    access_token = ""
    auth_header = websocket.headers.get("authorization", "") or ""
    if auth_header:
        scheme, _, credential = auth_header.strip().partition(" ")
        if scheme.lower() == "bearer":
            access_token = credential.strip()
    if not access_token:
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

    from ops.middleware.tenant_guard import session_check_database_enabled

    try:
        # anti_csrf_check stays False: a browser cannot set an ``anti-csrf``
        # header on a WebSocket handshake, so requiring it would break the
        # cookie transport. The Origin allow-list in _resolve_ws_claims is the
        # cross-site (CSWSH) control instead (F2).
        session = await get_session_without_request_response(
            access_token,
            anti_csrf_token,
            anti_csrf_check=False,
            session_required=False,
            # Ask the core whether the session is still alive so a signed-out
            # or revoked session cannot open a socket (F3).
            check_database=session_check_database_enabled(),
        )
    except Exception as exc:  # noqa: BLE001 — any verification failure → reject
        # Never log the credential value (Req 7.4); log only the failure reason.
        _logger().debug("WebSocket SuperTokens session verification failed: %s", exc)
        return None
    if session is None:
        return None
    return dict(session.get_access_token_payload() or {})


def _handshake_origin_allowed(websocket: WebSocket) -> bool:
    """Return whether the handshake's ``Origin`` may open a socket (F2).

    Cross-Site WebSocket Hijacking guard. Browsers attach the session cookie to
    a cross-site WebSocket handshake and CORS does not apply to WebSockets, so
    without this a page on any origin could open a socket as the signed-in user
    (staging finding F2). Allowed:

    * **No Origin header** — native clients that send none. A credential is
      still required by the caller.
    * **An origin in ``CORS_ORIGINS``** — exact match after trimming a trailing
      ``/``, the same list (via :mod:`config.cors`) the REST ``CORSMiddleware``
      uses. A ``"*"`` entry is NOT honoured here.
    * **Same-origin** — the Origin's ``host[:port]`` equals the handshake
      ``Host`` header. React Native's WebSocket sends the target URL as the
      default Origin (``wss://api…`` → ``https://api…``), so the driver app's
      ``/ws/driver`` socket depends on this. A cross-site page cannot make a
      browser send a forged ``Host``, and a non-browser client could set any
      Origin anyway, so this does not weaken the guard.

    Everything else, including the literal ``null`` origin, is rejected.
    """
    origin = (websocket.headers.get("origin") or "").strip()
    if not origin:
        return True

    from config.cors import get_cors_origins

    allowed = {o.rstrip("/") for o in get_cors_origins() if isinstance(o, str)}
    if origin.rstrip("/") in allowed:
        return True

    from urllib.parse import urlsplit

    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    host = (websocket.headers.get("host") or "").strip().lower()
    return (
        parts.scheme in ("http", "https")
        and bool(parts.netloc)
        and parts.netloc.lower() == host
    )


async def _resolve_ws_claims(websocket: WebSocket) -> Optional[Dict[str, Any]]:
    """Resolve verified SuperTokens session claims for a WS handshake.

    First rejects a cross-origin handshake (:func:`_handshake_origin_allowed`,
    F2) before reading or verifying any credential, so every route — all of
    them authenticate through here — closes it with ``4001`` before accept
    (HTTP 403 on a real server), exactly like an unauthenticated handshake.

    Then verifies a SuperTokens session only: the credential is read in the
    order ``Authorization: Bearer`` header → ``sAccessToken`` cookie →
    short-lived ``token`` query parameter (Req 7.5, 14.1). Returns the verified
    claims mapping, or ``None`` when
    the connection cannot be associated with a verified session (Req 7.1, 7.2).
    """
    if not _handshake_origin_allowed(websocket):
        try:
            path = websocket.url.path
        except Exception:  # noqa: BLE001 — log context only
            path = "?"
        _logger().warning(
            "WebSocket handshake rejected: origin not allowed (path=%s origin=%r)",
            path,
            (websocket.headers.get("origin") or "").strip(),
        )
        return None
    verifier = _ws_session_verifier or _default_ws_verify
    access_token, anti_csrf = _extract_session_credential(websocket)
    claims = await verifier(access_token, anti_csrf)
    if claims:
        # Remember the session handle so _ws_loop can re-check that the
        # session is still alive after sign-out or revoke (OI-11).
        handle = claims.get("sessionHandle")
        try:
            websocket.state.ws_session_handle = (
                handle if isinstance(handle, str) and handle else None
            )
        except Exception:  # noqa: BLE001 — a fake socket without state
            pass
    return claims


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
# Session revalidation for long-lived sockets (OI-11)
# ---------------------------------------------------------------------------
#
# The handshake verifies the session once. A socket opened before sign-out or
# revoke would otherwise stay open for as long as the client keeps it. Every
# ``WS_SESSION_RECHECK_SECONDS`` the loop asks whether the session handle from
# the handshake is still alive and closes the socket with ``4001 Session
# ended`` when it isn't. The check is by session handle, not access token, so
# an access-token refresh never closes a healthy socket.

# Alive-check seam: ``async (session_handle) -> Optional[bool]``. ``True`` is
# alive, ``False`` is ended, ``None`` is unknown (the socket stays open).
WSSessionAliveCheck = Callable[[str], Awaitable[Optional[bool]]]

_ws_session_alive_check: Optional[WSSessionAliveCheck] = None


def configure_ws_session_alive_check(check: Optional[WSSessionAliveCheck]) -> None:
    """Install the session alive-check used by the WS revalidation task.

    Passing ``None`` resets to the default SDK-backed check. Tests use this
    seam to drive revalidation without a live managed core.
    """
    global _ws_session_alive_check
    _ws_session_alive_check = check


async def _default_ws_session_alive(session_handle: str) -> Optional[bool]:
    """Ask the SuperTokens core whether *session_handle* is still alive.

    Returns ``None`` on any error: a core blip must not disconnect every
    client, so a transient failure keeps the socket open (logged at WARNING).
    """
    try:
        from supertokens_python.recipe.session.asyncio import (
            get_session_information,
        )

        return await get_session_information(session_handle) is not None
    except Exception as exc:  # noqa: BLE001 — unknown, keep the socket open
        _logger().warning("WebSocket session re-check failed: %s", exc)
        return None


def _session_recheck_seconds() -> float:
    """``settings.ws_session_recheck_seconds`` (0 disables), default 60."""
    try:
        from config.settings import get_settings

        value = getattr(get_settings(), "ws_session_recheck_seconds", 60)
    except Exception:  # noqa: BLE001 — settings unavailable, keep the default
        return 60.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 60.0
    return float(value)


async def _watch_session(websocket, session_handle, interval, endpoint, tenant_id):
    """Close *websocket* with 4001 once its session is no longer alive."""
    check = _ws_session_alive_check or _default_ws_session_alive
    while True:
        await asyncio.sleep(interval)
        alive = await check(session_handle)
        if alive is False:
            _logger().info(
                "WebSocket session ended; closing %s (tenant_id=%s)",
                endpoint, tenant_id,
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=4001, reason="Session ended")
            return


def _start_session_watch(websocket, endpoint, tenant_id) -> Optional[asyncio.Task]:
    """Start the revalidation task, or return ``None`` when there is no
    session handle or the interval is 0."""
    try:
        handle = getattr(websocket.state, "ws_session_handle", None)
    except Exception:  # noqa: BLE001 — a fake socket without state
        handle = None
    if not isinstance(handle, str) or not handle:
        return None
    interval = _session_recheck_seconds()
    if interval <= 0:
        return None
    return asyncio.create_task(
        _watch_session(websocket, handle, interval, endpoint, tenant_id)
    )


# ---------------------------------------------------------------------------
# Shared loop + JSON echo handler
# ---------------------------------------------------------------------------


async def _ws_loop(websocket, mgr, endpoint, tenant_id, handler=None,
                   check_connected=False):
    """Shared WebSocket receive loop with disconnect + error handling.

    Runs the session revalidation task (OI-11) alongside the receive loop and
    cancels it when the loop ends.
    """
    if check_connected and websocket not in mgr._clients:
        return
    watcher = _start_session_watch(websocket, endpoint, tenant_id)
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
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
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
