"""Payment REST endpoints for the Commerce Backbone.

Provides endpoints for Payment records under /api/commerce/payments,
including manual-payment creation (Req 6.3) and reversal (Req 6.6).

All endpoints require ``commerce.backbone_enabled`` and
``commerce.invoicing_enabled`` feature flags to be active for the
requesting tenant — returns HTTP 404 when either flag is off
(Req 8.1, 8.2).

Validates: Requirements 6.3, 6.6, 8.1, 8.2, C1, C3
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config.settings import get_settings
from commerce.api._authz import require_commerce_staff
from commerce.services.payment_service import PaymentService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.ref_resolver import get_ref_resolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_payment_api()
# ---------------------------------------------------------------------------

_payment_service: Optional[PaymentService] = None
#: Optional resolver override (tests inject a fake); falls back to the
#: process-wide resolver registered at bootstrap.
_ref_resolver = None

router = APIRouter(prefix="/api/commerce/payments", tags=["commerce-payments"])


def configure_payment_api(
    *, payment_service: PaymentService, ref_resolver=None
) -> None:
    """Wire service dependencies into the payment API module.

    Called once during application startup so that the router handlers
    can access the shared PaymentService without circular imports. An
    optional ``ref_resolver`` overrides the process-wide resolver used to
    expand ``invoice`` / ``account`` reference links (Req 12.3); when omitted
    the handlers fall back to :func:`services.ref_resolver.get_ref_resolver`.
    """
    global _payment_service, _ref_resolver
    _payment_service = payment_service
    _ref_resolver = ref_resolver


def _get_payment_service() -> PaymentService:
    """Return the configured PaymentService or raise."""
    if _payment_service is None:
        raise RuntimeError(
            "Payment API not configured. Call configure_payment_api() during startup."
        )
    return _payment_service


def _get_ref_resolver():
    """Return the resolver used to expand reference links (Req 12.3)."""
    return _ref_resolver if _ref_resolver is not None else get_ref_resolver()


# ---------------------------------------------------------------------------
# Feature-flag gate dependency
# ---------------------------------------------------------------------------


async def require_payments_enabled(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """FastAPI dependency that checks commerce feature flags for the tenant.

    Returns HTTP 404 when ``commerce.backbone_enabled`` or
    ``commerce.invoicing_enabled`` is off, making the endpoints invisible
    to tenants that have not been migrated.

    Validates: Requirements 8.1, 8.2
    """
    settings = get_settings()

    if not settings.commerce_backbone_enabled:
        logger.debug(
            "Commerce payment request blocked: commerce_backbone_enabled=False "
            "for tenant_id=%s",
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "COMMERCE_DISABLED",
                "message": "Commerce backbone is not enabled for this tenant",
            },
        )

    if not settings.commerce_invoicing_enabled:
        logger.debug(
            "Commerce payment request blocked: commerce_invoicing_enabled=False "
            "for tenant_id=%s",
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "INVOICING_DISABLED",
                "message": "Commerce invoicing module is not enabled for this tenant",
            },
        )

    # Payments are a Tier 4 / staff surface: the ERP bills and collects. Applied
    # after the flag check so a tenant without the module still sees 404 rather
    # than 403.
    require_commerce_staff(tenant)

    return tenant


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateManualPaymentRequest(BaseModel):
    """Request body for POST /api/commerce/payments (Req 6.3).

    Creates a manual payment applied to an invoice.
    """

    invoice_id: str = Field(..., description="Invoice this payment applies to")
    amount_cents: int = Field(
        ..., gt=0, description="Payment amount in integer cents (must be > 0)"
    )
    method: str = Field(
        ...,
        description="Payment method: check|ach|wire|other",
    )
    reference: Optional[str] = Field(
        default=None,
        description="Free-text reference (check number, wire memo, etc.)",
    )
    received_at: Optional[datetime] = Field(
        default=None,
        description="When the payment was received (ISO 8601 UTC)",
    )


class ReversePaymentRequest(BaseModel):
    """Request body for POST /api/commerce/payments/{payment_id}/reverse.

    Currently empty — the endpoint only requires the payment_id path param.
    Kept as a model for future extensibility (e.g., reason, authorized_by).
    """

    reason: Optional[str] = Field(
        default=None, description="Reason for the reversal"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# POST /api/commerce/payments
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_manual_payment(
    request: Request,
    body: CreateManualPaymentRequest,
    tenant: TenantContext = Depends(require_payments_enabled),
) -> dict:
    """Create a manual payment and apply it to an invoice.

    Accepts ``{invoice_id, amount_cents, method, reference, received_at}``
    and creates a Payment with ``source=manual``. The payment is
    immediately applied to the referenced invoice.

    Returns 201 with the created payment document.

    Validates: Requirement 6.3
    """
    service = _get_payment_service()

    # Validate method is one of the allowed manual methods
    allowed_methods = {"check", "ach", "wire", "other"}
    if body.method not in allowed_methods:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "INVALID_PAYMENT_METHOD",
                "message": (
                    f"Invalid payment method: '{body.method}'. "
                    f"Allowed: {sorted(allowed_methods)}"
                ),
            },
        )

    # Look up the invoice to get the account_id
    if service._invoice_service:
        invoice = await service._invoice_service.get(
            tenant_id=tenant.tenant_id, invoice_id=body.invoice_id
        )
        account_id = invoice.get("account_id", "")
    else:
        # Fallback: if no invoice service is wired, we cannot determine
        # the account_id. This should not happen in production.
        account_id = ""

    payment = await service.ingest(
        tenant_id=tenant.tenant_id,
        invoice_id=body.invoice_id,
        account_id=account_id,
        amount_cents=body.amount_cents,
        source="manual",
        method=body.method,
        external_id=None,
        reference=body.reference,
        received_at=body.received_at,
        actor=tenant.user_id,
    )

    return {
        "data": payment,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/payments
# ---------------------------------------------------------------------------


@router.get("")
async def list_payments(
    request: Request,
    tenant: TenantContext = Depends(require_payments_enabled),
    invoice_id: Optional[str] = Query(
        default=None, description="Filter by invoice_id"
    ),
    account_id: Optional[str] = Query(
        default=None, description="Filter by account_id"
    ),
    cursor: Optional[str] = Query(
        default=None,
        description="Cursor for pagination (payment_id of last item)",
    ),
    limit: int = Query(
        default=50, ge=1, le=200, description="Page size (default 50, max 200)"
    ),
) -> dict:
    """List Payments with cursor/limit pagination.

    Tenant-scoped via ``inject_tenant_filter``. Default limit is 50,
    max 200. Supports filtering by invoice_id and account_id.

    Validates: Constraint C3
    """
    service = _get_payment_service()

    result = await service.list(
        tenant_id=tenant.tenant_id,
        invoice_id=invoice_id,
        account_id=account_id,
        cursor=cursor,
        limit=limit,
    )

    return {
        "data": result["items"],
        "next_cursor": result["next_cursor"],
        "limit": result["limit"],
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/payments/{payment_id}
# ---------------------------------------------------------------------------

#: Reference types ``GET /api/commerce/payments/{id}?expand=...`` can resolve.
_VALID_PAYMENT_EXPAND = ("invoice", "account")


def _parse_expand(expand: Optional[str]) -> set:
    """Parse a comma-separated ``expand`` query into a set of known tokens.

    Unknown tokens are ignored so the param stays additive/forward-compatible.
    """
    if not expand:
        return set()
    requested = {tok.strip() for tok in expand.split(",") if tok.strip()}
    return requested & set(_VALID_PAYMENT_EXPAND)


async def _build_payment_links(
    tenant_id: str, payment: Dict[str, Any], expand: set
) -> Dict[str, Any]:
    """Resolve the requested payment references into a ``links`` object.

    A payment's ``invoice_id`` / ``account_id`` become resolvable references
    (Req 12.3). All resolution is tenant-scoped via the loaders; references
    never cross tenants (Req 5.3) and are returned resolved or explicitly
    ``unresolved`` — never silently omitted (Req 5.4 / Property 4).
    """
    refs: Dict[str, Any] = {}
    if "invoice" in expand:
        refs["invoice"] = ("invoice", payment.get("invoice_id"))
    if "account" in expand:
        refs["account"] = ("account", payment.get("account_id"))

    resolver = _get_ref_resolver()
    resolved = await resolver.resolve_many(tenant_id, refs)
    return {key: ref.to_dict() for key, ref in resolved.items()}


@router.get("/{payment_id}")
async def get_payment(
    payment_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_payments_enabled),
    expand: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated references to resolve into a `links` object: "
            "invoice, account. Omitted → no `links` key (additive, Req 6.3)."
        ),
    ),
) -> dict:
    """Retrieve a single Payment by ID.

    Returns the full payment document including source, method,
    status, and external references. When ``expand`` is supplied, a
    ``links`` object resolves the payment's ``invoice_id`` / ``account_id``
    into navigable references (each resolved or explicitly ``unresolved``)
    so the billing chain can be traversed end to end (Req 12.3).

    Validates: Constraint C3, Requirement 12.3
    """
    service = _get_payment_service()

    payment = await service.get(tenant_id=tenant.tenant_id, payment_id=payment_id)

    response: Dict[str, Any] = {
        "data": payment,
        "request_id": _get_request_id(request),
    }

    requested = _parse_expand(expand)
    if requested:
        response["links"] = await _build_payment_links(
            tenant.tenant_id, payment, requested
        )

    return response


# ---------------------------------------------------------------------------
# POST /api/commerce/payments/{payment_id}/reverse
# ---------------------------------------------------------------------------


@router.post("/{payment_id}/reverse")
async def reverse_payment(
    payment_id: str,
    request: Request,
    body: ReversePaymentRequest = ReversePaymentRequest(),
    tenant: TenantContext = Depends(require_payments_enabled),
) -> dict:
    """Reverse a payment.

    Transitions the payment to ``reversed``, subtracts its amount from
    the Invoice's ``amount_paid_cents``, re-evaluates the Invoice's
    state (paid → partial or partial → open), and emits
    ``payment_reversed`` to ``invoice_events``.

    Validates: Requirement 6.6
    """
    service = _get_payment_service()

    payment = await service.reverse(
        tenant_id=tenant.tenant_id,
        payment_id=payment_id,
        actor=tenant.user_id,
    )

    return {
        "data": payment,
        "message": "Payment reversed successfully",
        "request_id": _get_request_id(request),
    }
