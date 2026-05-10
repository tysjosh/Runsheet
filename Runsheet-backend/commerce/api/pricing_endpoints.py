"""Sales Pricing Rules REST endpoints for the Fuel Compliance Backbone.

Exposes CRUD over pricing rules plus a price-resolution endpoint
(Task 5.10 of the fuel-compliance-backbone spec). Mounted under
``/api/commerce/pricing-rules`` and ``/api/commerce/pricing``.

Endpoints:

* ``POST   /api/commerce/pricing-rules``       — create rule
* ``GET    /api/commerce/pricing-rules``       — list / filter
* ``POST   /api/commerce/pricing/resolve``     — resolve price for a delivery

Wiring pattern mirrors :mod:`commerce.api.price_protection_endpoints`:

1. Module-level ``_es_service`` is populated by
   :func:`configure_pricing_api` at application startup
   (``bootstrap/compliance.py``).
2. Each handler constructs a per-request, tenant-scoped
   :class:`SalesPricingEngine` from the
   :class:`TenantContext` resolved by :func:`get_tenant_context`.
3. ``tenant_id`` on new rules is stamped from the verified JWT
   claim so clients cannot seed cross-tenant rows (Constraint C3).

Validates: Requirement 11.1, 11.2
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from commerce.models.pricing_rule import PricingRule
from commerce.services.sales_pricing_engine import (
    PricingNoRuleMatchedError,
    SalesPricingEngine,
)
from compliance.services.compliance_es_mappings import PRICING_RULES_INDEX
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_pricing_api()
# ---------------------------------------------------------------------------

_es_service: Optional[Any] = None

router = APIRouter(
    prefix="/api/commerce",
    tags=["commerce-pricing"],
)


def configure_pricing_api(*, es_service: Any) -> None:
    """Wire the Elasticsearch service into this module.

    Called once during application startup
    (``bootstrap/compliance.py``) so per-request handlers can
    construct a tenant-scoped :class:`SalesPricingEngine`.
    """
    global _es_service
    _es_service = es_service


def _get_es_service() -> Any:
    """Return the configured Elasticsearch service or raise."""
    if _es_service is None:
        raise RuntimeError(
            "Pricing API not configured. "
            "Call configure_pricing_api() during startup."
        )
    return _es_service


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PricingRuleCreateRequest(BaseModel):
    """Body for ``POST /api/commerce/pricing-rules``."""

    model_config = ConfigDict(extra="forbid")

    customer_id: Optional[str] = Field(
        default=None,
        description="Customer scope (None = product-default rule).",
    )
    account_id: Optional[str] = Field(
        default=None,
        description="Account scope (None = no account restriction).",
    )
    product_code: str = Field(
        ..., description="Canonical fuel product code."
    )
    strategy: str = Field(
        ...,
        description=(
            "posted_price | rack_plus_margin | tiered_volume | cost_plus"
        ),
    )
    margin_cents: Optional[int] = Field(
        default=None, description="Margin in cents (rack_plus_margin, cost_plus)."
    )
    posted_price_cents: Optional[int] = Field(
        default=None, description="Fixed price in cents (posted_price)."
    )
    tier_thresholds: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Tier breaks (tiered_volume)."
    )
    freight_rate_cents_per_mile: Optional[int] = Field(
        default=None, description="Freight rate (cost_plus)."
    )
    terminal_id: Optional[str] = Field(
        default=None, description="Terminal for rack price lookup."
    )
    priority: int = Field(
        default=0, description="Lower = higher priority."
    )
    effective_date: date = Field(
        ..., description="Rule effective start date."
    )
    expiry_date: Optional[date] = Field(
        default=None, description="Rule expiry date (None = no expiry)."
    )


class PriceResolveRequest(BaseModel):
    """Body for ``POST /api/commerce/pricing/resolve``."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(..., description="Customer being invoiced.")
    product_code: str = Field(..., description="Fuel product code.")
    gallons: float = Field(..., gt=0, description="Delivered volume.")
    terminal_id: str = Field(..., description="Source terminal.")
    route_miles: float = Field(
        default=0.0, ge=0, description="Route distance in miles."
    )
    effective_date: date = Field(
        ..., description="Invoice / delivery date."
    )
    market_price_cents: Optional[int] = Field(
        default=None,
        description="Current rack price (optional, for rack-based strategies).",
    )
    account_id: Optional[str] = Field(
        default=None, description="Billing account."
    )


# ---------------------------------------------------------------------------
# POST /api/commerce/pricing-rules — create
# ---------------------------------------------------------------------------


@router.post("/pricing-rules", status_code=201)
async def create_pricing_rule(
    request: Request,
    body: PricingRuleCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Create a new pricing rule.

    Validates: Requirement 11.1
    """
    payload = body.model_dump(exclude_none=False)
    payload["tenant_id"] = tenant.tenant_id
    payload["status"] = "active"
    now = utcnow().isoformat()
    payload["created_at"] = now
    payload["updated_at"] = now

    try:
        rule = PricingRule.model_validate(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "pricing_rule.invalid_payload",
                "message": str(exc),
            },
        )

    document = rule.model_dump(mode="json")

    es = _get_es_service()
    await es.index_document(PRICING_RULES_INDEX, rule.rule_id, document)

    logger.info(
        "pricing_rules.create: tenant=%s rule=%s strategy=%s product=%s",
        tenant.tenant_id,
        rule.rule_id,
        rule.strategy,
        rule.product_code,
    )

    return {
        "data": document,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/pricing-rules — list
# ---------------------------------------------------------------------------


@router.get("/pricing-rules")
async def list_pricing_rules(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    customer_id: Optional[str] = Query(default=None),
    product_code: Optional[str] = Query(default=None),
    strategy: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    size: int = Query(default=200, ge=1, le=1000),
) -> Dict[str, Any]:
    """List pricing rules for the tenant with optional filters.

    Validates: Requirement 11.1
    """
    es = _get_es_service()

    filters: List[Dict[str, Any]] = []
    if customer_id is not None:
        filters.append({"term": {"customer_id": customer_id.strip()}})
    if product_code is not None:
        filters.append({"term": {"product_code": product_code.strip()}})
    if strategy is not None:
        filters.append({"term": {"strategy": strategy.strip()}})
    if status is not None:
        filters.append({"term": {"status": status.strip()}})

    base_query: Dict[str, Any] = {
        "query": (
            {"bool": {"filter": filters}} if filters else {"match_all": {}}
        ),
        "size": size,
    }
    query = inject_tenant_filter(base_query, tenant.tenant_id)

    response = await es.search_documents(PRICING_RULES_INDEX, query, size=size)

    hits = ((response or {}).get("hits") or {}).get("hits") or []
    items: List[Dict[str, Any]] = []
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if isinstance(source, dict):
            items.append(source)

    return {
        "data": items,
        "count": len(items),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/commerce/pricing/resolve — resolve price
# ---------------------------------------------------------------------------


@router.post("/pricing/resolve")
async def resolve_price(
    request: Request,
    body: PriceResolveRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Resolve the sell price for a delivery.

    Calls :meth:`SalesPricingEngine.resolve_price` and returns the
    resolution result.

    Validates: Requirement 11.2
    """
    es = _get_es_service()
    engine = SalesPricingEngine(es_service=es, tenant_id=tenant.tenant_id)

    try:
        resolution = await engine.resolve_price(
            customer_id=body.customer_id,
            product_code=body.product_code,
            gallons=body.gallons,
            terminal_id=body.terminal_id,
            route_miles=body.route_miles,
            effective_date=body.effective_date,
            market_price_cents=body.market_price_cents,
            account_id=body.account_id,
        )
    except PricingNoRuleMatchedError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": exc.error_code,
                "message": str(exc),
            },
        )
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "pricing.not_implemented",
                "message": str(exc),
            },
        )

    return {
        "data": {
            "effective_price_cents": resolution.effective_price_cents,
            "market_price_cents": resolution.market_price_cents,
            "contract_id": resolution.contract_id,
            "contract_type": resolution.contract_type,
        },
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_pricing_api",
    "router",
    "PricingRuleCreateRequest",
    "PriceResolveRequest",
]
