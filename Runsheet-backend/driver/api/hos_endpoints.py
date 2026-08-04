"""
HOS_Advisory router — ``GET /api/driver/hos`` and ``POST /api/driver/hos/override``.

One read for the driver, one write for the dispatcher. Everything about what an
Hours-of-Service advisory *means*
lives in :class:`~driver.services.hos_advisory_service.HOSAdvisoryService`: the
truck resolution, the freshness classification, the two ``unknown`` reason codes,
and the rule that a ``stale`` or ``unknown`` reading reports every remaining-hours
figure as ``unavailable`` and the compliance state as ``unknown``. This handler
resolves the caller's identity, decides whether the caller may ask for the
``driver_id`` it named, and stamps the correlation id.

**The scope is the caller's own advisory.** ``driver_id`` is accepted as an
optional query parameter for exactly one reason — so that a client that sends it
is answered with a clear rejection rather than being quietly served someone
else's hours. A ``driver``-role caller naming a different ``driver_id`` is 403
``FORBIDDEN`` (R17.32); omitting it, or naming its own, returns the caller's own
advisory. :func:`~auth.authorization.require_driver_identity` is the handler's
first statement, so there is no path on which a client-supplied identifier could
widen what the response contains.

**Every figure is labelled advisory** (R17.1). The
:class:`~driver.services.hos_advisory_service.HOSFigure` model carries
``advisory: True`` on each of the three figures, the advisory itself carries
``advisory: True`` and ``authoritative_record: "carrier_eld"``, and the response
envelope repeats the statement that the carrier's ELD is the authoritative record
of Hours of Service. Runsheet is not an ELD: this surface is read-only against
``truck_telemetry`` and writes nothing back to any telematics vendor or ELD.

The read carries no idempotency handling and no rate-limit key beyond the
IP-keyed default the other driver reads use.

**The override is the mirror image of the read: someone else's driver, never the
caller's own.** ``POST /api/driver/hos/override`` is a dispatcher surface, not a
driver one. :func:`~auth.authorization.require_role` matches ``dispatcher`` and
``admin`` **exactly**, so a tenant role lexicon such as ``dispatcher_lead`` does
not pass, and a caller holding only ``driver`` is refused with 403
``INSUFFICIENT_ROLE`` — a driver may not clear its own gate (R17.24). The request
body names the subject driver, a non-blank reason, and an expiry; it carries no
``actor_id`` and no ``override_id`` field, because the actor is
``tenant.user_id`` from the verified session and the identifier is minted by the
service (R17.23). The pattern is the one at ``POST /api/fuel/storm-mode/override``
(``fuel/api/fuel_ops_endpoints.py:8825-8900``).

No rejection on either surface echoes the roles the caller holds — the shared
role helper surfaces the requirement and nothing about the tenant's lexicon
(R15.14).

Every rejection on this surface is an ``AppException`` from
``errors/exceptions.py`` — this module raises **zero** raw ``HTTPException``
(R15.10).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §HTTP surface,
"New index: ``hos_gate_overrides``", and Property 34.

Validates: Requirements 17.1, 17.11, 17.23, 17.24, 17.32
- 17.1: every surfaced figure is labelled advisory, with the carrier's ELD named
  as the authoritative record
- 17.11: the response carries the duty status, ``recorded_at``, the reading age,
  the freshness state, and the provider name
- 17.23: the override is accepted from an exact ``dispatcher`` / ``admin``
  caller, requires a non-blank reason and an expiry, and takes its actor from
  the verified session
- 17.24: a ``driver``-only caller is 403 ``INSUFFICIENT_ROLE``
- 17.32: only the caller's own advisory is returned; naming another ``driver_id``
  is 403 ``FORBIDDEN``
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from auth.authorization import require_driver_identity, require_role
from config.settings import get_settings
from driver.services.hos_advisory_service import (
    ELD_AUTHORITATIVE_STATEMENT,
    MAX_OVERRIDE_REASON_LENGTH,
    OVERRIDE_ROLES,
    HOSAdvisoryService,
)
from errors.exceptions import forbidden, internal_error
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level collaborators, wired via configure_hos_endpoints().
_es_service: Optional[Any] = None
_driver_repository: Optional[Any] = None
_integration_instance_repository: Optional[Any] = None
_feature_flag_service: Optional[Any] = None
_hos_advisory_service: Optional[HOSAdvisoryService] = None

router = APIRouter(prefix="/api/driver", tags=["driver-hos"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_hos_endpoints(
    *,
    es_service: Any = None,
    driver_repository: Any = None,
    integration_instance_repository: Any = None,
    feature_flag_service: Any = None,
) -> None:
    """Wire the HOS_Advisory collaborators and build the service.

    Called once during startup from ``bootstrap/driver.py``. Like every other
    ``configure_*`` on this surface it assigns each module global
    unconditionally, so an argument a caller omits is reset to ``None`` — every
    call site must pass its full argument set.

    ``es_service`` is what reads ``truck_telemetry``; without it there is no
    service and the handler fails closed. ``driver_repository`` is the preferred
    reader of ``drivers_current``. ``integration_instance_repository`` supplies
    the tenant's freshness-window override and the provider name (R17.9,
    R17.11); absent it the window is the documented 300-second default.
    ``feature_flag_service`` is read by the *gate* alone, for the overlay key
    ``driver.hos_gating``; this read surface consults no flag, and an absent
    service means gating is disabled in every tenant (R17.19, R17.20).
    """
    global _es_service, _driver_repository, _integration_instance_repository
    global _feature_flag_service, _hos_advisory_service

    _es_service = es_service
    _driver_repository = driver_repository
    _integration_instance_repository = integration_instance_repository
    _feature_flag_service = feature_flag_service
    _hos_advisory_service = (
        HOSAdvisoryService(
            es_service=es_service,
            driver_repository=driver_repository,
            integration_instance_repository=integration_instance_repository,
            feature_flag_service=feature_flag_service,
        )
        if es_service is not None
        else None
    )


def configured_hos_advisory_service() -> Optional[HOSAdvisoryService]:
    """Return the service :func:`configure_hos_endpoints` built, or ``None``.

    The seam the Hours-of-Service gate is armed through: the driver transition
    gate stack takes an ``hos_advisory_service=`` collaborator and calls
    ``gate_verdict`` on it (``_gate_hos`` in
    ``driver/services/order_transition_service.py``), and ``bootstrap/driver.py``
    passes this function's result there. ``None`` — no ``es_service`` — leaves
    the gate a recorded skip rather than a failure.
    """
    return _hos_advisory_service


def _get_hos_advisory_service() -> HOSAdvisoryService:
    """Return the configured :class:`HOSAdvisoryService`, failing closed."""
    if _hos_advisory_service is None:
        logger.error(
            "HOS endpoints not configured. "
            "Call configure_hos_endpoints() during startup."
        )
        raise internal_error(
            message="The hours-of-service advisory is temporarily unavailable",
            details={"reason": "hos_endpoints_not_configured"},
        )
    return _hos_advisory_service


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/hos")
@limiter.limit(_driver_rate)
async def get_hos_advisory(
    request: Request,
    driver_id: Optional[str] = Query(
        None,
        description=(
            "Optional and self-only. A driver may name its own driver_id or "
            "omit it; naming a different one is rejected with 403 FORBIDDEN."
        ),
    ),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Return the calling driver's own Hours-of-Service advisory.

    Args:
        request: The inbound request, for the correlation id.
        driver_id: Optional, and only ever the caller's own (R17.32).
        tenant: The verified Auth_Context.

    Returns:
        ``{"data": <advisory>, "advisory": true, "authoritative_record":
        "carrier_eld", "authoritative_record_statement": ...,
        "request_id": ...}``.

    Raises:
        AppException: 403 ``FORBIDDEN`` when the caller names a different
            ``driver_id`` (R17.32), or ``INSUFFICIENT_ROLE`` /
            ``DRIVER_IDENTITY_MISSING`` from the composed driver gate; 500
            ``INTERNAL_ERROR`` when the surface is unconfigured.

    Validates: Requirements 17.1, 17.11, 17.32
    """
    own_driver_id = require_driver_identity(tenant)

    requested = (driver_id or "").strip()
    if requested and requested != own_driver_id:
        # R15.14 — the rejection names the rule, never the other identity. 403
        # rather than 404: whether that driver exists is not this caller's to
        # learn.
        raise forbidden(
            message="A driver may read only its own hours-of-service advisory",
            details={"reason": "driver_id_mismatch"},
        )

    advisory = await _get_hos_advisory_service().resolve(
        tenant.tenant_id, own_driver_id
    )

    return {
        "data": advisory.model_dump(mode="json"),
        "advisory": True,
        "authoritative_record": "carrier_eld",
        "authoritative_record_statement": ELD_AUTHORITATIVE_STATEMENT,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/driver/hos/override (R17.23, R17.24)
# ---------------------------------------------------------------------------


class HOSGateOverrideRequest(BaseModel):
    """Body for ``POST /api/driver/hos/override``.

    Three fields, and the two that matter most are the ones that are **absent**.
    There is no ``actor_id``: attribution comes from the verified session, so a
    client cannot record someone else as the person who cleared a gate. There is
    no ``override_id``: the service mints one, so a client can neither claim
    ownership of an existing override nor overwrite it (R17.23). ``tenant_id`` is
    likewise stamped from the verified scope.

    ``expires_at`` is required and must be in the future — an override without a
    lapse is not an override, and the reason is required non-blank so every
    cleared gate is explainable at review. ``extra="forbid"`` means a body that
    tries to smuggle ``actor_id`` is a 422 rather than a silently ignored field.
    """

    model_config = ConfigDict(extra="forbid")

    driver_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The driver whose hours-of-service gate is being cleared.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=MAX_OVERRIDE_REASON_LENGTH,
        description=(
            "Why the gate is being cleared. Required and non-blank so every "
            "override is explainable at incident review."
        ),
    )
    expires_at: datetime = Field(
        ...,
        description=(
            "When the override lapses, in UTC. Required and must be in the "
            "future: an override that never expires is a disabled gate."
        ),
    )


@router.post("/hos/override", status_code=status.HTTP_201_CREATED)
@limiter.limit(_driver_rate)
async def submit_hos_gate_override(
    body: HOSGateOverrideRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Record a dispatcher or admin clearance of one driver's HOS gate.

    Flow, in the order the guarantees have to hold:

        1. Match the ``dispatcher`` / ``admin`` role set exactly. A caller
           holding only ``driver`` is 403 ``INSUFFICIENT_ROLE`` (R17.24), and the
           rejection names the requirement rather than the roles the caller holds
           (R15.14).
        2. Hand the service the subject driver, the reason, and the expiry, with
           ``tenant_id`` from the verified scope and ``actor_id`` from
           ``tenant.user_id`` — never from the body (R17.23).
        3. The service mints ``hgo_<uuid4hex>``, refuses a blank reason or an
           expiry at or before now, and persists to the ``dynamic: strict``
           ``hos_gate_overrides`` index. A write that does not land is a 503, not
           a reported success.

    The gate picks the override up on its next evaluation — ``gate_verdict``
    reads the index for the ``(tenant_id, driver_id)`` pair with ``expires_at``
    in the future (R17.25) — so nothing is nudged here.

    Args:
        body: The subject driver, the reason, and the expiry.
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context, supplying both the tenant scope and
            the actor identity.

    Returns:
        ``{"data": <override>, "request_id": ...}`` with HTTP 201. The override
        carries its server-minted ``override_id`` so a dispatcher can correlate
        the clearance with the order event that records it.

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` for a caller without the
            ``dispatcher`` or ``admin`` role; 400 ``INVALID_REQUEST`` for a blank
            reason or a non-future expiry; 503 when the override cannot be
            persisted; 500 ``INTERNAL_ERROR`` when the surface is unconfigured.

    Validates: Requirements 17.23, 17.24
    """
    # R17.24 — the role gate is first, so an unauthorized caller learns nothing
    # about whether the named driver exists.
    require_role(tenant, *OVERRIDE_ROLES)

    override = await _get_hos_advisory_service().record_override(
        tenant.tenant_id,
        body.driver_id,
        # R17.23 — the actor is the verified session's user, never the body.
        actor_id=tenant.user_id,
        reason=body.reason,
        expires_at=body.expires_at,
    )

    return {
        "data": override.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


__all__ = [
    "router",
    "configure_hos_endpoints",
    "configured_hos_advisory_service",
    "HOSGateOverrideRequest",
]
