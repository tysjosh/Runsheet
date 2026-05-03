"""
Runsheet Logistics API — Application entry point.

All service initialization is delegated to the bootstrap/ package.
This file contains only app creation, lifespan, router inclusion,
and WebSocket/health endpoint definitions.

Requirements: 1.3, 1.6, 2.5
"""
# Load .env.development BEFORE any imports to ensure GEMINI_API_KEY is available
import os
from dotenv import load_dotenv
_env = os.environ.get("ENVIRONMENT", "development").lower()
_env_file = f".env.{_env}" if os.path.exists(f".env.{_env}") else ".env"
load_dotenv(_env_file, override=True)

from contextlib import asynccontextmanager
import json
import logging
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse

from bootstrap import ServiceContainer, initialize_all, shutdown_all
from errors.handlers import register_exception_handlers
from data_endpoints import router as data_router
from ops.webhooks.receiver import router as webhook_router
from ops.api.endpoints import router as ops_router
from fuel.api.endpoints import router as fuel_router
from scheduling.api.endpoints import router as scheduling_router
from scheduling.api.driver_endpoints import router as driver_scheduling_router
from driver.api.message_endpoints import router as message_router
from driver.api.exception_endpoints import router as exception_router
from driver.api.pod_endpoints import router as pod_router
from agent_endpoints import router as agent_router
from inline_endpoints import router as inline_router
from import_endpoints import router as import_router
from notifications.api.endpoints import router as notification_router
from notifications.api.metrics_endpoints import router as metrics_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager — delegates to bootstrap modules."""
    logger.info("Starting Runsheet Logistics API...")
    container = ServiceContainer()
    app.state.container = container
    await initialize_all(app, container)
    yield
    logger.info("Shutting down Runsheet Logistics API...")
    await shutdown_all(app, container)


app = FastAPI(title="Runsheet Logistics API", version="1.0.0", lifespan=lifespan)

# Register structured error handlers (AppException → proper JSON, not 500)
register_exception_handlers(app)

# CORS must be added before the app starts (cannot be added in lifespan/bootstrap)
from fastapi.middleware.cors import CORSMiddleware
import os, json as _json
_cors_raw = os.environ.get("CORS_ORIGINS", '["http://localhost:3000", "http://127.0.0.1:3000"]')
try:
    _cors_origins = _json.loads(_cors_raw)
except Exception as e:
    logger.warning(f"Failed to parse CORS_ORIGINS: {e}, using defaults")
    _cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Accept", "Accept-Language", "Content-Language", "Content-Type",
                    "Authorization", "X-Request-ID", "X-Requested-With", "X-Idempotency-Key"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                     "X-Idempotent-Replayed"],
    max_age=600,
)

# Routers (middleware is registered by bootstrap/middleware.py)
app.include_router(data_router)
app.include_router(webhook_router)
app.include_router(ops_router)
app.include_router(fuel_router)
app.include_router(scheduling_router)
app.include_router(driver_scheduling_router)
app.include_router(message_router)
app.include_router(exception_router)
app.include_router(pod_router)
app.include_router(agent_router)
app.include_router(inline_router)
app.include_router(import_router)
app.include_router(notification_router)
app.include_router(metrics_router)


def _c(app: FastAPI) -> ServiceContainer:
    return app.state.container


def _ws_authenticate(websocket: WebSocket) -> str | None:
    """Extract tenant_id from a WebSocket ``token`` query parameter.

    In development mode, returns ``"dev-tenant"`` when no token is
    provided — matching the REST endpoint behaviour in
    ``get_tenant_context``.  In non-development environments, returns
    ``None`` to signal that the connection should be rejected.
    """
    from jose import JWTError, jwt as jose_jwt
    from config.settings import get_settings

    settings = get_settings()
    token = websocket.query_params.get("token", "")

    if not token:
        if settings.environment.value == "development":
            return "dev-tenant"
        return None

    try:
        payload = jose_jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        tenant_id = payload.get("tenant_id", "")
        return tenant_id if tenant_id else None
    except JWTError:
        return None


def _ws_authenticate_driver(websocket: WebSocket) -> tuple[str, str] | None:
    """Extract tenant_id and driver_id from a WebSocket ``token`` query parameter.

    Used by the ``/ws/driver`` endpoint which requires both tenant_id and
    driver_id from the JWT claims.

    In development mode, returns ``("dev-tenant", "dev-driver")`` when no
    token is provided.  In non-development environments, returns ``None``
    to signal that the connection should be rejected.

    Returns:
        A ``(tenant_id, driver_id)`` tuple on success, or ``None`` on failure.

    Validates: Requirements 9.1, 9.2
    """
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
        if not tenant_id or not driver_id:
            return None
        return (tenant_id, driver_id)
    except JWTError:
        return None


# Health endpoints
@app.get("/")
async def root():
    return {"message": "Runsheet Logistics API is running"}

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "Runsheet Logistics API",
            "agent": "LogisticsAgent", "version": "1.0.0"}

@app.get("/health")
async def health_basic(request: Request):
    result = await _c(request.app).health_check_service.check_health()
    return {"status": result["status"], "service": "Runsheet Logistics API",
            "version": "1.0.0", "timestamp": result["timestamp"]}

@app.get("/health/ready")
async def health_ready(request: Request):
    hs = await _c(request.app).health_check_service.check_readiness()
    data = {"status": hs.status, "service": "Runsheet Logistics API",
            "version": "1.0.0", "timestamp": hs.timestamp.isoformat() + "Z",
            "dependencies": [d.to_dict() for d in hs.dependencies]}
    if hs.status == "unhealthy":
        data["failure_reasons"] = [{"dependency": d.name, "error": d.error}
                                   for d in hs.dependencies if not d.healthy]
        return JSONResponse(status_code=503, content=data)
    return data

@app.get("/health/live")
async def health_live(request: Request):
    result = await _c(request.app).health_check_service.check_liveness()
    return {"status": result["status"], "service": "Runsheet Logistics API",
            "version": "1.0.0", "timestamp": result["timestamp"]}


# WebSocket helpers and endpoints

async def _ws_loop(websocket: WebSocket, mgr, endpoint: str, tenant_id: str,
                   handler=None, check_connected: bool = False):
    """Shared WebSocket receive loop with error handling."""
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
        logger.debug("WebSocket client disconnected normally from %s (tenant_id=%s)", endpoint, tenant_id)
    except Exception as e:
        logger.error("Unexpected WebSocket error on %s: tenant_id=%s error=%s", endpoint, tenant_id, str(e))
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass
    finally:
        await mgr.disconnect(websocket)


async def _json_echo_handler(websocket: WebSocket, raw: str, endpoint: str, tenant_id: str,
                              extra_types: dict | None = None):
    """Handle ping/pong and optional extra message types for JSON-based WS endpoints."""
    try:
        msg = json.loads(raw)
        if msg.get("type") == "ping":
            await websocket.send_json({"type": "pong", "timestamp": datetime.utcnow().isoformat() + "Z"})
        elif extra_types and msg.get("type") in extra_types:
            await websocket.send_json(extra_types[msg["type"]])
    except json.JSONDecodeError:
        logger.warning(f"Malformed JSON received on {endpoint} (tenant_id=%s): %s", tenant_id, raw)
        await websocket.send_json({"type": "error", "message": "Invalid JSON"})


@app.websocket("/ws/ops")
async def ops_live_websocket(websocket: WebSocket):
    c = _c(websocket.app)
    tenant_id = _ws_authenticate(websocket)
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    mgr = c.ops_ws_manager
    await mgr.connect(websocket, tenant_id=tenant_id)
    await _ws_loop(websocket, mgr, "/ws/ops", tenant_id, check_connected=True)

@app.websocket("/ws/scheduling")
async def scheduling_live_websocket(websocket: WebSocket):
    c = _c(websocket.app)
    tenant_id = _ws_authenticate(websocket)
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    subs = websocket.query_params.get("subscriptions", "")
    subs_list = [s.strip() for s in subs.split(",") if s.strip()] if subs else None
    mgr = c.scheduling_ws_manager
    await mgr.connect(websocket, subscriptions=subs_list, tenant_id=tenant_id)
    await _ws_loop(websocket, mgr, "/ws/scheduling", tenant_id)

@app.websocket("/ws/notifications")
async def notifications_live_websocket(websocket: WebSocket):
    c = _c(websocket.app)
    tenant_id = _ws_authenticate(websocket)
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    mgr = c.notification_ws_manager
    await mgr.connect(websocket, tenant_id=tenant_id)
    ep = "/ws/notifications"
    handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
    await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

@app.websocket("/ws/agent-activity")
async def agent_activity_websocket(websocket: WebSocket):
    tenant_id = _ws_authenticate(websocket)
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    mgr = _c(websocket.app).agent_ws_manager
    await mgr.connect(websocket, tenant_id=tenant_id)
    ep = "/ws/agent-activity"
    handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id)
    await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

@app.websocket("/api/fleet/live")
async def fleet_live_websocket(websocket: WebSocket):
    tenant_id = _ws_authenticate(websocket)
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    mgr = _c(websocket.app).fleet_ws_manager
    await mgr.connect(websocket, tenant_id=tenant_id)
    ep = "/api/fleet/live"
    extras = {"subscribe": {"type": "subscribed", "message": "Subscribed to all fleet updates",
              "timestamp": datetime.utcnow().isoformat() + "Z"}}
    handler = lambda ws, raw: _json_echo_handler(ws, raw, ep, tenant_id, extra_types=extras)
    await _ws_loop(websocket, mgr, ep, tenant_id, handler=handler)

@app.websocket("/ws/driver")
async def driver_live_websocket(websocket: WebSocket):
    """Dedicated WebSocket channel for driver mobile clients.
    Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
    """
    c = _c(websocket.app)
    auth = _ws_authenticate_driver(websocket)
    if not auth:
        await websocket.close(code=4001, reason="Authentication required")
        return
    tenant_id, driver_id = auth
    mgr = c.driver_ws_manager
    await mgr.connect_driver(websocket, driver_id=driver_id, tenant_id=tenant_id)
    handler = lambda ws, raw: mgr.handle_driver_message(ws, raw)
    await _ws_loop(websocket, mgr, "/ws/driver", tenant_id, handler=handler, check_connected=True)


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
