"""
Tenant Guard for the Ops Intelligence Layer.

Extracts tenant identity exclusively from signed JWT claims and injects
tenant-scoped filters into every Elasticsearch query. Ignores tenant_id
from query parameters, request headers (other than JWT), or unsigned
payload fields to prevent tenant spoofing.

Validates: Requirements 9.1, 9.2, 9.4, 9.6, 9.8
"""

import logging
from dataclasses import dataclass

from fastapi import Depends, Request
from jose import JWTError, jwt

from config.settings import get_settings
from errors.exceptions import forbidden

logger = logging.getLogger(__name__)

# Default tenant context for development mode (no JWT required)
_DEV_TENANT = None


@dataclass
class TenantContext:
    """Verified tenant identity extracted from a signed JWT."""

    tenant_id: str
    user_id: str
    has_pii_access: bool


async def get_tenant_context(request: Request) -> TenantContext:
    """
    FastAPI dependency that extracts tenant_id exclusively from the
    signed JWT ``tenant_id`` claim.

    In development mode, if no Authorization header is present, returns
    a default dev tenant context to allow frontend access without JWT.

    Validates: Requirements 9.1, 9.6, 9.8
    - Derives tenant_id only from the signed JWT token
    - Rejects requests where the JWT claim is missing or invalid (403)
    - Ignores any tenant_id in query params, headers, or unsigned fields
    """
    settings = get_settings()

    # Extract the Authorization header
    auth_header: str | None = request.headers.get("Authorization")

    # In development mode, allow unauthenticated access with a default tenant
    if (not auth_header or not auth_header.startswith("Bearer ")) and settings.environment.value == "development":
        logger.debug(
            "Dev mode: returning default tenant context for %s %s",
            request.method,
            request.url.path,
        )
        return TenantContext(
            tenant_id="dev-tenant",
            user_id="dev-user",
            has_pii_access=True,
        )

    if not auth_header or not auth_header.startswith("Bearer "):
        logger.debug(
            "Tenant guard rejected request: missing or malformed Authorization header "
            "for %s %s",
            request.method,
            request.url.path,
        )
        raise forbidden(
            message="Missing or invalid authentication token",
            details={"reason": "Authorization header with Bearer token is required"},
        )

    token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        raise forbidden(
            message="Missing or invalid authentication token",
            details={"reason": "Bearer token is empty"},
        )

    # Decode and verify the JWT
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        logger.debug("Tenant guard JWT verification failed: %s", exc)
        raise forbidden(
            message="Invalid authentication token",
            details={"reason": "JWT verification failed"},
        )

    # Extract tenant_id — must be present in the signed claims
    tenant_id: str | None = payload.get("tenant_id")
    if not tenant_id:
        logger.debug(
            "Tenant guard rejected request: JWT missing tenant_id claim for %s %s",
            request.method,
            request.url.path,
        )
        raise forbidden(
            message="Missing tenant_id in authentication token",
            details={"reason": "JWT must contain a tenant_id claim"},
        )

    user_id: str = payload.get("sub", payload.get("user_id", "unknown"))
    has_pii_access: bool = payload.get("has_pii_access", False)

    logger.debug(
        "Tenant scope enforced: tenant_id=%s user_id=%s endpoint=%s %s",
        tenant_id,
        user_id,
        request.method,
        request.url.path,
    )

    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        has_pii_access=has_pii_access,
    )


def inject_tenant_filter(query: dict, tenant_id: str) -> dict:
    """
    Wrap any Elasticsearch query with a bool filter on ``tenant_id``.

    Applied to all read endpoints (shipments, riders, events, metrics)
    to enforce tenant-scoped data isolation.

    Validates: Requirements 9.2, 9.4
    """
    return {
        "query": {
            "bool": {
                "must": [query.get("query", {"match_all": {}})],
                "filter": [{"term": {"tenant_id": tenant_id}}],
            }
        }
    }
