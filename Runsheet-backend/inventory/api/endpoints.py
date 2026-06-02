"""
Inventory API endpoints for the Fleet Inventory & Maintenance Supplies module.

Provides REST endpoints for inventory item management, stock adjustments,
low-stock alerts, and summary aggregation under the /api/inventory prefix.

All endpoints are rate-limited and tenant-scoped via JWT.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from config.settings import get_settings
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from schemas.common import paginated_response_dict
from inventory.models import (
    CreateInventoryItem,
    StockAdjustment,
    UpdateInventoryItem,
)
from inventory.service import InventoryService

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_inventory_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service reference, wired via configure_inventory_api()
_inventory_service: Optional[InventoryService] = None

router = APIRouter(prefix="/api/inventory", tags=["inventory"])


def configure_inventory_api(*, inventory_service: InventoryService) -> None:
    """
    Wire service dependencies into the inventory API module.

    Called once during application startup (from bootstrap) so that the
    router handlers can access the shared InventoryService.
    """
    global _inventory_service
    _inventory_service = inventory_service


def _get_inventory_service() -> InventoryService:
    """Return the configured InventoryService or raise."""
    if _inventory_service is None:
        raise RuntimeError(
            "Inventory API not configured. Call configure_inventory_api() during startup."
        )
    return _inventory_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# List items
# ---------------------------------------------------------------------------


@router.get("/items")
@limiter.limit(_inventory_rate)
async def list_items(
    request: Request,
    category: Optional[str] = Query(default=None, description="Filter by category"),
    status: Optional[str] = Query(default=None, description="Filter by status"),
    location: Optional[str] = Query(default=None, description="Filter by location"),
    page: int = Query(default=1, ge=1, description="Page number"),
    size: int = Query(default=50, ge=1, le=100, description="Page size"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """List inventory items with optional filters and pagination."""
    service = _get_inventory_service()
    result = await service.list_items(
        tenant_id=tenant.tenant_id,
        category=category,
        status=status,
        location=location,
        page=page,
        size=size,
    )

    items_data = [item.model_dump() for item in result["items"]]
    return paginated_response_dict(
        items=items_data,
        total=result["total"],
        page=result["page"],
        page_size=result["size"],
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# Get single item
# ---------------------------------------------------------------------------


@router.get("/items/{item_id}")
@limiter.limit(_inventory_rate)
async def get_item(
    request: Request,
    item_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Get a single inventory item by ID."""
    service = _get_inventory_service()
    item = await service.get_item(item_id=item_id, tenant_id=tenant.tenant_id)
    return {
        "data": item.model_dump(),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Create item
# ---------------------------------------------------------------------------


@router.post("/items", status_code=201)
@limiter.limit(_inventory_rate)
async def create_item(
    request: Request,
    body: CreateInventoryItem,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Register a new inventory item."""
    service = _get_inventory_service()
    item = await service.create_item(data=body, tenant_id=tenant.tenant_id)
    return {
        "data": item.model_dump(),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Update item
# ---------------------------------------------------------------------------


@router.patch("/items/{item_id}")
@limiter.limit(_inventory_rate)
async def update_item(
    request: Request,
    item_id: str,
    body: UpdateInventoryItem,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Partially update an inventory item."""
    service = _get_inventory_service()
    item = await service.update_item(
        item_id=item_id, data=body, tenant_id=tenant.tenant_id
    )
    return {
        "data": item.model_dump(),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Delete item
# ---------------------------------------------------------------------------


@router.delete("/items/{item_id}", status_code=204, response_model=None)
@limiter.limit(_inventory_rate)
async def delete_item(
    request: Request,
    item_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Delete an inventory item by ID."""
    service = _get_inventory_service()
    await service.delete_item(item_id=item_id, tenant_id=tenant.tenant_id)
    return None


# ---------------------------------------------------------------------------
# Stock adjustment
# ---------------------------------------------------------------------------


@router.post("/items/{item_id}/adjust")
@limiter.limit(_inventory_rate)
async def adjust_stock(
    request: Request,
    item_id: str,
    body: StockAdjustment,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Record a stock adjustment (restock or consumption).

    Positive quantity_change = restock, negative = consumption.
    """
    service = _get_inventory_service()
    # Use tenant_id as actor_id fallback (in production, extract from JWT)
    actor_id = tenant.tenant_id
    result = await service.adjust_stock(
        item_id=item_id,
        adjustment=body,
        tenant_id=tenant.tenant_id,
        actor_id=actor_id,
    )
    return {
        "data": result.model_dump(),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Low-stock alerts
# ---------------------------------------------------------------------------


@router.get("/alerts")
@limiter.limit(_inventory_rate)
async def get_alerts(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Get items that are below their low-stock threshold or out of stock."""
    service = _get_inventory_service()
    alerts = await service.get_low_stock_alerts(tenant_id=tenant.tenant_id)
    return {
        "data": [item.model_dump() for item in alerts],
        "count": len(alerts),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@router.get("/summary")
@limiter.limit(_inventory_rate)
async def get_summary(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Get aggregated inventory summary (counts, value, categories)."""
    service = _get_inventory_service()
    summary = await service.get_summary(tenant_id=tenant.tenant_id)
    return {
        "data": summary.model_dump(),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Item history
# ---------------------------------------------------------------------------


@router.get("/items/{item_id}/history")
@limiter.limit(_inventory_rate)
async def get_item_history(
    request: Request,
    item_id: str,
    page: int = Query(default=1, ge=1, description="Page number"),
    size: int = Query(default=50, ge=1, le=100, description="Page size"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Get stock movement history for an item."""
    service = _get_inventory_service()
    result = await service.get_item_history(
        item_id=item_id,
        tenant_id=tenant.tenant_id,
        page=page,
        size=size,
    )
    return paginated_response_dict(
        items=result["items"],
        total=result["total"],
        page=result["page"],
        page_size=result["size"],
        request_id=_get_request_id(request),
    )
