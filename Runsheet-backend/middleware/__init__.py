"""
Middleware components for the Runsheet backend.

This module contains FastAPI middleware for cross-cutting concerns
such as request correlation, logging, and security.
"""

from middleware.request_id import RequestIDMiddleware, request_id_var
from middleware.rate_limiter import (
    limiter,
    setup_rate_limiting,
    api_rate_limit,
    ai_rate_limit,
    get_client_ip,
    AI_CHAT_PATHS,
    is_ai_chat_endpoint,
)
from middleware.security_headers import (
    SecurityHeadersMiddleware,
    setup_security_headers,
    build_csp_header,
    DEFAULT_CSP_DIRECTIVES,
)

__all__ = [
    "RequestIDMiddleware",
    "request_id_var",
    "limiter",
    "setup_rate_limiting",
    "api_rate_limit",
    "ai_rate_limit",
    "get_client_ip",
    "AI_CHAT_PATHS",
    "is_ai_chat_endpoint",
    "SecurityHeadersMiddleware",
    "setup_security_headers",
    "build_csp_header",
    "DEFAULT_CSP_DIRECTIVES",
]
