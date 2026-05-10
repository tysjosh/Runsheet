"""
Runsheet Logistics API — Application entry point.

All service initialization is delegated to the bootstrap/ package.
This file contains only app creation, lifespan, router inclusion,
and health endpoint definitions.

WebSocket endpoints live in :mod:`bootstrap.websockets` and CORS is
configured by :mod:`bootstrap.middleware`, keeping this entrypoint
below its ≤350 line budget (Req 1.6).

Requirements: 1.3, 1.6, 2.5
"""
# Load .env.<environment> BEFORE any imports so GEMINI_API_KEY etc. are set
import os
from dotenv import load_dotenv
_env = os.environ.get("ENVIRONMENT", "development").lower()
_env_file = f".env.{_env}" if os.path.exists(f".env.{_env}") else ".env"
load_dotenv(_env_file, override=True)

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bootstrap import ServiceContainer, initialize_all, shutdown_all
from bootstrap.websockets import register_websocket_routes
from errors.handlers import register_exception_handlers
from data_endpoints import router as data_router
from ops.webhooks.receiver import router as webhook_router
from ops.api.endpoints import router as ops_router
from fuel.api.endpoints import router as fuel_router
from inventory.api.endpoints import router as inventory_router
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
from integrations.api.integrations_endpoints import router as integrations_router
from integrations.api.intake_channel_endpoints import router as intake_channel_router
from fuel.api.order_endpoints import router as order_router
from fuel.api.order_webhook_endpoints import router as order_webhook_router
from fuel.api.driver_endpoints import router as driver_ops_router
from integrations.api.stripe_endpoints import (
    router as stripe_router,
    webhook_router as stripe_webhook_router,
)
from commerce.api.customer_endpoints import (
    router as commerce_customer_router,
    configure_customer_api,
)
from commerce.api.account_endpoints import (
    router as commerce_account_router,
    configure_account_api,
)
from commerce.api.price_book_endpoints import (
    router as commerce_price_book_router,
    pricing_router as commerce_pricing_router,
    configure_price_book_api,
)
from commerce.api.invoice_endpoints import (
    router as commerce_invoice_router,
    configure_invoice_api,
)
from commerce.api.payment_endpoints import (
    router as commerce_payment_router,
    configure_payment_api,
)
from commerce.api.ar_aging_endpoints import (
    router as commerce_ar_aging_router,
    configure_ar_aging_api,
)
from compliance.api.tax_endpoints import (
    router as compliance_tax_router,
)
from compliance.api.terminal_bol_endpoints import (
    router as compliance_terminal_bol_router,
)
from commerce.api.price_protection_endpoints import (
    router as commerce_price_protection_router,
)
from commerce.api.pricing_endpoints import (
    router as commerce_pricing_rules_router,
)

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
import json as _json
_cors_raw = os.environ.get(
    "CORS_ORIGINS", '["http://localhost:3000", "http://127.0.0.1:3000"]'
)
try:
    _cors_origins = _json.loads(_cors_raw)
except Exception as e:  # noqa: BLE001
    logger.warning(f"Failed to parse CORS_ORIGINS: {e}, using defaults")
    _cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=[
        "Accept", "Accept-Language", "Content-Language", "Content-Type",
        "Authorization", "X-Request-ID", "X-Requested-With", "X-Idempotency-Key",
    ],
    expose_headers=[
        "X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining",
        "X-RateLimit-Reset", "X-Idempotent-Replayed",
    ],
    max_age=600,
)

# Routers (middleware is registered by bootstrap/middleware.py).
# The Integration Marketplace and Stripe routers are configured by
# bootstrap/agents.py where the credentials vault, instance repository,
# scheduler, and connector factory are wired; router inclusion lives
# here so every top-level REST surface is discoverable in one place.
for _router in (
    data_router, webhook_router, ops_router, fuel_router, inventory_router,
    scheduling_router, driver_scheduling_router, message_router,
    exception_router, pod_router, agent_router, inline_router, import_router,
    notification_router, metrics_router, integrations_router,
    intake_channel_router, order_router, order_webhook_router,
    driver_ops_router,
    stripe_router, stripe_webhook_router,
    compliance_tax_router,
    compliance_terminal_bol_router,
    commerce_price_protection_router,
    commerce_pricing_rules_router,
):
    app.include_router(_router)

# Commerce Backbone routers — conditionally included when the master
# feature flag is on. Individual endpoint-level guards still check
# sub-flags per-request (Req 8.1, 8.2).
from config.settings import get_settings as _get_settings

try:
    _settings = _get_settings()
    if getattr(_settings, "commerce_backbone_enabled", False):
        app.include_router(commerce_customer_router)
        app.include_router(commerce_account_router)
        app.include_router(commerce_price_book_router)
        app.include_router(commerce_pricing_router)
        app.include_router(commerce_invoice_router)
        app.include_router(commerce_payment_router)
        app.include_router(commerce_ar_aging_router)
except Exception:
    # Settings may not load cleanly at import time in test environments;
    # the router will be registered during lifespan if needed.
    pass


def _c(app: FastAPI) -> ServiceContainer:
    return app.state.container


# Health endpoints
@app.get("/")
async def root():
    return {"message": "Runsheet Logistics API is running"}


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy", "service": "Runsheet Logistics API",
        "agent": "LogisticsAgent", "version": "1.0.0",
    }


@app.get("/health")
async def health_basic(request: Request):
    result = await _c(request.app).health_check_service.check_health()
    return {
        "status": result["status"], "service": "Runsheet Logistics API",
        "version": "1.0.0", "timestamp": result["timestamp"],
    }


@app.get("/health/ready")
async def health_ready(request: Request):
    hs = await _c(request.app).health_check_service.check_readiness()
    data = {
        "status": hs.status, "service": "Runsheet Logistics API",
        "version": "1.0.0", "timestamp": hs.timestamp.isoformat() + "Z",
        "dependencies": [d.to_dict() for d in hs.dependencies],
    }
    if hs.status == "unhealthy":
        data["failure_reasons"] = [
            {"dependency": d.name, "error": d.error}
            for d in hs.dependencies if not d.healthy
        ]
        return JSONResponse(status_code=503, content=data)
    return data


@app.get("/health/live")
async def health_live(request: Request):
    result = await _c(request.app).health_check_service.check_liveness()
    return {
        "status": result["status"], "service": "Runsheet Logistics API",
        "version": "1.0.0", "timestamp": result["timestamp"],
    }


# WebSocket endpoints (bootstrap/websockets.py)
register_websocket_routes(app)

# ---------------------------------------------------------------------------
# Backwards-compatible re-exports
#
# The WebSocket auth helpers and log/handler utilities used to live in
# this module. They now live in :mod:`bootstrap.websockets`, but tests
# and external callers still import ``main._ws_authenticate`` /
# ``main._ws_authenticate_driver`` and patch ``main.logger``. The
# aliases below keep both seams working; ``bootstrap.websockets``
# resolves its logger through ``main.logger`` at call time so patches
# continue to surface every WS warning.
# ---------------------------------------------------------------------------
from bootstrap.websockets import (  # noqa: E402  (after app creation)
    _authenticate_driver as _ws_authenticate_driver,
    _authenticate_tenant as _ws_authenticate,
    _json_echo_handler,
    _ws_loop,
)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
