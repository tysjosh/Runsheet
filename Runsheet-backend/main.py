"""
Runsheet Logistics API — Application entry point.

All service initialization is delegated to the bootstrap/ package.
This file contains only app creation, lifespan, router inclusion,
and WebSocket/health endpoint definitions.

Requirements: 1.3, 1.6, 2.5
"""
from contextlib import asynccontextmanager
import json
import logging
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse

from bootstrap import ServiceContainer, initialize_all, shutdown_all
from data_endpoints import router as data_router
from ops.webhooks.receiver import router as webhook_router
from ops.api.endpoints import router as ops_router
from fuel.api.endpoints import router as fuel_router
from scheduling.api.endpoints import router as scheduling_router
from agent_endpoints import router as agent_router
from inline_endpoints import router as inline_router

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

# Routers (middleware is registered by bootstrap/middleware.py)
app.include_router(data_router)
app.include_router(webhook_router)
app.include_router(ops_router)
app.include_router(fuel_router)
app.include_router(scheduling_router)
app.include_router(agent_router)
app.include_router(inline_router)


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
    tenant_id = ""
    if token:
        try:
            payload = jose_jwt.decode(token, c.settings.jwt_secret,
                                      algorithms=[c.settings.jwt_algorithm])
            tenant_id = payload.get("tenant_id", "")
        except JWTError:
            pass
    mgr = c.ops_ws_manager
    await mgr.connect(websocket, tenant_id=tenant_id)
    if websocket not in mgr._clients:
        return
    try:
        while True:
            raw = await websocket.receive_text()
            await mgr.handle_client_message(websocket, raw)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await mgr.disconnect(websocket)

@app.websocket("/ws/scheduling")
async def scheduling_live_websocket(websocket: WebSocket):
    c = _c(websocket.app)
    subs = websocket.query_params.get("subscriptions", "")
    subs_list = [s.strip() for s in subs.split(",") if s.strip()] if subs else None
    mgr = c.scheduling_ws_manager
    await mgr.connect(websocket, subscriptions=subs_list)
    try:
        while True:
            raw = await websocket.receive_text()
            await mgr.handle_client_message(websocket, raw)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await mgr.disconnect(websocket)

@app.websocket("/ws/agent-activity")
async def agent_activity_websocket(websocket: WebSocket):
    mgr = _c(websocket.app).agent_ws_manager
    await mgr.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong",
                        "timestamp": datetime.utcnow().isoformat() + "Z"})
            except json.JSONDecodeError:
                pass
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await mgr.disconnect(websocket)

@app.websocket("/api/fleet/live")
async def fleet_live_websocket(websocket: WebSocket):
    mgr = _c(websocket.app).fleet_ws_manager
    await mgr.connect(websocket)
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
                pass
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await mgr.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
