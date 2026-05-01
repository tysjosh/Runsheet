"""
Centralized authentication and tenant scoping policy.

Defines an AuthPolicy enum and a middleware/dependency that enforces
the declared policy for every request. Routers declare their default
policy; per-route overrides are supported via dependency injection.

Requirements: 5.1–5.7
"""
import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AuthPolicy enum (Req 5.1)
# ---------------------------------------------------------------------------

class AuthPolicy(str, Enum):
    """Authentication policy for endpoints."""
    JWT_REQUIRED = "jwt_required"
    API_KEY_REQUIRED = "api_key_required"
    WEBHOOK_HMAC = "webhook_hmac"
    PUBLIC = "public"


# ---------------------------------------------------------------------------
# Policy matrix — maps route prefixes to their default AuthPolicy (Req 5.6)
# ---------------------------------------------------------------------------

POLICY_MATRIX: Dict[str, AuthPolicy] = {
    "/api/scheduling": AuthPolicy.JWT_REQUIRED,
    "/api/ops/admin": AuthPolicy.JWT_REQUIRED,   # admin role required
    "/api/ops": AuthPolicy.JWT_REQUIRED,
    "/api/fuel": AuthPolicy.JWT_REQUIRED,
    "/api/agent": AuthPolicy.JWT_REQUIRED,
    "/api/chat": AuthPolicy.JWT_REQUIRED,
    "/api/chat/clear": AuthPolicy.JWT_REQUIRED,
    "/api/data": AuthPolicy.JWT_REQUIRED,
    "/ws": AuthPolicy.JWT_REQUIRED,
    "/health": AuthPolicy.PUBLIC,
    "/docs": AuthPolicy.PUBLIC,
    "/openapi.json": AuthPolicy.PUBLIC,
    "/redoc": AuthPolicy.PUBLIC,
    "/": AuthPolicy.PUBLIC,
}

# Per-route exceptions — override the default policy for specific routes (Req 5.6)
POLICY_EXCEPTIONS: Dict[str, AuthPolicy] = {
    "GET /api/agent/health": AuthPolicy.PUBLIC,
    "GET /api/health": AuthPolicy.PUBLIC,
    "GET /ws/agent-activity": AuthPolicy.PUBLIC,  # read-only
}


# ---------------------------------------------------------------------------
# Startup validation (Req 5.5, 5.7)
# ---------------------------------------------------------------------------

def validate_policy_matrix(app: Any) -> List[str]:
    """Compare declared policies against registered routes at startup.

    Logs warnings for any route without an explicit policy declaration.
    Unmatched routes default to JWT_REQUIRED.

    Args:
        app: The FastAPI application instance.

    Returns:
        List of route paths that had no explicit policy match (for testing).
    """
    unmatched: List[str] = []

    for route in app.routes:
        path = getattr(route, "path", "")
        if not path:
            continue

        # Check if route matches any prefix in the policy matrix
        matched = False
        for prefix in POLICY_MATRIX:
            if prefix == "/":
                # Root "/" only matches exactly "/"
                if path == "/":
                    matched = True
                    break
            elif path == prefix or path.startswith(prefix + "/") or path.startswith(prefix):
                matched = True
                break

        # Check exceptions
        methods = getattr(route, "methods", set())
        for method in methods:
            exception_key = f"{method} {path}"
            if exception_key in POLICY_EXCEPTIONS:
                matched = True
                break

        if not matched:
            unmatched.append(path)
            logger.warning(
                "Route %s has no explicit AuthPolicy — defaulting to JWT_REQUIRED",
                path,
            )

    if not unmatched:
        logger.info("All registered routes have explicit AuthPolicy declarations.")

    return unmatched


def get_policy_for_route(method: str, path: str) -> AuthPolicy:
    """Determine the effective AuthPolicy for a given method + path.

    Checks POLICY_EXCEPTIONS first, then matches against POLICY_MATRIX
    prefixes. Falls back to JWT_REQUIRED if no match is found.

    Args:
        method: HTTP method (e.g., "GET", "POST").
        path: The route path (e.g., "/api/agent/health").

    Returns:
        The effective AuthPolicy for this route.
    """
    # Check per-route exceptions first
    exception_key = f"{method} {path}"
    if exception_key in POLICY_EXCEPTIONS:
        return POLICY_EXCEPTIONS[exception_key]

    # Match against policy matrix prefixes (longest prefix first)
    sorted_prefixes = sorted(POLICY_MATRIX.keys(), key=len, reverse=True)
    for prefix in sorted_prefixes:
        if path == prefix or path.startswith(prefix + "/") or (
            prefix != "/" and path.startswith(prefix)
        ):
            return POLICY_MATRIX[prefix]

    # Special case: exact match for "/"
    if path == "/" and "/" in POLICY_MATRIX:
        return POLICY_MATRIX["/"]

    # Default to JWT_REQUIRED for unmatched routes (Req 5.5)
    return AuthPolicy.JWT_REQUIRED


# ---------------------------------------------------------------------------
# Tenant context model (Req 5.4)
# ---------------------------------------------------------------------------

class TenantContext(BaseModel):
    """Extracted tenant context from JWT claims."""
    tenant_id: str
    user_id: Optional[str] = None
    roles: list[str] = []


# ---------------------------------------------------------------------------
# Auth enforcement dependency (Req 5.3)
# ---------------------------------------------------------------------------

async def enforce_auth_policy(request: Request) -> Optional[dict]:
    """FastAPI dependency that enforces the declared AuthPolicy for each request.

    Checks the effective policy for the current route and:
    - PUBLIC routes: allows unauthenticated access, returns None.
    - JWT_REQUIRED routes: verifies the Authorization header contains a valid JWT.
      Returns the decoded JWT payload on success, raises 401 on failure.
    - API_KEY_REQUIRED routes: verifies the X-API-Key header.
    - WEBHOOK_HMAC routes: delegates to webhook-specific verification.

    Returns:
        Decoded JWT payload dict for authenticated routes, or None for PUBLIC routes.

    Raises:
        HTTPException: 401 if authentication fails for a protected route.
    """
    method = request.method
    path = request.url.path
    policy = get_policy_for_route(method, path)

    if policy == AuthPolicy.PUBLIC:
        return None

    if policy == AuthPolicy.JWT_REQUIRED:
        return await _verify_jwt(request)

    if policy == AuthPolicy.API_KEY_REQUIRED:
        return await _verify_api_key(request)

    if policy == AuthPolicy.WEBHOOK_HMAC:
        # Webhook HMAC verification is handled by the webhook receiver itself
        return None

    # Fallback — should not reach here
    return None


async def _verify_jwt(request: Request) -> dict:
    """Verify JWT from the Authorization header.

    Returns the decoded payload on success.
    Raises 401 HTTPException on failure.
    """
    from schemas.common import ErrorResponse

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=ErrorResponse(
                error_code="AUTH_REQUIRED",
                message="Missing or invalid Authorization header. Expected: Bearer <token>",
                details=None,
                request_id=getattr(request.state, "request_id", "unknown"),
            ).model_dump(),
        )

    token = auth_header[7:]  # Strip "Bearer "

    try:
        from jose import JWTError, jwt as jose_jwt
        from config.settings import get_settings

        settings = get_settings()
        payload = jose_jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except Exception:
        raise HTTPException(
            status_code=401,
            detail=ErrorResponse(
                error_code="AUTH_INVALID_TOKEN",
                message="Invalid or expired JWT token",
                details=None,
                request_id=getattr(request.state, "request_id", "unknown"),
            ).model_dump(),
        )


async def _verify_api_key(request: Request) -> dict:
    """Verify API key from the X-API-Key header.

    Returns a minimal payload dict on success.
    Raises 401 HTTPException on failure.
    """
    from schemas.common import ErrorResponse

    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail=ErrorResponse(
                error_code="API_KEY_REQUIRED",
                message="Missing X-API-Key header",
                details=None,
                request_id=getattr(request.state, "request_id", "unknown"),
            ).model_dump(),
        )

    # In production, validate against a stored key list
    # For now, accept any non-empty key
    return {"api_key": api_key}


# ---------------------------------------------------------------------------
# Tenant scoping dependency (Req 5.4)
# ---------------------------------------------------------------------------

async def require_tenant(request: Request) -> TenantContext:
    """FastAPI dependency that extracts tenant_id from JWT claims.

    Verifies the JWT and extracts tenant context including tenant_id,
    user_id, and roles.

    Returns:
        TenantContext with extracted claims.

    Raises:
        HTTPException: 401 if no valid JWT is present or tenant_id is missing.
    """
    from schemas.common import ErrorResponse

    payload = await _verify_jwt(request)

    tenant_id = payload.get("tenant_id", "")
    if not tenant_id:
        raise HTTPException(
            status_code=401,
            detail=ErrorResponse(
                error_code="TENANT_REQUIRED",
                message="JWT does not contain a tenant_id claim",
                details=None,
                request_id=getattr(request.state, "request_id", "unknown"),
            ).model_dump(),
        )

    return TenantContext(
        tenant_id=tenant_id,
        user_id=payload.get("sub", payload.get("user_id")),
        roles=payload.get("roles", []),
    )
