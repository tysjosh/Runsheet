"""
Tenant Guard for the Ops Intelligence Layer.

Extracts tenant identity exclusively from signed JWT claims and injects
tenant-scoped filters into every Elasticsearch query. Ignores tenant_id
from query parameters, request headers (other than JWT), or unsigned
payload fields to prevent tenant spoofing.

Validates: Requirements 9.1, 9.2, 9.4, 9.6, 9.8, 6.1.5, 6.3.1
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import Depends, Request
from jose import JWTError, jwt

from config.settings import get_settings
from errors.exceptions import forbidden
from services.tenant_settings import (
    MeasurementUnits,
    TenantSettings,
    TenantSettingsService,
    default_measurement_units_for_region,
    default_tenant_settings,
)

logger = logging.getLogger(__name__)

request_tenant_id_var: ContextVar[str] = ContextVar(
    "request_tenant_id", default=""
)

# Module-level tenant settings service — configured once at bootstrap via
# ``configure_tenant_guard``. Remains None in tests that never wire it, in
# which case the middleware falls back to in-process US/imperial defaults so
# no request path breaks due to missing infrastructure.
_tenant_settings_service: Optional[TenantSettingsService] = None


def configure_tenant_guard(
    tenant_settings_service: Optional[TenantSettingsService],
) -> None:
    """Register the tenant settings service used to hydrate TenantContext.

    Called from the bootstrap layer once Redis is available. Passing ``None``
    resets the wiring (useful for tests that need an isolated baseline).

    Validates: Requirement 6.1.5, 6.3.1.
    """
    global _tenant_settings_service
    _tenant_settings_service = tenant_settings_service


def get_tenant_settings_service() -> Optional[TenantSettingsService]:
    """Return the currently wired :class:`TenantSettingsService`, or ``None``."""
    return _tenant_settings_service


@dataclass
class TenantContext:
    """Verified tenant identity extracted from a signed JWT.

    Fields beyond the JWT claims (``region`` and ``measurement_units``) come
    from the tenant settings service and default to the platform's
    US/imperial values when a tenant has no explicit record. The tenant guard
    middleware populates them on every request so downstream handlers can
    render volumes and distances in the tenant's preferred units without
    re-querying the settings store.
    """

    tenant_id: str
    user_id: str
    has_pii_access: bool
    roles: list[str] = field(default_factory=list)
    region: str = "US"
    measurement_units: Dict[str, str] = field(
        default_factory=lambda: default_measurement_units_for_region("US").to_dict()
    )


async def _load_tenant_settings(tenant_id: str) -> TenantSettings:
    """Fetch tenant settings via the configured service, with safe defaults.

    Returns the default ``TenantSettings`` whenever the service is missing or
    a lookup fails, so a flaky settings backend never locks users out.
    """
    service = _tenant_settings_service
    if service is None:
        return default_tenant_settings()
    try:
        return await service.get(tenant_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Tenant guard: settings lookup failed for tenant=%s: %s — using defaults",
            tenant_id,
            exc,
        )
        return default_tenant_settings()


def _build_context(
    *,
    tenant_id: str,
    user_id: str,
    has_pii_access: bool,
    roles: list[str],
    settings: TenantSettings,
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        has_pii_access=has_pii_access,
        roles=roles,
        region=settings.region,
        measurement_units=settings.measurement_units.to_dict(),
    )


async def get_tenant_context(request: Request) -> TenantContext:
    """
    FastAPI dependency that extracts tenant_id exclusively from the
    signed JWT ``tenant_id`` claim.

    Validates: Requirements 9.1, 9.6, 9.8, 6.1.5, 6.3.1
    - Derives tenant_id only from the signed JWT token
    - Rejects requests where the JWT claim is missing or invalid (403)
    - Ignores any tenant_id in query params, headers, or unsigned fields
    - Hydrates region + measurement_units from the tenant settings service,
      falling back to the US/imperial defaults when unset
    """
    settings_obj = get_settings()

    # Extract the Authorization header
    auth_header: str | None = request.headers.get("Authorization")

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
            settings_obj.jwt_secret,
            algorithms=[settings_obj.jwt_algorithm],
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

    request_tenant_id_var.set(tenant_id)
    user_id: str = payload.get("sub", payload.get("user_id", "unknown"))
    has_pii_access: bool = payload.get("has_pii_access", False)
    roles: list[str] = payload.get("roles", [])

    tenant_settings = await _load_tenant_settings(tenant_id)

    logger.debug(
        "Tenant scope enforced: tenant_id=%s user_id=%s region=%s endpoint=%s %s",
        tenant_id,
        user_id,
        tenant_settings.region,
        request.method,
        request.url.path,
    )

    return _build_context(
        tenant_id=tenant_id,
        user_id=user_id,
        has_pii_access=has_pii_access,
        roles=roles,
        settings=tenant_settings,
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
