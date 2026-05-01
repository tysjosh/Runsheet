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
    """Register all middleware on the FastAPI application."""
    from fastapi.middleware.cors import CORSMiddleware
    from middleware.request_id import RequestIDMiddleware
    from middleware.rate_limiter import setup_rate_limiting
    from middleware.security_headers import setup_security_headers

    settings = container.settings

    # CORS — only configured origins, no wildcards
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
        ],
        expose_headers=[
            "X-Request-ID",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
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

    # Auth policy matrix validation at startup (Req 5.5, 5.7)
    from middleware.auth_policy import validate_policy_matrix
    validate_policy_matrix(app)

    logger.info("Middleware registered")
