"""Account REST endpoints for the Commerce Backbone.

Provides CRUD endpoints for Account records under /api/commerce/accounts,
including credit-override POST/DELETE and per-account aging.

All endpoints require ``commerce.backbone_enabled`` feature flag to be
active for the requesting tenant — returns HTTP 404 when the flag is off
(Req 8.1, 8.2).

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6, 7.1, 8.1, 8.2
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config.settings import get_settings
from commerce.services.account_service import AccountService
from commerce.services.credit_service import CreditService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.ref_resolver import get_ref_resolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service references, wired via configure_account_api()
# ---------------------------------------------------------------------------

_account_service: Optional[AccountService] = None
_credit_service: Optional[CreditService] = None
#: Optional resolver override (tests inject a fake); falls back to the
#: process-wide resolver registered at bootstrap.
_ref_resolver = None

router = APIRouter(prefix="/api/commerce/accounts", tags=["commerce-accounts"])


def configure_account_api(
    *,
    account_service: AccountService,
    credit_service: CreditService,
    ref_resolver=None,
) -> None:
    """Wire service dependencies into the account API module.

    Called once during application startup so that the router handlers
    can access the shared services without circular imports. An optional
    ``ref_resolver`` overrides the process-wide resolver used to expand the
    ``customer`` reference link (Req 12.4); when omitted the handlers fall
    back to :func:`services.ref_resolver.get_ref_resolver`.
    """
    global _account_service, _credit_service, _ref_resolver
    _account_service = account_service
    _credit_service = credit_service
    _ref_resolver = ref_resolver


def _get_account_service() -> AccountService:
    """Return the configured AccountService or raise."""
    if _account_service is None:
        raise RuntimeError(
            "Account API not configured. Call configure_account_api() during startup."
        )
    return _account_service


def _get_credit_service() -> CreditService:
    """Return the configured CreditService or raise."""
    if _credit_service is None:
        raise RuntimeError(
            "Account API not configured. Call configure_account_api() during startup."
        )
    return _credit_service


def _get_ref_resolver():
    """Return the resolver used to expand reference links (Req 12.4, 5.4)."""
    return _ref_resolver if _ref_resolver is not None else get_ref_resolver()


# ---------------------------------------------------------------------------
# Feature-flag gate dependency
# ---------------------------------------------------------------------------


async def require_accounts_enabled(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """FastAPI dependency that checks commerce feature flags for the tenant.

    Returns HTTP 404 when ``commerce.backbone_enabled`` is off, making
    the endpoints invisible to tenants that have not been migrated.

    Validates: Requirements 8.1, 8.2
    """
    settings = get_settings()

    if not settings.commerce_backbone_enabled:
        logger.debug(
            "Commerce account request blocked: commerce_backbone_enabled=False "
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

    return tenant


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateAccountRequest(BaseModel):
    """Request body for POST /api/commerce/accounts."""

    customer_id: str = Field(..., description="ID of the owning Customer")
    display_name: str = Field(..., description="Account display name")
    credit_limit_cents: int = Field(default=0, description="Credit limit in cents")
    net_terms_days: int = Field(default=30, description="Net payment terms in days")
    billing_address: Optional[Dict[str, Any]] = Field(
        default=None, description="Billing address object"
    )
    payment_method_preference: str = Field(
        default="invoice", description="Preferred payment method: invoice|ach|card"
    )
    status: str = Field(default="active", description="Initial status: active|suspended|closed")
    tier: str = Field(default="default", description="Pricing tier: platinum|gold|silver|bronze|default")


class UpdateAccountRequest(BaseModel):
    """Request body for PATCH /api/commerce/accounts/{account_id}."""

    display_name: Optional[str] = Field(default=None, description="Account display name")
    credit_limit_cents: Optional[int] = Field(default=None, description="Credit limit in cents")
    net_terms_days: Optional[int] = Field(default=None, description="Net payment terms in days")
    billing_address: Optional[Dict[str, Any]] = Field(
        default=None, description="Billing address object"
    )
    payment_method_preference: Optional[str] = Field(
        default=None, description="Preferred payment method: invoice|ach|card"
    )
    status: Optional[str] = Field(default=None, description="Status: active|suspended|closed")
    tier: Optional[str] = Field(
        default=None, description="Pricing tier: platinum|gold|silver|bronze|default"
    )


class CreditOverrideRequest(BaseModel):
    """Request body for POST /api/commerce/accounts/{account_id}/credit-override."""

    reason: str = Field(..., description="Reason for the credit override")
    authorized_by: str = Field(..., description="User who authorized the override")
    expires_at: str = Field(
        ..., description="ISO 8601 datetime when the override expires"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


def _parse_expires_at(expires_at_str: str) -> datetime:
    """Parse an ISO 8601 datetime string into a timezone-aware datetime.

    Raises HTTPException 422 if the string is not a valid datetime.
    """
    try:
        dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "INVALID_EXPIRES_AT",
                "message": f"expires_at must be a valid ISO 8601 datetime, got: {expires_at_str}",
            },
        )


# ---------------------------------------------------------------------------
# POST /api/commerce/accounts
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_account(
    request: Request,
    body: CreateAccountRequest,
    tenant: TenantContext = Depends(require_accounts_enabled),
) -> dict:
    """Create a new Account record.

    Returns 201 with the created account document including the
    server-assigned ``account_id`` of shape ``acct_<uuid4>``.
    Asserts the referenced Customer exists under the caller's tenant.

    Validates: Requirements 2.1, 2.2, 2.3
    """
    service = _get_account_service()

    account = await service.create(
        tenant.tenant_id,
        customer_id=body.customer_id,
        display_name=body.display_name,
        credit_limit_cents=body.credit_limit_cents,
        net_terms_days=body.net_terms_days,
        billing_address=body.billing_address,
        payment_method_preference=body.payment_method_preference,
        status=body.status,
        tier=body.tier,
        actor=tenant.user_id,
    )

    return {
        "data": account,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/accounts
# ---------------------------------------------------------------------------


@router.get("")
async def list_accounts(
    request: Request,
    tenant: TenantContext = Depends(require_accounts_enabled),
    customer_id: Optional[str] = Query(default=None, description="Filter by customer_id"),
    status: Optional[str] = Query(default=None, description="Filter by status: active|suspended|closed"),
    cursor: Optional[str] = Query(default=None, description="Cursor for pagination (account_id of last item)"),
    limit: int = Query(default=50, ge=1, le=200, description="Page size (default 50, max 200)"),
) -> dict:
    """List Accounts with cursor/limit pagination.

    Tenant-scoped via ``inject_tenant_filter``. Default limit is 50,
    max 200. Supports filtering by customer_id and status.

    Validates: Constraint C3
    """
    service = _get_account_service()

    result = await service.list(
        tenant.tenant_id,
        customer_id=customer_id,
        status=status,
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
# GET /api/commerce/accounts/{account_id}
# ---------------------------------------------------------------------------

#: Reference types ``GET /api/commerce/accounts/{id}?expand=...`` can resolve.
_VALID_ACCOUNT_EXPAND = ("customer",)


def _parse_expand(expand: Optional[str]) -> set:
    """Parse a comma-separated ``expand`` query into a set of known tokens.

    Unknown tokens are ignored so the param stays additive/forward-compatible.
    """
    if not expand:
        return set()
    requested = {tok.strip() for tok in expand.split(",") if tok.strip()}
    return requested & set(_VALID_ACCOUNT_EXPAND)


async def _build_account_links(
    tenant_id: str, account: Dict[str, Any], expand: set
) -> Dict[str, Any]:
    """Resolve the requested account references into a ``links`` object.

    An account's ``customer_id`` becomes a resolvable reference to its owning
    customer (Req 12.4). Resolution is tenant-scoped via the loaders;
    references never cross tenants (Req 5.3) and are returned resolved or
    explicitly ``unresolved``/``empty`` — never silently omitted (Req 5.4 /
    Property 4).
    """
    refs: Dict[str, Any] = {}
    if "customer" in expand:
        refs["customer"] = ("customer", account.get("customer_id"))

    resolver = _get_ref_resolver()
    resolved = await resolver.resolve_many(tenant_id, refs)
    return {key: ref.to_dict() for key, ref in resolved.items()}


@router.get("/{account_id}")
async def get_account(
    account_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_accounts_enabled),
    expand: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated references to resolve into a `links` object: "
            "customer. Omitted → no `links` key (additive, Req 6.3)."
        ),
    ),
) -> dict:
    """Retrieve a single Account by ID with computed fields.

    Includes ``open_balance_cents``, ``available_credit_cents``,
    ``oldest_open_invoice_days``, and ``credit_state``. When ``expand`` is
    supplied, a ``links`` object resolves the account's ``customer_id`` into a
    navigable reference to its owning customer (resolved or explicitly
    ``unresolved``) so the billing chain can be traversed up to the customer
    (Req 12.4). Reads without ``expand`` return the unchanged contract
    (Req 6.3).

    All resolution is tenant-scoped; references never cross tenants (Req 5.3).

    Validates: Requirements 2.4, 12.4, 5.4, C3
    """
    service = _get_account_service()

    account = await service.get(tenant.tenant_id, account_id)

    response: Dict[str, Any] = {
        "data": account,
        "request_id": _get_request_id(request),
    }

    requested = _parse_expand(expand)
    if requested:
        response["links"] = await _build_account_links(
            tenant.tenant_id, account, requested
        )

    return response


# ---------------------------------------------------------------------------
# PATCH /api/commerce/accounts/{account_id}
# ---------------------------------------------------------------------------


@router.patch("/{account_id}")
async def update_account(
    account_id: str,
    request: Request,
    body: UpdateAccountRequest,
    tenant: TenantContext = Depends(require_accounts_enabled),
) -> dict:
    """Update an existing Account record.

    Only non-None fields are applied. Validates credit_limit_cents and
    net_terms_days when provided.

    Validates: Requirements 2.2, 2.3
    """
    service = _get_account_service()

    # Build kwargs for partial update, only including provided fields
    update_kwargs: Dict[str, Any] = {}
    if body.display_name is not None:
        update_kwargs["display_name"] = body.display_name
    if body.credit_limit_cents is not None:
        update_kwargs["credit_limit_cents"] = body.credit_limit_cents
    if body.net_terms_days is not None:
        update_kwargs["net_terms_days"] = body.net_terms_days
    if body.billing_address is not None:
        update_kwargs["billing_address"] = body.billing_address
    if body.payment_method_preference is not None:
        update_kwargs["payment_method_preference"] = body.payment_method_preference
    if body.status is not None:
        update_kwargs["status"] = body.status
    if body.tier is not None:
        update_kwargs["tier"] = body.tier

    account = await service.update(
        tenant.tenant_id,
        account_id,
        actor=tenant.user_id,
        **update_kwargs,
    )

    return {
        "data": account,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/commerce/accounts/{account_id}/credit-override
# ---------------------------------------------------------------------------


@router.post("/{account_id}/credit-override", status_code=201)
async def apply_credit_override(
    account_id: str,
    request: Request,
    body: CreditOverrideRequest,
    tenant: TenantContext = Depends(require_accounts_enabled),
) -> dict:
    """Apply a one-shot credit override that bypasses credit checks.

    The override remains active until ``expires_at`` OR until a single
    order clears (whichever comes first). Writes the override to
    ``account_events`` for audit.

    Validates: Requirement 2.6
    """
    credit_service = _get_credit_service()

    expires_at = _parse_expires_at(body.expires_at)

    await credit_service.apply_override(
        tenant_id=tenant.tenant_id,
        account_id=account_id,
        reason=body.reason,
        authorized_by=body.authorized_by,
        expires_at=expires_at,
    )

    # Fetch the updated account to return current state
    account_service = _get_account_service()
    account = await account_service.get(tenant.tenant_id, account_id)

    return {
        "data": account,
        "message": "Credit override applied successfully",
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# DELETE /api/commerce/accounts/{account_id}/credit-override
# ---------------------------------------------------------------------------


@router.delete("/{account_id}/credit-override")
async def expire_credit_override(
    account_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_accounts_enabled),
) -> dict:
    """Expire an active credit override for the account.

    After expiring the override, re-evaluates credit state to determine
    whether the account should transition to 'ok' or 'hold'.

    Validates: Requirement 2.6
    """
    credit_service = _get_credit_service()

    await credit_service.expire_override(
        tenant_id=tenant.tenant_id,
        account_id=account_id,
    )

    # Fetch the updated account to return current state
    account_service = _get_account_service()
    account = await account_service.get(tenant.tenant_id, account_id)

    return {
        "data": account,
        "message": "Credit override expired",
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/accounts/{account_id}/aging
# ---------------------------------------------------------------------------


@router.get("/{account_id}/aging")
async def get_account_aging(
    account_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_accounts_enabled),
) -> dict:
    """Return AR aging bucket breakdown for the account.

    Returns ``{bucket_0_30_cents, bucket_31_60_cents, bucket_61_90_cents,
    bucket_90_plus_cents, total_open_cents}`` computed against the current
    moment via ``utcnow()``.

    Validates: Requirement 7.1

    Note: Full implementation in Phase 10 (ar_aging_service). This endpoint
    provides a direct computation against invoices_current for now.
    """
    from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX
    from ops.middleware.tenant_guard import inject_tenant_filter
    from services.time_utils import utcnow

    # First verify the account exists under this tenant
    account_service = _get_account_service()
    await account_service.get(tenant.tenant_id, account_id)

    # Use the account service's ES client for the aging query
    es_service = account_service._es
    now = utcnow()

    # Query all open invoices for this account
    query: Dict[str, Any] = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"account_id": account_id}},
                    {"terms": {"status": ["open", "partial", "overdue"]}},
                ]
            }
        },
        "size": 10000,
        "_source": ["remaining_cents", "issued_at"],
    }
    query = inject_tenant_filter(query, tenant.tenant_id)

    response = await es_service.search_documents(
        INVOICES_CURRENT_INDEX, query, size=10000
    )

    hits = response.get("hits", {}).get("hits", [])

    # Compute buckets
    bucket_0_30_cents = 0
    bucket_31_60_cents = 0
    bucket_61_90_cents = 0
    bucket_90_plus_cents = 0

    for hit in hits:
        source = hit["_source"]
        remaining = int(source.get("remaining_cents", 0))
        issued_at_str = source.get("issued_at")

        if not issued_at_str or remaining <= 0:
            continue

        # Parse issued_at
        try:
            if isinstance(issued_at_str, str):
                issued_at = datetime.fromisoformat(
                    issued_at_str.replace("Z", "+00:00")
                )
            else:
                issued_at = issued_at_str
        except (ValueError, TypeError):
            continue

        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=timezone.utc)

        now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        days_old = (now_aware - issued_at).days

        if days_old <= 30:
            bucket_0_30_cents += remaining
        elif days_old <= 60:
            bucket_31_60_cents += remaining
        elif days_old <= 90:
            bucket_61_90_cents += remaining
        else:
            bucket_90_plus_cents += remaining

    total_open_cents = (
        bucket_0_30_cents + bucket_31_60_cents + bucket_61_90_cents + bucket_90_plus_cents
    )

    return {
        "data": {
            "account_id": account_id,
            "bucket_0_30_cents": bucket_0_30_cents,
            "bucket_31_60_cents": bucket_31_60_cents,
            "bucket_61_90_cents": bucket_61_90_cents,
            "bucket_90_plus_cents": bucket_90_plus_cents,
            "total_open_cents": total_open_cents,
        },
        "request_id": _get_request_id(request),
    }
