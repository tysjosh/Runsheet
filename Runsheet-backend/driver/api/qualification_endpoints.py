"""
Driver_Qualification read surface — ``GET /api/driver/qualifications``.

One read, no new service. Every qualification rule already lives in
:class:`~compliance.services.driver_qualification_service.DriverQualificationService`:
the tenant-scoped DQF fetch (``get``), and the eligibility computation
(``is_dispatch_eligible``) that the driver transition gate stack already
consumes as ``Dispatch_Eligibility``. This module resolves the caller's
identity, re-validates the document it got back, and projects the field set
R12.2 names. It computes **no** eligibility of its own — a second
implementation of that rule would be free to disagree with the gate that
actually blocks a transition.

**The scope is not a parameter.** The ``(tenant_id, driver_id)`` pair comes from
:func:`~auth.authorization.require_driver_identity`, which is the handler's
first statement, so the record returned is always the one whose ``driver_id``
equals ``TenantContext.driver_id`` (R12.1). ``driver_id`` is accepted as an
optional query parameter for exactly one reason: so a request naming somebody
else can be **rejected** with 403 ``FORBIDDEN`` rather than quietly answered
with the caller's own record (R12.6). The rejection names the rule and nothing
else — never the caller's held roles, never the other driver's identity, and
never whether that driver exists (R15.14).

Tenant isolation is defence in depth. ``DriverQualificationService.get``
wraps its query with ``inject_tenant_filter``; this module then re-validates
``tenant_id`` and ``driver_id`` on the returned document and treats a mismatch
as an absent record, so a filter regression downstream cannot turn into a
cross-tenant read here (R15.11).

The compliance ``DriverStatus`` (``active`` / ``suspended`` / ``expired``,
``compliance/models/driver.py:34``) is surfaced as ``qualification_status`` —
deliberately not ``status`` — because the operational duty-status vocabulary
(``fuel/order_models.py:63``) uses the same field name for a different set of
values, and the profile screen has to present the two separately (R12.7).

This is a read, so there is no idempotency handling: a replayed GET is another
GET. Rate limiting is IP-keyed, matching the other driver reads.

Every rejection on this surface is an ``AppException`` from
``errors/exceptions.py`` — this module raises **zero** raw ``HTTPException``
(R15.10).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Endpoint inventory,
``GET /api/driver/qualifications`` (Phase 2).

Validates: Requirements 12.1, 12.2, 12.6
- 12.1: return only the DQF record whose ``driver_id`` equals
  ``TenantContext.driver_id``
- 12.2: CDL class, CDL expiry, medical card expiry, HAZMAT endorsement expiry,
  tanker endorsement expiry, most recent drug-test date, and the computed
  ``is_dispatch_eligible``
- 12.6: a request naming another ``driver_id`` is 403 ``FORBIDDEN``
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request

from auth.authorization import require_driver_identity
from config.settings import get_settings
from errors.exceptions import forbidden, internal_error, resource_not_found
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

#: The DQF fields R12.2 names, paired with the key they are published under.
#: Every one is a date on ``compliance/models/driver.py`` and is emitted as an
#: ISO-8601 ``YYYY-MM-DD`` string, or ``None`` where the driver holds no such
#: qualification (the two endorsements and the drug test are all optional).
_DATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("cdl_expiry_date", "cdl_expiry_date"),
    ("medical_card_expiry_date", "medical_card_expiry_date"),
    ("hazmat_endorsement_expiry_date", "hazmat_endorsement_expiry_date"),
    ("tanker_endorsement_expiry_date", "tanker_endorsement_expiry_date"),
    ("last_drug_test_date", "last_drug_test_date"),
)

# Module-level collaborator, wired via configure_qualification_endpoints().
_driver_qualification_service: Optional[Any] = None

router = APIRouter(prefix="/api/driver", tags=["driver-qualifications"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_qualification_endpoints(
    *,
    driver_qualification_service: Any = None,
) -> None:
    """Wire the existing ``Driver_Qualification_Service`` behind this read.

    Called once during startup from ``bootstrap/driver.py``. Like every other
    ``configure_*`` on this surface it assigns the module global
    unconditionally, so an argument a caller omits is reset to ``None`` — every
    call site must pass its full argument set.

    The service is the instance ``bootstrap/compliance.py`` registered, not a
    second one built over the same index: the eligibility value this read
    reports has to be the one the transition gate stack enforces.
    """
    global _driver_qualification_service

    _driver_qualification_service = driver_qualification_service


def _get_qualification_service() -> Any:
    """Return the configured qualification service, failing closed."""
    if _driver_qualification_service is None:
        logger.error(
            "Driver qualification endpoints not configured. "
            "Call configure_qualification_endpoints() during startup."
        )
        raise internal_error(
            message="Qualification status is temporarily unavailable",
            details={"reason": "qualification_endpoints_not_configured"},
        )
    return _driver_qualification_service


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_subject(tenant: TenantContext, requested_driver_id: Optional[str]) -> str:
    """Return the caller's own ``driver_id``, rejecting a request naming another.

    The driver gate runs first, so a caller without the exact ``driver`` role or
    without a ``driver_id`` claim is refused before any identifier comparison
    happens. A caller may name its own id or omit it; naming a different one is
    403 ``FORBIDDEN`` — never 404, which would confirm whether that driver
    exists, and never with the requested identifier echoed back (R15.14).

    Validates: Requirements 12.1, 12.6
    """
    own_driver_id = require_driver_identity(tenant)
    requested = (requested_driver_id or "").strip()
    if requested and requested != own_driver_id:
        raise forbidden(
            message="A driver may read only its own qualification status",
            details={"reason": "driver_id_mismatch"},
        )
    return own_driver_id


def _iso_date(value: Any) -> Optional[str]:
    """Normalize a stored date to an ISO-8601 ``YYYY-MM-DD`` string.

    The DQF record reaches this module either from Elasticsearch (ISO strings)
    or, under the Postgres read-cutover, as ``date``/``datetime`` objects. Both
    are published in the one shape so the mobile client never has to branch on
    which store answered. An unparseable value becomes ``None`` rather than
    being passed through as-is, so a malformed stored date cannot masquerade as
    a valid expiry.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except (ValueError, TypeError):
            logger.warning(
                "Dropping unparseable qualification date from the driver read"
            )
            return None
    return None


def _assert_own_record(
    record: Any, tenant_id: str, driver_id: str
) -> Dict[str, Any]:
    """Re-validate the fetched document's ``tenant_id`` and ``driver_id``.

    ``DriverQualificationService.get`` already scopes its query with
    ``inject_tenant_filter``; this is the per-document check behind it. A
    document belonging to another tenant, or to another driver, is
    indistinguishable to the caller from one that does not exist (R15.11).
    """
    if not isinstance(record, dict):
        raise resource_not_found(
            "No qualification record exists for this driver",
            details={"reason": "qualification_record_absent"},
        )
    if record.get("tenant_id") != tenant_id or record.get("driver_id") != driver_id:
        logger.warning(
            "Suppressing a qualification record that failed per-document "
            "tenant/driver re-validation"
        )
        raise resource_not_found(
            "No qualification record exists for this driver",
            details={"reason": "qualification_record_absent"},
        )
    return record


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/qualifications")
@limiter.limit(_driver_rate)
async def get_qualifications(
    request: Request,
    driver_id: Optional[str] = Query(
        None,
        description=(
            "Accepted only when it equals the caller's own driver_id; naming "
            "another driver is rejected with 403 FORBIDDEN."
        ),
    ),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Return the calling driver's own qualification summary.

    The field set is R12.2's: the CDL class and expiry, the medical card
    expiry, the HAZMAT and tanker endorsement expiries, the most recent
    drug-test date, and ``is_dispatch_eligible``. The eligibility value and the
    reasons behind it come from
    ``DriverQualificationService.is_dispatch_eligible`` — the same
    ``Dispatch_Eligibility`` the transition gate stack consults — so the screen
    cannot tell a driver they are eligible while the gate is blocking them. The
    reasons are what the persistent ineligibility banner lists (R12.5).

    ``qualification_status`` is the compliance ``DriverStatus``, kept under a
    distinct key from the operational duty status (R12.7).

    Args:
        request: The inbound request, for the correlation id.
        driver_id: Optional, and only ever the caller's own (R12.6).
        tenant: The verified Auth_Context.

    Returns:
        ``{"data": {...}, "request_id": ...}`` with HTTP 200.

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` / ``DRIVER_IDENTITY_MISSING``
            from the driver gate; 403 ``FORBIDDEN`` when the request names
            another ``driver_id`` (R12.6); 404 ``RESOURCE_NOT_FOUND`` when this
            tenant holds no DQF record for the caller.

    Validates: Requirements 12.1, 12.2, 12.6
    """
    subject_driver_id = _resolve_subject(tenant, driver_id)
    service = _get_qualification_service()

    record = _assert_own_record(
        await service.get(tenant.tenant_id, subject_driver_id),
        tenant.tenant_id,
        subject_driver_id,
    )
    eligibility = await service.is_dispatch_eligible(
        tenant.tenant_id, subject_driver_id
    )

    data: Dict[str, Any] = {
        "driver_id": subject_driver_id,
        "cdl_class": record.get("cdl_class"),
        "qualification_status": record.get("status"),
        "is_dispatch_eligible": bool(getattr(eligibility, "eligible", False)),
        "ineligibility_reasons": list(getattr(eligibility, "reasons", []) or []),
    }
    for source_field, published_key in _DATE_FIELDS:
        data[published_key] = _iso_date(record.get(source_field))

    return {"data": data, "request_id": _get_request_id(request)}


__all__ = ["router", "configure_qualification_endpoints"]
