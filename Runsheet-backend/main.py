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
import traceback
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
from agent_endpoints import router as agent_router
from inline_endpoints import router as inline_router
from import_endpoints import router as import_router

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
                    "Authorization", "X-Request-ID", "X-Requested-With"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
    max_age=600,
)

# Routers (middleware is registered by bootstrap/middleware.py)
app.include_router(data_router)
app.include_router(webhook_router)
app.include_router(ops_router)
app.include_router(fuel_router)
app.include_router(scheduling_router)
app.include_router(agent_router)
app.include_router(inline_router)
app.include_router(import_router)


def _c(app: FastAPI) -> ServiceContainer:
    return app.state.container


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


# WebSocket endpoints
@app.websocket("/ws/ops")
async def ops_live_websocket(websocket: WebSocket):
    from jose import JWTError, jwt as jose_jwt
    c = _c(websocket.app)
    token = websocket.query_params.get("token", "")
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return
    try:
        payload = jose_jwt.decode(token, c.settings.jwt_secret,
                                  algorithms=[c.settings.jwt_algorithm])
        tenant_id = payload.get("tenant_id", "")
    except JWTError:
        await websocket.close(code=4001, reason="Authentication failed")
        return
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    mgr = c.ops_ws_manager
    await mgr.connect(websocket, tenant_id=tenant_id)
    if websocket not in mgr._clients:
        return
    try:
        while True:
            raw = await websocket.receive_text()
            await mgr.handle_client_message(websocket, raw)
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected normally from /ws/ops (tenant_id=%s)", tenant_id)
    except Exception as e:
        logger.error(
            "Unexpected WebSocket error on /ws/ops: endpoint=/ws/ops tenant_id=%s exception_type=%s error=%s traceback=%s",
            tenant_id, type(e).__name__, str(e), traceback.format_exc()
        )
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass
    finally:
        await mgr.disconnect(websocket)

@app.websocket("/ws/scheduling")
async def scheduling_live_websocket(websocket: WebSocket):
    from jose import JWTError, jwt as jose_jwt
    from config.settings import get_settings
    c = _c(websocket.app)
    settings = get_settings()
    token = websocket.query_params.get("token", "")
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return
    try:
        payload = jose_jwt.decode(token, settings.jwt_secret,
                                  algorithms=[settings.jwt_algorithm])
        tenant_id = payload.get("tenant_id", "")
    except JWTError:
        await websocket.close(code=4001, reason="Authentication required")
        return
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    subs = websocket.query_params.get("subscriptions", "")
    subs_list = [s.strip() for s in subs.split(",") if s.strip()] if subs else None
    mgr = c.scheduling_ws_manager
    await mgr.connect(websocket, subscriptions=subs_list, tenant_id=tenant_id)
    try:
        while True:
            raw = await websocket.receive_text()
            await mgr.handle_client_message(websocket, raw)
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected normally from /ws/scheduling (tenant_id=%s)", tenant_id)
    except Exception as e:
        logger.error(
            "Unexpected WebSocket error on /ws/scheduling: endpoint=/ws/scheduling tenant_id=%s exception_type=%s error=%s traceback=%s",
            tenant_id, type(e).__name__, str(e), traceback.format_exc()
        )
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass
    finally:
        await mgr.disconnect(websocket)

@app.websocket("/ws/agent-activity")
async def agent_activity_websocket(websocket: WebSocket):
    from jose import JWTError, jwt as jose_jwt
    from config.settings import get_settings
    settings = get_settings()
    token = websocket.query_params.get("token", "")
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return
    try:
        payload = jose_jwt.decode(token, settings.jwt_secret,
                                  algorithms=[settings.jwt_algorithm])
        tenant_id = payload.get("tenant_id", "")
    except JWTError:
        await websocket.close(code=4001, reason="Authentication required")
        return
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    mgr = _c(websocket.app).agent_ws_manager
    await mgr.connect(websocket, tenant_id=tenant_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong",
                        "timestamp": datetime.utcnow().isoformat() + "Z"})
            except json.JSONDecodeError:
                logger.warning("Malformed JSON received on /ws/agent-activity (tenant_id=%s): %s", tenant_id, data)
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected normally from /ws/agent-activity (tenant_id=%s)", tenant_id)
    except Exception as e:
        logger.error(
            "Unexpected WebSocket error on /ws/agent-activity: endpoint=/ws/agent-activity tenant_id=%s exception_type=%s error=%s traceback=%s",
            tenant_id, type(e).__name__, str(e), traceback.format_exc()
        )
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass
    finally:
        await mgr.disconnect(websocket)

@app.websocket("/api/fleet/live")
async def fleet_live_websocket(websocket: WebSocket):
    from jose import JWTError, jwt as jose_jwt
    from config.settings import get_settings
    settings = get_settings()
    token = websocket.query_params.get("token", "")
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return
    try:
        payload = jose_jwt.decode(token, settings.jwt_secret,
                                  algorithms=[settings.jwt_algorithm])
        tenant_id = payload.get("tenant_id", "")
    except JWTError:
        await websocket.close(code=4001, reason="Authentication required")
        return
    if not tenant_id:
        await websocket.close(code=4001, reason="Authentication required")
        return
    mgr = _c(websocket.app).fleet_ws_manager
    await mgr.connect(websocket, tenant_id=tenant_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong",
                        "timestamp": datetime.utcnow().isoformat() + "Z"})
                elif msg.get("type") == "subscribe":
                    await websocket.send_json({"type": "subscribed",
                        "message": "Subscribed to all fleet updates",
                        "timestamp": datetime.utcnow().isoformat() + "Z"})
            except json.JSONDecodeError:
                logger.warning("Malformed JSON received on /api/fleet/live (tenant_id=%s): %s", tenant_id, data)
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected normally from /api/fleet/live (tenant_id=%s)", tenant_id)
    except Exception as e:
        logger.error(
            "Unexpected WebSocket error on /api/fleet/live: endpoint=/api/fleet/live tenant_id=%s exception_type=%s error=%s traceback=%s",
            tenant_id, type(e).__name__, str(e), traceback.format_exc()
        )
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass
    finally:
        await mgr.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
