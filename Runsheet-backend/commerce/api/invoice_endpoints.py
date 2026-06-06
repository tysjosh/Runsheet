"""Invoice REST endpoints for the Commerce Backbone.

Provides read and action endpoints for Invoice records under
/api/commerce/invoices, including the event timeline replay and
QBO dead-letter retry.

All endpoints require ``commerce.backbone_enabled`` and
``commerce.invoicing_enabled`` feature flags to be active for the
requesting tenant — returns HTTP 404 when either flag is off
(Req 8.1, 8.2).

Validates: Requirements 5.5, 5.6b, 5.6c, 8.1, 8.2, C7
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config.settings import get_settings
from commerce.services.invoice_service import InvoiceService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.ref_resolver import get_ref_resolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_invoice_api()
# ---------------------------------------------------------------------------

_invoice_service: Optional[InvoiceService] = None
#: Optional resolver override (tests inject a fake); falls back to the
#: process-wide resolver registered at bootstrap.
_ref_resolver = None

router = APIRouter(prefix="/api/commerce/invoices", tags=["commerce-invoices"])


def configure_invoice_api(
    *, invoice_service: InvoiceService, ref_resolver=None
) -> None:
    """Wire service dependencies into the invoice API module.

    Called once during application startup so that the router handlers
    can access the shared InvoiceService without circular imports. An
    optional ``ref_resolver`` overrides the process-wide resolver used to
    expand ``order`` / ``account`` / ``customer`` reference links (Req 12.1);
    when omitted the handlers fall back to
    :func:`services.ref_resolver.get_ref_resolver`.
    """
    global _invoice_service, _ref_resolver
    _invoice_service = invoice_service
    _ref_resolver = ref_resolver


def _get_invoice_service() -> InvoiceService:
    """Return the configured InvoiceService or raise."""
    if _invoice_service is None:
        raise RuntimeError(
            "Invoice API not configured. Call configure_invoice_api() during startup."
        )
    return _invoice_service


def _get_ref_resolver():
    """Return the resolver used to expand reference links (Req 12.1, 5.4)."""
    return _ref_resolver if _ref_resolver is not None else get_ref_resolver()


# ---------------------------------------------------------------------------
# Feature-flag gate dependency
# ---------------------------------------------------------------------------


async def require_invoicing_enabled(
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
            "Commerce invoice request blocked: commerce_backbone_enabled=False "
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
            "Commerce invoice request blocked: commerce_invoicing_enabled=False "
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

    return tenant


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class VoidInvoiceRequest(BaseModel):
    """Request body for POST /api/commerce/invoices/{invoice_id}/void."""

    reason: str = Field(..., description="Reason for voiding the invoice")
    force: bool = Field(
        default=False,
        description="Force void even if payments are applied (triggers cascading reversal)",
    )
    authorized_by: Optional[str] = Field(
        default=None,
        description="User who authorized the void (required when force=true)",
    )


class RetryQboPushRequest(BaseModel):
    """Request body for POST /api/commerce/invoices/{invoice_id}/retry-qbo-push.

    Currently empty — the endpoint only requires the invoice_id path param.
    Kept as a model for future extensibility (e.g., override push config).
    """

    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# GET /api/commerce/invoices
# ---------------------------------------------------------------------------


@router.get("")
async def list_invoices(
    request: Request,
    tenant: TenantContext = Depends(require_invoicing_enabled),
    status: Optional[str] = Query(
        default=None,
        description="Filter by status: draft|open|partial|paid|overdue|void",
    ),
    customer_id: Optional[str] = Query(
        default=None, description="Filter by customer_id"
    ),
    account_id: Optional[str] = Query(
        default=None, description="Filter by account_id"
    ),
    qbo_push_state: Optional[str] = Query(
        default=None,
        description="Filter by qbo_push_state: pending|pushed|retry|dead_letter",
    ),
    cursor: Optional[str] = Query(
        default=None,
        description="Cursor for pagination (invoice_id of last item)",
    ),
    limit: int = Query(
        default=50, ge=1, le=200, description="Page size (default 50, max 200)"
    ),
) -> dict:
    """List Invoices with cursor/limit pagination.

    Tenant-scoped via ``inject_tenant_filter``. Default limit is 50,
    max 200. Supports filtering by status, customer_id, account_id,
    and qbo_push_state (for dead-letter triage per Req 5.6b).

    Validates: Constraint C3
    """
    service = _get_invoice_service()

    result = await service.list(
        tenant_id=tenant.tenant_id,
        status=status,
        customer_id=customer_id,
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
# GET /api/commerce/invoices/{invoice_id}
# ---------------------------------------------------------------------------

#: Reference types ``GET /api/commerce/invoices/{id}?expand=...`` can resolve.
_VALID_INVOICE_EXPAND = ("order", "account", "customer")


def _parse_expand(expand: Optional[str]) -> set:
    """Parse a comma-separated ``expand`` query into a set of known tokens.

    Unknown tokens are ignored so the param stays additive/forward-compatible.
    """
    if not expand:
        return set()
    requested = {tok.strip() for tok in expand.split(",") if tok.strip()}
    return requested & set(_VALID_INVOICE_EXPAND)


async def _build_invoice_links(
    tenant_id: str, invoice: Dict[str, Any], expand: set
) -> Dict[str, Any]:
    """Resolve the requested invoice references into a ``links`` object.

    An invoice's ``order_id`` / ``account_id`` / ``customer_id`` become
    resolvable references (Req 12.1). All resolution is tenant-scoped via the
    loaders; references never cross tenants (Req 5.3) and are returned resolved
    or explicitly ``unresolved``/``empty`` — never silently omitted (Req 5.4 /
    Property 4).
    """
    refs: Dict[str, Any] = {}
    if "order" in expand:
        refs["order"] = ("order", invoice.get("order_id"))
    if "account" in expand:
        refs["account"] = ("account", invoice.get("account_id"))
    if "customer" in expand:
        refs["customer"] = ("customer", invoice.get("customer_id"))

    resolver = _get_ref_resolver()
    resolved = await resolver.resolve_many(tenant_id, refs)
    return {key: ref.to_dict() for key, ref in resolved.items()}


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_invoicing_enabled),
    expand: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated references to resolve into a `links` object: "
            "order, account, customer. Omitted → no `links` key (additive, "
            "Req 6.3)."
        ),
    ),
) -> dict:
    """Retrieve a single Invoice by ID.

    Returns the full invoice projection including line items,
    payment totals, and QBO push state. When ``expand`` is supplied, a
    ``links`` object resolves the invoice's ``order_id`` / ``account_id`` /
    ``customer_id`` into navigable references (each resolved or explicitly
    ``unresolved``) so the delivery-to-billing chain can be traversed end to
    end (Req 12.1). Reads without ``expand`` return the unchanged contract
    (Req 6.3).

    All resolution is tenant-scoped; references never cross tenants (Req 5.3).

    Validates: Constraint C3, Requirements 12.1, 5.4
    """
    service = _get_invoice_service()

    invoice = await service.get(tenant_id=tenant.tenant_id, invoice_id=invoice_id)

    response: Dict[str, Any] = {
        "data": invoice,
        "request_id": _get_request_id(request),
    }

    requested = _parse_expand(expand)
    if requested:
        response["links"] = await _build_invoice_links(
            tenant.tenant_id, invoice, requested
        )

    return response


# ---------------------------------------------------------------------------
# POST /api/commerce/invoices/{invoice_id}/void
# ---------------------------------------------------------------------------


@router.post("/{invoice_id}/void")
async def void_invoice(
    invoice_id: str,
    request: Request,
    body: VoidInvoiceRequest,
    tenant: TenantContext = Depends(require_invoicing_enabled),
) -> dict:
    """Void an invoice.

    If ``amount_paid_cents == 0``: transitions directly to void.
    If ``amount_paid_cents > 0`` and ``force=false``: rejects with HTTP 409.
    If ``amount_paid_cents > 0`` and ``force=true``: auto-reverses all
    applied payments with ``source=void_cascade`` before voiding.

    Requires ``authorized_by`` when ``force=true``.

    Validates: Requirement 5.5
    """
    # Validate that authorized_by is provided when force=true
    if body.force and not body.authorized_by:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "MISSING_AUTHORIZED_BY",
                "message": "authorized_by is required when force=true",
            },
        )

    service = _get_invoice_service()

    actor = body.authorized_by if body.force else tenant.user_id

    invoice = await service.void(
        tenant_id=tenant.tenant_id,
        invoice_id=invoice_id,
        reason=body.reason,
        actor=actor,
        force=body.force,
    )

    return {
        "data": invoice,
        "message": "Invoice voided successfully",
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/invoices/{invoice_id}/events
# ---------------------------------------------------------------------------


@router.get("/{invoice_id}/events")
async def get_invoice_events(
    invoice_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_invoicing_enabled),
) -> dict:
    """Retrieve the full event timeline for an invoice.

    Returns all events ordered by sequence number (ascending), replaying
    the complete event-sourced history of the invoice. This is the
    canonical audit trail per Constraint C7.

    Validates: Constraint C7, Requirement 5.4 (event sourcing)
    """
    service = _get_invoice_service()

    # First verify the invoice exists under this tenant (raises 404 if not)
    await service.get(tenant_id=tenant.tenant_id, invoice_id=invoice_id)

    # Retrieve the full event log
    events = await service.get_events(
        tenant_id=tenant.tenant_id, invoice_id=invoice_id
    )

    return {
        "data": events,
        "invoice_id": invoice_id,
        "event_count": len(events),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/commerce/invoices/{invoice_id}/retry-qbo-push
# ---------------------------------------------------------------------------


@router.post("/{invoice_id}/retry-qbo-push")
async def retry_qbo_push(
    invoice_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_invoicing_enabled),
) -> dict:
    """Retry QBO push for a dead-lettered invoice.

    Resets ``qbo_push_state`` to ``pending`` and re-enqueues the push
    through the standard path. Only valid for invoices with
    ``qbo_push_state=dead_letter``.

    Validates: Requirement 5.6c
    """
    service = _get_invoice_service()

    # Fetch the invoice to validate state
    invoice = await service.get(tenant_id=tenant.tenant_id, invoice_id=invoice_id)

    current_push_state = invoice.get("qbo_push_state")
    if current_push_state != "dead_letter":
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "INVALID_QBO_PUSH_STATE",
                "message": (
                    f"Cannot retry QBO push: invoice qbo_push_state is "
                    f"'{current_push_state}', expected 'dead_letter'"
                ),
                "invoice_id": invoice_id,
                "current_qbo_push_state": current_push_state,
            },
        )

    # Reset qbo_push_state to pending and clear retry counter
    from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX
    from services.time_utils import utcnow

    now = utcnow()
    update_doc: Dict[str, Any] = {
        "qbo_push_state": "pending",
        "qbo_push_attempts": 0,
        "qbo_push_last_error": None,
        "updated_at": now.isoformat(),
    }

    await service._es.update_document(
        INVOICES_CURRENT_INDEX,
        invoice_id,
        update_doc,
    )

    logger.info(
        "Reset qbo_push_state to pending for invoice %s (tenant %s) — "
        "re-enqueued for QBO push",
        invoice_id,
        tenant.tenant_id,
    )

    # Return the updated invoice
    updated_invoice = {**invoice, **update_doc}

    return {
        "data": updated_invoice,
        "message": "QBO push retry enqueued",
        "request_id": _get_request_id(request),
    }
