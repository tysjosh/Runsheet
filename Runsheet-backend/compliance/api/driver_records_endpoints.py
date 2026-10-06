"""Tenant-wide HOS and DVIR record lists for admins (data-export G3, design §5).

* ``GET /api/compliance/hos-records`` — duty-status events across every
  driver in the tenant.
* ``GET /api/compliance/inspections`` — vehicle inspections (DVIRs) across
  every driver in the tenant.

Both are admin only: the router carries the ops gate every compliance module
has (``tests/unit/test_compliance_api_authz.py`` pins it) plus an admin gate,
so the effective rule is ``admin``. Attached to the router so a route added
later inherits both.

Offset paging (``page`` / ``size``, max 200) with the same contract as the
orders and jobs lists, capped at the store's 10,000-row result window.
The driver-facing ``GET /api/driver/hos`` and ``POST /api/driver/inspections``
are untouched.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request

from compliance.api._authz import (
    compliance_admin_dependency,
    compliance_ops_dependency,
)
from driver.services.driver_es_mappings import (
    DUTY_STATUS_EVENTS_INDEX,
    VEHICLE_INSPECTIONS_INDEX,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)
from schemas.common import paginated_response_dict
from services.date_range import doc_range_clause, parse_date_range

logger = logging.getLogger(__name__)

#: Elasticsearch's default ``index.max_result_window``, which the store matches.
MAX_RESULT_WINDOW = 10_000
_MAX_DRIVER_ID_LEN = 128

_es_service: Any = None

router = APIRouter(
    prefix="/api/compliance", tags=["Compliance"],
    dependencies=[
        Depends(compliance_ops_dependency),
        Depends(compliance_admin_dependency),
    ],
)


def configure_driver_records_api(*, es_service: Any) -> None:
    """Inject the document-store facade (tests); defaults to the process one."""
    global _es_service
    _es_service = es_service


def _get_es() -> Any:
    if _es_service is not None:
        return _es_service
    from services.elasticsearch_service import elasticsearch_service
    return elasticsearch_service


def _validation(field: str, value: Any, *, reason: Optional[str] = None) -> AppException:
    details: Dict[str, Any] = {"field": field}
    if reason:
        details["reason"] = reason
    else:
        details["value"] = value
    return AppException(
        error_code=ErrorCode.VALIDATION_ERROR,
        message=f"Invalid {field}",
        status_code=422,
        details=details,
    )


async def _list_records(
    request: Request,
    tenant: TenantContext,
    *,
    index: str,
    date_field: str,
    id_field: str,
    driver_id: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    page: int,
    size: int,
) -> Dict[str, Any]:
    date_range = parse_date_range(start_date, end_date)
    driver = driver_id.strip() if driver_id else None
    if driver and len(driver) > _MAX_DRIVER_ID_LEN:
        raise _validation("driver_id", driver[:_MAX_DRIVER_ID_LEN])
    if page * size > MAX_RESULT_WINDOW:
        raise _validation("page", page, reason="window_exceeded")

    must: List[Dict[str, Any]] = []
    if driver:
        must.append({"term": {"driver_id": driver}})
    # Timestamps are stored as ``client_timestamp.isoformat()``; the driver
    # app sends UTC, so ``+00:00`` bounds compare correctly (design §5).
    clause = doc_range_clause(date_field, date_range, z_suffix=False)
    if clause is not None:
        must.append(clause)
    inner: Dict[str, Any] = {
        "query": {"bool": {"must": must}} if must else {"match_all": {}},
        "sort": [{date_field: {"order": "desc"}}, {id_field: {"order": "asc"}}],
        "from": (page - 1) * size,
        "size": size,
    }
    query = inject_tenant_filter(inner, tenant.tenant_id)
    resp = await _get_es().search_documents(index, query, size)

    hits_outer = resp.get("hits", {}) if hasattr(resp, "get") else {}
    total_block = hits_outer.get("total", {}) or {}
    total = int(total_block.get("value", 0) or 0) if hasattr(total_block, "get") else int(total_block or 0)
    items: List[Dict[str, Any]] = []
    for hit in hits_outer.get("hits", []) or []:
        source = hit.get("_source") if hasattr(hit, "get") else None
        if not source:
            continue
        if source.get("tenant_id") != tenant.tenant_id:
            logger.warning(
                "compliance.driver_records: dropping %s row with mismatched "
                "tenant_id %s (expected %s)",
                index, source.get("tenant_id"), tenant.tenant_id,
            )
            continue
        items.append({k: v for k, v in source.items() if k != "tenant_id"})

    return paginated_response_dict(
        items=items,
        total=total,
        page=page,
        page_size=size,
        request_id=getattr(request.state, "request_id", "unknown"),
    )


@router.get("/hos-records")
async def list_hos_records(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    driver_id: Optional[str] = Query(default=None, description="Filter to one driver."),
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD or ISO-8601 (UTC)."),
    end_date: Optional[str] = Query(
        default=None, description="YYYY-MM-DD (whole day included) or ISO-8601."
    ),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """Duty-status events for every driver in the tenant, newest first (admin)."""
    return await _list_records(
        request, tenant,
        index=DUTY_STATUS_EVENTS_INDEX, date_field="event_timestamp",
        id_field="event_id", driver_id=driver_id,
        start_date=start_date, end_date=end_date, page=page, size=size,
    )


@router.get("/inspections")
async def list_inspections(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    driver_id: Optional[str] = Query(default=None, description="Filter to one driver."),
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD or ISO-8601 (UTC)."),
    end_date: Optional[str] = Query(
        default=None, description="YYYY-MM-DD (whole day included) or ISO-8601."
    ),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """Vehicle inspections (DVIRs) for every driver in the tenant, newest first (admin)."""
    return await _list_records(
        request, tenant,
        index=VEHICLE_INSPECTIONS_INDEX, date_field="inspection_timestamp",
        id_field="inspection_id", driver_id=driver_id,
        start_date=start_date, end_date=end_date, page=page, size=size,
    )
