"""
Driver PIN routers — the driver's own three operations and the administrator's
revocation.

Four handlers, no business rule of its own. Every rule about what a PIN is, what
makes one weak, the order in which a rotation verifies and then validates, and
what a response body may carry lives in
:mod:`driver.services.driver_pin_service` — so a direct service call and an HTTP
request behave identically.

Two routers, because the two audiences are two different surfaces:

* ``router`` (``/api/driver/pin``) is the driver acting on **their own** PIN.
  ``require_driver_identity`` runs as each handler's first statement and supplies
  the ``driver_id``; there is no ``driver_id`` field in any body, path, or query
  string on this router, so a driver cannot set, rotate, or read the enrollment
  state of anyone else's PIN.
* ``admin_router`` (``/api/ops/drivers/{driver_id}/pin``) is an administrator
  revoking someone else's, gated by ``require_role(tenant, "admin")`` and
  scoped to the caller's tenant by the vault ref (R2.10). It sits beside the
  rest of the ops driver surface rather than under ``/api/driver`` because its
  caller is not a driver and its subject is not the caller.

**No PIN material can reach a response, including a validation error (R2.7).**
That is why every PIN field on the request models below is typed ``Any`` with a
default of ``None`` instead of a constrained ``str``: FastAPI's default
``RequestValidationError`` handler echoes the offending ``input`` back to the
client, and for a *missing* field that ``input`` is the **whole request body** —
which on this surface contains a PIN. Typing the fields so that no
missing-field and no type error can fire for them moves every rejection into
:func:`~driver.services.driver_pin_service.validate_pin`, which raises the
structured 422 and names only the rule it broke.

Every rejection is an ``AppException`` from ``errors/exceptions.py`` — this
module raises **zero** raw ``HTTPException`` (R15.10).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Error Codes and
§Bootstrap Wiring (``Driver_PIN_Service``, Phase 2).

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.authorization import require_driver_identity, require_role
from config.settings import get_settings
from driver.services.driver_pin_service import (
    DriverPinService,
    get_pin_service,
)
from errors.exceptions import internal_error
from middleware.rate_limiter import driver_rate_key, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

router = APIRouter(prefix="/api/driver", tags=["driver-pin"])
admin_router = APIRouter(prefix="/api/ops/drivers", tags=["driver-pin-admin"])


# ---------------------------------------------------------------------------
# Collaborator access
# ---------------------------------------------------------------------------
#
# There is no ``configure_*`` here: the collaborators are wired once by
# ``driver/services/driver_pin_service.configure_pin_endpoints`` and read back
# through its accessor, so the router and any other reader cannot end up holding
# two different service instances.


def _require_pin_service() -> DriverPinService:
    """Return the configured :class:`DriverPinService`, failing closed."""
    service = get_pin_service()
    if service is None:
        logger.error(
            "Driver PIN endpoints not configured — no pin_vault reached "
            "configure_pin_endpoints() during startup."
        )
        raise internal_error(
            message="Driver PIN management is temporarily unavailable",
            details={"reason": "pin_endpoints_not_configured"},
        )
    return service


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


def _envelope(data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """Wrap a PIN-free service result in the driver-surface response envelope."""
    return {"data": data, "request_id": _get_request_id(request)}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class PinEnrollmentRequest(BaseModel):
    """Body for ``POST /api/driver/pin``.

    There is no ``driver_id`` field: the subject is the verified session's
    driver. ``pin`` is typed ``Any`` and defaults to ``None`` deliberately — see
    the module docstring; the format and strength rules are applied by
    :func:`~driver.services.driver_pin_service.validate_pin`, which rejects a
    non-string, an absent value, and a wrong length with the same structured 422
    and echoes nothing back.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.7
    """

    model_config = ConfigDict(extra="forbid")

    pin: Any = Field(default=None, description="4 to 8 decimal digits")


class PinRotationRequest(BaseModel):
    """Body for ``POST /api/driver/pin/rotate``.

    Both fields are typed ``Any`` for the reason given in the module docstring:
    a missing-field validation error on this body would echo the *other* field's
    value, and both of them are PINs.

    Validates: Requirements 2.5, 2.6, 2.7
    """

    model_config = ConfigDict(extra="forbid")

    current_pin: Any = Field(default=None, description="The PIN on file")
    new_pin: Any = Field(default=None, description="4 to 8 decimal digits")


# ---------------------------------------------------------------------------
# Driver endpoints
# ---------------------------------------------------------------------------


@router.post("/pin")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def enroll_pin(
    body: PinEnrollmentRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Any:
    """Set the calling driver's own voice-agent PIN.

    An upsert: a driver who already has a PIN and submits a new one replaces it,
    which is what lets a driver who forgot theirs recover without an
    administrator revoking the old one first.

    Args:
        body: The new PIN.
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context.

    Returns:
        ``{"data": {"pin_enrolled": true}, "request_id": ...}`` with HTTP 200.
        No hash, salt, or iteration count (R2.7).

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` / ``DRIVER_IDENTITY_MISSING``
            for a caller that is not a driver; 422 ``INVALID_PIN_FORMAT`` for
            anything other than 4 to 8 decimal digits; 422 ``WEAK_PIN`` for a
            repeated digit or a strictly monotonic sequence.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.7
    """
    driver_id = require_driver_identity(tenant)
    result = await _require_pin_service().enroll(
        tenant.tenant_id, driver_id, body.pin
    )
    return _envelope(result, request)


@router.post("/pin/rotate")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def rotate_pin(
    body: PinRotationRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Any:
    """Replace the calling driver's PIN, proving the current one first.

    The only handler on this surface that verifies a PIN, and so the only one the
    R2.8 attempt lockout applies to: five consecutive failures inside 15 minutes
    lock the driver out for 15 minutes. Enrollment is untouched by the lockout —
    it proves nothing, and it is the recovery path for a driver who has forgotten
    their PIN.

    Args:
        body: The current PIN and its replacement.
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context.

    Returns:
        ``{"data": {"pin_enrolled": true}, "request_id": ...}`` with HTTP 200.

    Raises:
        AppException: 429 ``PIN_ATTEMPTS_EXCEEDED`` while the lockout is in
            force, checked before the vault is consulted so the 429 pre-empts the
            403; 403 ``PIN_VERIFICATION_FAILED`` when the current PIN does not
            verify — a driver with no PIN on file gets the same answer, so the
            endpoint reveals nothing about enrollment; 422
            ``INVALID_PIN_FORMAT`` or ``WEAK_PIN`` for the replacement, judged
            only after the current PIN has verified.

    Validates: Requirements 2.5, 2.6, 2.7, 2.8
    """
    driver_id = require_driver_identity(tenant)
    result = await _require_pin_service().rotate(
        tenant.tenant_id, driver_id, body.current_pin, body.new_pin
    )
    return _envelope(result, request)


@router.get("/pin")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def read_pin_enrollment_state(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Any:
    """Report whether the calling driver has a PIN on file.

    Args:
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context.

    Returns:
        ``{"data": {"pin_enrolled": <bool>}, "request_id": ...}`` — the state as
        a single boolean and nothing else (R2.9).

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` / ``DRIVER_IDENTITY_MISSING``
            for a caller that is not a driver.

    Validates: Requirements 2.7, 2.9
    """
    driver_id = require_driver_identity(tenant)
    result = await _require_pin_service().enrollment_state(
        tenant.tenant_id, driver_id
    )
    return _envelope(result, request)


# ---------------------------------------------------------------------------
# Administrator endpoint
# ---------------------------------------------------------------------------


@admin_router.delete("/{driver_id}/pin")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def revoke_pin(
    driver_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Any:
    """Revoke a driver's PIN and record the audit event.

    Idempotent: revoking a PIN that is not on file leaves the driver in the
    state the caller asked for, answers 200, and audits the outcome as
    ``no_pin_on_file``. The vault ref is built from the caller's own tenant, so
    an administrator cannot revoke across tenants regardless of the
    ``driver_id`` supplied.

    Args:
        driver_id: The driver whose PIN is revoked.
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context of the acting administrator.

    Returns:
        ``{"data": {"driver_id": ..., "pin_enrolled": false, "pin_existed":
        <bool>}, "request_id": ...}`` with HTTP 200.

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` for a caller holding only
            ``dispatcher`` or only ``driver``.

    Validates: Requirements 2.7, 2.10
    """
    require_role(tenant, "admin")
    result = await _require_pin_service().revoke(tenant, driver_id)
    return _envelope(result, request)


__all__ = [
    "router",
    "admin_router",
    "PinEnrollmentRequest",
    "PinRotationRequest",
]
