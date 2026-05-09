"""Customer REST endpoints for the Commerce Backbone.

Provides CRUD endpoints for Customer records under /api/commerce/customers.
All endpoints require both ``commerce.backbone_enabled`` and
``commerce.customers_enabled`` feature flags to be active for the
requesting tenant — returns HTTP 404 when either flag is off (Req 8.1, 8.2).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6, 8.1, 8.2
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config.settings import get_settings
from commerce.services.customer_service import CustomerService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_customer_api()
# ---------------------------------------------------------------------------

_customer_service: Optional[CustomerService] = None

router = APIRouter(prefix="/api/commerce/customers", tags=["commerce-customers"])


def configure_customer_api(*, customer_service: CustomerService) -> None:
    """Wire service dependencies into the customer API module.

    Called once during application startup so that the router handlers
    can access the shared CustomerService without circular imports.
    """
    global _customer_service
    _customer_service = customer_service


def _get_customer_service() -> CustomerService:
    """Return the configured CustomerService or raise."""
    if _customer_service is None:
        raise RuntimeError(
            "Customer API not configured. Call configure_customer_api() during startup."
        )
    return _customer_service


# ---------------------------------------------------------------------------
# Feature-flag gate dependency
# ---------------------------------------------------------------------------


async def require_customers_enabled(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """FastAPI dependency that checks commerce feature flags for the tenant.

    Returns HTTP 404 when either ``commerce.backbone_enabled`` or
    ``commerce.customers_enabled`` is off, making the endpoints invisible
    to tenants that have not been migrated.

    Validates: Requirements 8.1, 8.2
    """
    settings = get_settings()

    if not settings.commerce_backbone_enabled:
        logger.debug(
            "Commerce customer request blocked: commerce_backbone_enabled=False "
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

    if not settings.commerce_customers_enabled:
        logger.debug(
            "Commerce customer request blocked: commerce_customers_enabled=False "
            "for tenant_id=%s",
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "CUSTOMERS_DISABLED",
                "message": "Commerce customers module is not enabled for this tenant",
            },
        )

    return tenant


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateCustomerRequest(BaseModel):
    """Request body for POST /api/commerce/customers."""

    display_name: str = Field(..., description="Customer display name")
    legal_name: Optional[str] = Field(default=None, description="Legal business name")
    primary_email: Optional[str] = Field(default=None, description="Primary contact email")
    tax_id: Optional[str] = Field(default=None, description="Tax identification number")
    status: str = Field(default="active", description="Initial status: active or archived")


class UpdateCustomerRequest(BaseModel):
    """Request body for PATCH /api/commerce/customers/{customer_id}."""

    display_name: Optional[str] = Field(default=None, description="Customer display name")
    legal_name: Optional[str] = Field(default=None, description="Legal business name")
    primary_email: Optional[str] = Field(default=None, description="Primary contact email")
    tax_id: Optional[str] = Field(default=None, description="Tax identification number")
    status: Optional[str] = Field(default=None, description="Status: active or archived")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# POST /api/commerce/customers
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_customer(
    request: Request,
    body: CreateCustomerRequest,
    tenant: TenantContext = Depends(require_customers_enabled),
) -> dict:
    """Create a new Customer record.

    Returns 201 with the created customer document including the
    server-assigned ``customer_id`` of shape ``cust_<uuid4>``.

    Validates: Requirements 1.1, 1.2
    """
    service = _get_customer_service()

    customer = await service.create(
        tenant.tenant_id,
        display_name=body.display_name,
        legal_name=body.legal_name,
        primary_email=body.primary_email,
        tax_id=body.tax_id,
        status=body.status,
    )

    return {
        "data": customer,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/customers
# ---------------------------------------------------------------------------


@router.get("")
async def list_customers(
    request: Request,
    tenant: TenantContext = Depends(require_customers_enabled),
    cursor: Optional[str] = Query(default=None, description="Cursor for pagination (customer_id of last item)"),
    limit: int = Query(default=50, ge=1, le=200, description="Page size (default 50, max 200)"),
    status: Optional[str] = Query(default=None, description="Filter by status: active or archived"),
) -> dict:
    """List Customers with cursor/limit pagination.

    Tenant-scoped via ``inject_tenant_filter``. Default limit is 50,
    max 200.

    Validates: Requirements 1.3, C3
    """
    service = _get_customer_service()

    result = await service.list(
        tenant.tenant_id,
        cursor=cursor,
        limit=limit,
        status=status,
    )

    return {
        "data": result["items"],
        "next_cursor": result["next_cursor"],
        "limit": result["limit"],
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/customers/{customer_id}
# ---------------------------------------------------------------------------


@router.get("/{customer_id}")
async def get_customer(
    customer_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_customers_enabled),
) -> dict:
    """Retrieve a single Customer by ID with aggregate projections.

    Includes ``open_invoice_count``, ``open_balance_cents``,
    ``lifetime_revenue_cents``, and ``account_count`` computed against
    the tenant's accounts and invoices.

    Validates: Requirements 1.4, C3
    """
    service = _get_customer_service()

    customer = await service.get_with_projections(tenant.tenant_id, customer_id)

    return {
        "data": customer,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# PATCH /api/commerce/customers/{customer_id}
# ---------------------------------------------------------------------------


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: str,
    request: Request,
    body: UpdateCustomerRequest,
    tenant: TenantContext = Depends(require_customers_enabled),
) -> dict:
    """Update an existing Customer record.

    When ``status=archived`` is set and the customer has open invoices,
    returns HTTP 409 with the list of blocking invoice IDs.

    Validates: Requirements 1.2, 1.6
    """
    service = _get_customer_service()

    # If archiving, use the dedicated archive method that checks open invoices
    if body.status == "archived":
        customer = await service.archive(tenant.tenant_id, customer_id)
    else:
        # Build kwargs for partial update, only including provided fields
        update_kwargs: Dict[str, Any] = {}
        if body.display_name is not None:
            update_kwargs["display_name"] = body.display_name
        if body.legal_name is not None:
            update_kwargs["legal_name"] = body.legal_name
        if body.primary_email is not None:
            update_kwargs["primary_email"] = body.primary_email
        if body.tax_id is not None:
            update_kwargs["tax_id"] = body.tax_id
        if body.status is not None:
            update_kwargs["status"] = body.status

        customer = await service.update(
            tenant.tenant_id,
            customer_id,
            **update_kwargs,
        )

    return {
        "data": customer,
        "request_id": _get_request_id(request),
    }
