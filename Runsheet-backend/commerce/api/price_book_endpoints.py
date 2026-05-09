"""Price Book REST endpoints for the Commerce Backbone.

Provides CRUD endpoints for PriceBook records under /api/commerce/price-books,
including the ``/pricing/resolve`` dry-run resolver for UI preview.

All endpoints require ``commerce.backbone_enabled`` and
``commerce.pricing_engine_enabled`` feature flags to be active for the
requesting tenant — returns HTTP 404 when either flag is off (Req 8.1, 8.2).

Validates: Requirements 3.1, 3.4, 3.6, 8.1, 8.2
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config.settings import get_settings
from commerce.services.price_book_service import PriceBookService
from commerce.services.pricing_engine import PricingEngine, PricingError
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service references, wired via configure_price_book_api()
# ---------------------------------------------------------------------------

_price_book_service: Optional[PriceBookService] = None
_pricing_engine: Optional[PricingEngine] = None

router = APIRouter(prefix="/api/commerce/price-books", tags=["commerce-price-books"])
pricing_router = APIRouter(prefix="/api/commerce/pricing", tags=["commerce-pricing"])


def configure_price_book_api(
    *,
    price_book_service: PriceBookService,
    pricing_engine: PricingEngine,
) -> None:
    """Wire service dependencies into the price book API module.

    Called once during application startup so that the router handlers
    can access the shared services without circular imports.
    """
    global _price_book_service, _pricing_engine
    _price_book_service = price_book_service
    _pricing_engine = pricing_engine


def _get_price_book_service() -> PriceBookService:
    """Return the configured PriceBookService or raise."""
    if _price_book_service is None:
        raise RuntimeError(
            "Price Book API not configured. Call configure_price_book_api() during startup."
        )
    return _price_book_service


def _get_pricing_engine() -> PricingEngine:
    """Return the configured PricingEngine or raise."""
    if _pricing_engine is None:
        raise RuntimeError(
            "Price Book API not configured. Call configure_price_book_api() during startup."
        )
    return _pricing_engine


# ---------------------------------------------------------------------------
# Feature-flag gate dependency
# ---------------------------------------------------------------------------


async def require_pricing_enabled(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """FastAPI dependency that checks commerce + pricing feature flags.

    Returns HTTP 404 when either ``commerce.backbone_enabled`` or
    ``commerce.pricing_engine_enabled`` is off, making the endpoints
    invisible to tenants that have not been migrated.

    Validates: Requirements 8.1, 8.2
    """
    settings = get_settings()

    if not settings.commerce_backbone_enabled:
        logger.debug(
            "Commerce price-book request blocked: commerce_backbone_enabled=False "
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

    if not settings.commerce_pricing_engine_enabled:
        logger.debug(
            "Commerce price-book request blocked: commerce_pricing_engine_enabled=False "
            "for tenant_id=%s",
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "PRICING_DISABLED",
                "message": "Commerce pricing engine is not enabled for this tenant",
            },
        )

    return tenant


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class PricingRuleRequest(BaseModel):
    """A single pricing rule within a price book."""

    product_code: str = Field(..., description="Canonical product code")
    scope_type: str = Field(
        default="default",
        description="Scope type: account | tier | default",
    )
    scope_value: str = Field(
        default="default",
        description="Scope value: account_id, tier name, or 'default'",
    )
    effective_from: str = Field(..., description="ISO 8601 start of effective window")
    effective_to: Optional[str] = Field(
        default=None, description="ISO 8601 end of effective window (null = indefinite)"
    )
    min_quantity_gallons: Optional[float] = Field(
        default=None, description="Minimum quantity in gallons for this break"
    )
    unit_price_cents: int = Field(..., description="Unit price in cents (integer)")


class CreatePriceBookRequest(BaseModel):
    """Request body for POST /api/commerce/price-books."""

    name: str = Field(..., description="Price book name")
    description: Optional[str] = Field(default=None, description="Price book description")
    status: str = Field(default="draft", description="Initial status: draft | active")
    rules: List[PricingRuleRequest] = Field(
        default_factory=list, description="List of pricing rules"
    )


class UpdatePriceBookRequest(BaseModel):
    """Request body for PUT /api/commerce/price-books/{price_book_id}."""

    name: Optional[str] = Field(default=None, description="Price book name")
    description: Optional[str] = Field(default=None, description="Price book description")
    status: Optional[str] = Field(default=None, description="Status: draft | active | archived")
    rules: Optional[List[PricingRuleRequest]] = Field(
        default=None, description="Replacement list of pricing rules"
    )


class PricingResolveRequest(BaseModel):
    """Request body for POST /api/commerce/pricing/resolve (dry-run resolver)."""

    account_id: str = Field(..., description="Account ID to resolve pricing for")
    product_code: str = Field(..., description="Product code to price")
    quantity_gallons: float = Field(..., description="Quantity in gallons")
    moment: Optional[str] = Field(
        default=None,
        description="ISO 8601 datetime for effective-window evaluation (defaults to now)",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


def _rules_to_dicts(rules: List[PricingRuleRequest]) -> List[Dict[str, Any]]:
    """Convert PricingRuleRequest list to plain dicts for the service layer."""
    return [rule.model_dump() for rule in rules]


# ---------------------------------------------------------------------------
# POST /api/commerce/price-books
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_price_book(
    request: Request,
    body: CreatePriceBookRequest,
    tenant: TenantContext = Depends(require_pricing_enabled),
) -> dict:
    """Create a new PriceBook with rules.

    Validates each rule (product_code canonicalized, unit_price_cents >= 0,
    effective window coherent), persists the book to price_books_current,
    and fans rules into pricing_rules_current if status is active.

    Returns 201 with the created price book document.

    Validates: Requirements 3.1, C1, C3, C6
    """
    service = _get_price_book_service()

    price_book = await service.create(
        tenant.tenant_id,
        name=body.name,
        description=body.description,
        status=body.status,
        rules=_rules_to_dicts(body.rules),
    )

    return {
        "data": price_book,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/price-books
# ---------------------------------------------------------------------------


@router.get("")
async def list_price_books(
    request: Request,
    tenant: TenantContext = Depends(require_pricing_enabled),
    cursor: Optional[str] = Query(
        default=None, description="Cursor for pagination (price_book_id of last item)"
    ),
    limit: int = Query(default=50, ge=1, le=200, description="Page size (default 50, max 200)"),
    status: Optional[str] = Query(
        default=None, description="Filter by status: draft | active | archived"
    ),
) -> dict:
    """List PriceBooks with cursor/limit pagination.

    Tenant-scoped via ``inject_tenant_filter``. Default limit is 50, max 200.

    Validates: Constraint C3
    """
    service = _get_price_book_service()

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
# GET /api/commerce/price-books/{price_book_id}
# ---------------------------------------------------------------------------


@router.get("/{price_book_id}")
async def get_price_book(
    price_book_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_pricing_enabled),
) -> dict:
    """Retrieve a single PriceBook by ID with its rules.

    Validates: Constraint C3
    """
    service = _get_price_book_service()

    price_book = await service.get(tenant.tenant_id, price_book_id)

    return {
        "data": price_book,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# PUT /api/commerce/price-books/{price_book_id}
# ---------------------------------------------------------------------------


@router.put("/{price_book_id}")
async def update_price_book(
    price_book_id: str,
    request: Request,
    body: UpdatePriceBookRequest,
    tenant: TenantContext = Depends(require_pricing_enabled),
) -> dict:
    """Update an existing PriceBook.

    Edits do NOT retroactively re-price already-invoiced orders (Req 3.4).
    Book mutations bump the cache invalidation key so edits propagate
    within 5 minutes (Req 3.6).

    Validates: Requirements 3.4, 3.6, C1, C3, C6
    """
    service = _get_price_book_service()

    # Build kwargs — only pass fields that were explicitly provided
    update_kwargs: Dict[str, Any] = {}
    if body.name is not None:
        update_kwargs["name"] = body.name
    if body.description is not None:
        update_kwargs["description"] = body.description
    if body.status is not None:
        update_kwargs["status"] = body.status
    if body.rules is not None:
        update_kwargs["rules"] = _rules_to_dicts(body.rules)

    price_book = await service.update(
        tenant.tenant_id,
        price_book_id,
        **update_kwargs,
    )

    return {
        "data": price_book,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/commerce/price-books/{price_book_id}/activate
# ---------------------------------------------------------------------------


@router.post("/{price_book_id}/activate")
async def activate_price_book(
    price_book_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_pricing_enabled),
) -> dict:
    """Activate a PriceBook, fanning its rules into pricing_rules_current.

    When activated:
    1. Status transitions to 'active'
    2. Rules are fanned out into pricing_rules_current (denormalized)
    3. Cache invalidation key is bumped so PricingEngine picks up changes

    Validates: Requirements 3.1, 3.6, C3
    """
    service = _get_price_book_service()

    price_book = await service.activate(tenant.tenant_id, price_book_id)

    return {
        "data": price_book,
        "message": "Price book activated successfully",
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/commerce/pricing/resolve (dry-run resolver)
# ---------------------------------------------------------------------------


@pricing_router.post("/resolve")
async def resolve_pricing(
    request: Request,
    body: PricingResolveRequest,
    tenant: TenantContext = Depends(require_pricing_enabled),
) -> dict:
    """Dry-run pricing resolver for UI preview.

    Calls PricingEngine.resolve() with the provided parameters without
    persisting anything. Returns the resolved pricing result including
    unit_price_cents, rule_id, scope_type, and whether the result came
    from cache.

    This endpoint is intended for the PriceBookEditor UI to preview
    how a given (account, product, quantity, moment) tuple would resolve
    against the current active rules.

    Validates: Requirements 3.2, 3.3, 3.5
    """
    engine = _get_pricing_engine()

    # Parse optional moment
    moment: Optional[datetime] = None
    if body.moment:
        try:
            moment = datetime.fromisoformat(body.moment.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "INVALID_MOMENT",
                    "message": f"moment must be a valid ISO 8601 datetime, got: {body.moment}",
                },
            )

    # Fetch the account to pass to the resolver
    from commerce.services.account_service import AccountService
    from commerce.models.account import Account, AccountTier, CreditState

    # We need to look up the account to get its tier for scope matching
    account_service = _get_account_service_for_resolve()
    account_data = await account_service.get(tenant.tenant_id, body.account_id)

    # Build a minimal Account object for the pricing engine
    account = Account(
        account_id=account_data.get("account_id", body.account_id),
        tenant_id=tenant.tenant_id,
        customer_id=account_data.get("customer_id", ""),
        display_name=account_data.get("display_name", ""),
        tier=account_data.get("tier", "default"),
        credit_limit_cents=account_data.get("credit_limit_cents", 0),
        net_terms_days=account_data.get("net_terms_days", 30),
    )

    try:
        result = await engine.resolve(
            tenant_id=tenant.tenant_id,
            account=account,
            product_code=body.product_code,
            moment=moment,
            quantity_gallons=body.quantity_gallons,
        )

        return {
            "data": {
                "unit_price_cents": result.unit_price_cents,
                "rule_id": result.rule_id,
                "scope_type": result.scope_type.value if hasattr(result.scope_type, "value") else str(result.scope_type),
                "matched_from_cache": result.matched_from_cache,
                "product_code": body.product_code,
                "account_id": body.account_id,
                "quantity_gallons": body.quantity_gallons,
            },
            "request_id": _get_request_id(request),
        }
    except PricingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        )


# ---------------------------------------------------------------------------
# Internal helper for resolve endpoint
# ---------------------------------------------------------------------------

_account_service_ref: Optional[Any] = None


def configure_account_service_for_resolve(account_service: Any) -> None:
    """Wire the AccountService reference for the resolve endpoint."""
    global _account_service_ref
    _account_service_ref = account_service


def _get_account_service_for_resolve() -> Any:
    """Return the AccountService for the resolve endpoint."""
    if _account_service_ref is not None:
        return _account_service_ref

    # Fallback: try to get from the price_book_service's ES client
    if _price_book_service is not None:
        from commerce.services.account_service import AccountService
        return AccountService(es_service=_price_book_service._es)

    raise RuntimeError(
        "No AccountService available for pricing resolve. "
        "Call configure_account_service_for_resolve() during startup."
    )
