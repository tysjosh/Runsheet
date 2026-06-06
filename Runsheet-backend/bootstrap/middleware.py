"""
Middleware bootstrap module.

Registers: CORS, RequestID, RateLimit, SecurityHeaders middleware on the
FastAPI app.

Requirements: 1.1, 1.2
"""
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


async def initialize(app, container: ServiceContainer) -> None:
    """Register all middleware on the FastAPI application.

    .. note::
        The global auth enforcement layer (the SuperTokens SDK middleware and
        :class:`middleware.auth_enforcement.AuthEnforcementMiddleware`) and the
        CORS middleware are registered in ``main.py`` at **import time**, not
        here. Starlette builds its middleware stack before the lifespan startup
        runs, so ``add_middleware`` calls made during this bootstrap step raise
        ``RuntimeError("Cannot add middleware after an application has
        started")`` and have no effect. The CORS config below is kept in sync
        with ``main.py`` (including the SuperTokens headers) for parity, and the
        dead ``validate_policy_matrix`` startup call has been removed now that
        global enforcement is live (Req 6.1).
    """
    from fastapi.middleware.cors import CORSMiddleware
    from middleware.request_id import RequestIDMiddleware
    from middleware.rate_limiter import setup_rate_limiting
    from middleware.security_headers import setup_security_headers

    settings = container.settings

    # CORS — only configured origins, no wildcards. Header lists mirror
    # ``main.py`` and include the SuperTokens session/anti-CSRF headers so the
    # frontend SDK flow passes CORS preflight (Req 2.5, 2.3, 8.4).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Accept-Language",
            "Content-Language",
            "Content-Type",
            "Authorization",
            "X-Request-ID",
            "X-Requested-With",
            # SuperTokens SDK request headers (anti-CSRF token, recipe/FDI
            # routing, session transport mode).
            "anti-csrf",
            "rid",
            "fdi-version",
            "st-auth-mode",
        ],
        expose_headers=[
            "X-Request-ID",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            # SuperTokens session-issuance response headers the browser SDK
            # reads across origins.
            "front-token",
            "anti-csrf",
        ],
        max_age=600,
    )

    # Request ID
    app.add_middleware(RequestIDMiddleware)

    # Rate limiting
    setup_rate_limiting(
        app,
        api_rate_limit=settings.rate_limit_requests_per_minute,
        ai_rate_limit=settings.rate_limit_ai_requests_per_minute,
    )

    # Security headers
    setup_security_headers(app)

    logger.info("Middleware registered")
