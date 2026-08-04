"""
Device_Registry router — ``PUT`` and ``DELETE /api/driver/devices/{device_id}``.

Two handlers over :class:`~driver.services.device_registry.DeviceRegistry`. The
registry owns every rule about what a registration record *is*; this module
decides **whose** record a request may touch, and the answer is always "the
caller's own".

``driver_id`` comes from :func:`~auth.authorization.require_driver_identity` and
from nowhere else. The body may carry a ``driver_id`` — the copied
``notification-manager.ts`` sends a token payload that has historically included
one — and it is accepted and **ignored** rather than rejected, so an app build
that sends it still registers successfully, and against the session's driver.
Since the record's document id is ``{tenant_id}:{driver_id}:{device_id}``, a
session simply cannot address another driver's or another tenant's record.

``PUT`` is the whole registration contract: it creates a record on first call
and replaces it on every subsequent one (R9.2), which is what makes the app's
24-hour re-registration a ``last_seen_at`` refresh rather than a duplicate row.
``DELETE`` is sign-out (R9.3) and is idempotent — a sign-out after a session the
server has already forgotten reports ``deleted: false`` rather than failing.

Every rejection here is an ``AppException`` from ``errors/exceptions.py`` — this
module raises **zero** raw ``HTTPException`` (R15.10) — and no handler or log
statement ever emits a ``push_token`` value (R15.1).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Device registry
lifecycle.

Validates: Requirements 9.1, 9.2, 9.3, 9.18
- 9.1: registration persists the tenant, driver, device, token, platform, and a
  registration timestamp
- 9.2: re-registering a known ``device_id`` replaces the record
- 9.3: sign-out deletes the record for that ``device_id``
- 9.18: the token is handed to the registry uninterpreted
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.authorization import require_driver_identity
from config.settings import get_settings
from driver.services.device_registry import DeviceRegistry
from errors.exceptions import internal_error
from middleware.rate_limiter import driver_rate_key, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level collaborators, wired via configure_device_endpoints().
_es_service: Optional[Any] = None
_device_registry: Optional[DeviceRegistry] = None

router = APIRouter(prefix="/api/driver", tags=["driver-devices"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_device_endpoints(*, es_service: Any = None) -> None:
    """Wire the Device_Registry collaborators and build the registry.

    Called once during startup from ``bootstrap/driver.py``. Like every other
    ``configure_*`` on this surface it assigns each module global
    unconditionally, so an argument a caller omits is reset to ``None`` — every
    call site must pass its full argument set.

    ``es_service`` is the only collaborator: it is the store. Without it there
    is no registry and both handlers fail closed rather than silently accepting
    a registration that is never persisted.
    """
    global _es_service, _device_registry

    _es_service = es_service
    _device_registry = (
        DeviceRegistry(es_service=es_service) if es_service is not None else None
    )


def configured_device_registry() -> Optional[DeviceRegistry]:
    """Return the registry :func:`configure_device_endpoints` built, or ``None``.

    ``Driver_Push_Service`` reads the same records this router writes, so the
    one instance is shared rather than letting another surface construct a
    second one over the same index.
    """
    return _device_registry


def _get_device_registry() -> DeviceRegistry:
    """Return the configured :class:`DeviceRegistry`, failing closed."""
    if _device_registry is None:
        logger.error(
            "Device endpoints not configured. "
            "Call configure_device_endpoints() during startup."
        )
        raise internal_error(
            message="Device registration is temporarily unavailable",
            details={"reason": "device_endpoints_not_configured"},
        )
    return _device_registry


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class DeviceRegistrationRequest(BaseModel):
    """Body for ``PUT /api/driver/devices/{device_id}``.

    ``driver_id`` is accepted and ignored: the subject of a registration is the
    verified session's driver, never a body value. It is declared rather than
    forbidden so an app build that sends one still registers, instead of being
    rejected with a validation error it cannot act on.
    """

    model_config = ConfigDict(extra="forbid")

    push_token: str = Field(..., min_length=1, max_length=2048)
    platform: str = Field(..., min_length=1, max_length=32)
    app_version: Optional[str] = Field(default=None, max_length=64)
    driver_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description=(
            "Ignored. The registration is recorded against the verified "
            "session's driver."
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.put("/devices/{device_id}")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def register_device(
    body: DeviceRegistrationRequest,
    request: Request,
    device_id: str = Path(..., min_length=1, max_length=128),
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Register — or re-register — one device for the calling driver.

    Args:
        body: The ``push_token``, the ``platform``, and an optional
            ``app_version``. Any ``driver_id`` is ignored.
        request: The inbound request, for the correlation id.
        device_id: The app's stable per-installation device identifier.
        tenant: The verified Auth_Context.

    Returns:
        ``{"data": <record without the token>, "request_id": ...}`` with HTTP
        200. ``data.replaced`` says whether a record already existed, and
        ``data.last_seen_at`` is refreshed on every call.

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` / ``DRIVER_IDENTITY_MISSING``
            for a caller that is not a driver; 400 ``INVALID_REQUEST`` for a
            blank token or an unknown platform.

    Validates: Requirements 9.1, 9.2, 9.18
    """
    driver_id = require_driver_identity(tenant)

    record = await _get_device_registry().register(
        tenant.tenant_id,
        driver_id,
        device_id,
        push_token=body.push_token,
        platform=body.platform,
        app_version=body.app_version,
    )

    return {"data": record, "request_id": _get_request_id(request)}


@router.delete("/devices/{device_id}")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def unregister_device(
    request: Request,
    device_id: str = Path(..., min_length=1, max_length=128),
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Delete the calling driver's registration for one device (sign-out).

    Args:
        request: The inbound request, for the correlation id.
        device_id: The device whose registration to remove.
        tenant: The verified Auth_Context.

    Returns:
        ``{"data": {"device_id": ..., "driver_id": ..., "deleted": bool},
        "request_id": ...}`` with HTTP 200. ``deleted`` is ``False`` when there
        was no record to remove, which is a successful sign-out either way.

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` / ``DRIVER_IDENTITY_MISSING``
            for a caller that is not a driver.

    Validates: Requirements 9.3
    """
    driver_id = require_driver_identity(tenant)

    deleted = await _get_device_registry().unregister(
        tenant.tenant_id, driver_id, device_id
    )

    return {
        "data": {
            "device_id": device_id,
            "driver_id": driver_id,
            "deleted": deleted,
        },
        "request_id": _get_request_id(request),
    }


__all__ = [
    "router",
    "configure_device_endpoints",
    "configured_device_registry",
    "DeviceRegistrationRequest",
]
