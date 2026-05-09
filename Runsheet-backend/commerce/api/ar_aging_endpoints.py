"""AR Aging REST endpoints for the Commerce Backbone.

Provides tenant-level AR aging aggregation and historical snapshot
endpoints under /api/commerce/ar-aging.

All endpoints require ``commerce.backbone_enabled`` feature flag to be
active for the requesting tenant — returns HTTP 404 when the flag is off
(Req 8.1, 8.2).

Validates: Requirements 7.2, 9.4, 8.1, 8.2
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from config.settings import get_settings
from commerce.services.ar_aging_service import ARAgingService
from commerce.services.commerce_es_mappings import AR_AGING_SNAPSHOTS_INDEX
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_ar_aging_api()
# ---------------------------------------------------------------------------

_ar_aging_service: Optional[ARAgingService] = None

router = APIRouter(prefix="/api/commerce/ar-aging", tags=["commerce-ar-aging"])


def configure_ar_aging_api(*, ar_aging_service: ARAgingService) -> None:
    """Wire service dependencies into the AR aging API module.

    Called once during application startup so that the router handlers
    can access the shared ARAgingService without circular imports.
    """
    global _ar_aging_service
    _ar_aging_service = ar_aging_service


def _get_ar_aging_service() -> ARAgingService:
    """Return the configured ARAgingService or raise."""
    if _ar_aging_service is None:
        raise RuntimeError(
            "AR Aging API not configured. Call configure_ar_aging_api() during startup."
        )
    return _ar_aging_service


# ---------------------------------------------------------------------------
# Feature-flag gate dependency
# ---------------------------------------------------------------------------


async def require_ar_aging_enabled(
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
            "Commerce AR aging request blocked: commerce_backbone_enabled=False "
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
# Helpers
# ---------------------------------------------------------------------------


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# GET /api/commerce/ar-aging
# ---------------------------------------------------------------------------


@router.get("")
async def get_tenant_aging(
    request: Request,
    tenant: TenantContext = Depends(require_ar_aging_enabled),
) -> dict:
    """Return tenant-level AR aging aggregated across all accounts.

    Returns ``{bucket_0_30_cents, bucket_31_60_cents, bucket_61_90_cents,
    bucket_90_plus_cents, total_open_cents, by_account: [...top 50...]}``
    computed against the current moment via ``utcnow()``.

    The ``by_account`` array contains the top 50 accounts sorted by
    ``total_open_cents`` descending, each with their own bucket breakdown.

    Validates: Requirement 7.2
    """
    service = _get_ar_aging_service()

    aging = await service.compute_tenant_aging(tenant.tenant_id)

    return {
        "data": aging,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/commerce/ar-aging/history?from=&to=
# ---------------------------------------------------------------------------


@router.get("/history")
async def get_aging_history(
    request: Request,
    tenant: TenantContext = Depends(require_ar_aging_enabled),
    from_date: Optional[str] = Query(
        default=None,
        alias="from",
        description="Start date (inclusive) in YYYY-MM-DD format",
    ),
    to_date: Optional[str] = Query(
        default=None,
        alias="to",
        description="End date (inclusive) in YYYY-MM-DD format",
    ),
) -> dict:
    """Return historical AR aging snapshots for trend charts.

    Queries the ``ar_aging_snapshots`` index for the tenant, optionally
    filtered by a date range (``from`` / ``to`` query params in
    YYYY-MM-DD format).

    Returns snapshots ordered by ``snapshot_date`` ascending for
    chronological charting.

    Validates: Requirement 9.4
    """
    service = _get_ar_aging_service()

    # Build the ES query
    must_clauses: list = []

    # Date range filter
    if from_date or to_date:
        date_range: Dict[str, Any] = {}
        if from_date:
            date_range["gte"] = from_date
        if to_date:
            date_range["lte"] = to_date
        must_clauses.append({"range": {"snapshot_date": date_range}})

    query: Dict[str, Any] = {
        "query": {
            "bool": {
                "must": must_clauses if must_clauses else [{"match_all": {}}],
            }
        },
        "sort": [{"snapshot_date": {"order": "asc"}}],
        "size": 1000,
    }
    query = inject_tenant_filter(query, tenant.tenant_id)

    response = await service._es.search_documents(
        AR_AGING_SNAPSHOTS_INDEX, query, size=1000
    )

    hits = response.get("hits", {}).get("hits", [])
    snapshots = [hit["_source"] for hit in hits]

    return {
        "data": snapshots,
        "count": len(snapshots),
        "request_id": _get_request_id(request),
    }
