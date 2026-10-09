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

* ``GET /api/compliance/hos-records/daily-summary/export`` — CSV of duty-status
  minutes per driver per UTC day (OI-20, owner decision 2026-10-07). Admin
  only, advisory (Runsheet is not an ELD); see
  :mod:`compliance.services.driver_hours_summary`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence

from fastapi import APIRouter, Depends, Query, Request

from compliance.api._authz import (
    compliance_admin_dependency,
    compliance_ops_dependency,
)
from compliance.services.driver_hours_summary import (
    STATUS_COLUMNS,
    summarize_duty_status_days,
)
from driver.services.driver_es_mappings import (
    DUTY_STATUS_EVENTS_INDEX,
    VEHICLE_INSPECTIONS_INDEX,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)
from schemas.common import paginated_response_dict
from services.csv_export import (
    EXPORT_PAGE_SIZE,
    EXPORT_RATE_LIMIT,
    MAX_EXPORT_ROWS,
    ExportColumn,
    export_guard,
    export_rate_key,
    stream_csv_export,
)
from services.date_range import doc_range_clause, parse_date_range
from services.keyset_pagination import raw_keyset_info

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


# ---------------------------------------------------------------------------
# GET /api/compliance/hos-records/daily-summary/export (OI-20)
# ---------------------------------------------------------------------------

#: Longest range one driver hours export may cover.
DRIVER_HOURS_MAX_DAYS = 31

#: No name, phone, email or licence number (data-export FR4).
_DRIVER_HOURS_COLUMNS = [
    ExportColumn(name, (lambda n: lambda r: r.get(n))(name))
    for name in (
        "date", "driver_id", *STATUS_COLUMNS.values(), "event_count",
        "first_event_at", "last_event_at", "basis",
    )
]


class _DriverHoursSource:
    """ExportSource that reads the range's events, then aggregates them.

    ``count()`` returns the number of *events* when it is over the cap, so
    ``stream_csv_export`` answers 413 (and audits ``rejected_too_large``)
    before reading them all; otherwise it reads every event with a 2-key
    keyset sort and returns the number of summary rows.
    """

    def __init__(
        self,
        es: Any,
        tenant_id: str,
        base_query: Dict[str, Any],
        range_start: datetime,
        range_end: datetime,
        max_events: int,
    ) -> None:
        self._es = es
        self._tenant_id = tenant_id
        self._base_query = base_query
        self._range_start = range_start
        self._range_end = range_end
        self._max_events = max_events
        self._rows: List[Dict[str, Any]] = []

    async def _page(self, after: Optional[tuple]) -> tuple[List[Dict[str, Any]], int, Optional[tuple], int]:
        query = dict(self._base_query)
        query["sort"] = [
            {"event_timestamp": {"order": "asc"}},
            {"event_id": {"order": "asc"}},
        ]
        query["from"] = 0
        query["size"] = EXPORT_PAGE_SIZE
        if after is not None:
            query["search_after"] = [after[0], after[1]]
        resp = await self._es.search_documents(
            DUTY_STATUS_EVENTS_INDEX, query, EXPORT_PAGE_SIZE
        )
        raw_count, last_key = raw_keyset_info(resp, keyset=True)
        hits_outer = resp.get("hits", {}) if hasattr(resp, "get") else {}
        total_block = hits_outer.get("total", {}) or {}
        total = (
            int(total_block.get("value", 0) or 0)
            if hasattr(total_block, "get") else int(total_block or 0)
        )
        events: List[Dict[str, Any]] = []
        for hit in hits_outer.get("hits", []) or []:
            source = hit.get("_source") if hasattr(hit, "get") else None
            if not source:
                continue
            if source.get("tenant_id") != self._tenant_id:
                logger.warning(
                    "compliance.driver_hours_export: dropping event with "
                    "mismatched tenant_id %s (expected %s)",
                    source.get("tenant_id"), self._tenant_id,
                )
                continue
            events.append(source)
        return events, raw_count, last_key, total

    async def count(self) -> int:
        events, raw_count, last_key, total = await self._page(None)
        if total > self._max_events:
            return total
        collected = list(events)
        while raw_count >= EXPORT_PAGE_SIZE and last_key is not None:
            events, raw_count, last_key, _ = await self._page(last_key)
            collected.extend(events)
            if len(collected) > self._max_events:
                return len(collected)
        self._rows = summarize_duty_status_days(
            collected, self._range_start, self._range_end,
            datetime.now(timezone.utc),
        )
        return len(self._rows)

    async def pages(self) -> AsyncIterator[Sequence[Mapping[str, Any]]]:
        for start in range(0, len(self._rows), EXPORT_PAGE_SIZE):
            yield self._rows[start:start + EXPORT_PAGE_SIZE]


@router.get("/hos-records/daily-summary/export", response_model=None)
@limiter.limit(EXPORT_RATE_LIMIT, key_func=export_rate_key)
async def export_driver_hours_summary(
    request: Request,
    tenant: TenantContext = Depends(export_guard("admin", base=get_tenant_context)),
    driver_id: Optional[str] = Query(default=None, description="Filter to one driver."),
    start_date: str = Query(..., description="YYYY-MM-DD or ISO-8601 (UTC)."),
    end_date: str = Query(
        ..., description="YYYY-MM-DD (whole day included) or ISO-8601."
    ),
):
    """CSV of duty-status minutes per driver per UTC day (admin, advisory).

    The time between two consecutive events belongs to the earlier event's
    status, split at UTC midnight; the last event runs to the range end or
    now, whichever is earlier. Time before a driver's first event in the
    range is not attributed — ``first_event_at`` shows where counting
    starts. Runsheet is not an ELD, so ``basis`` labels every row advisory.
    Over 50,000 events in the range is a 413 ``EXPORT_TOO_LARGE``.
    """
    date_range = parse_date_range(start_date, end_date)
    range_start = date_range.gte
    range_end = date_range.lt or date_range.lte
    if range_start is None or range_end is None:  # pragma: no cover — both required
        raise _validation("start_date", start_date)
    if range_end - range_start > timedelta(days=DRIVER_HOURS_MAX_DAYS):
        raise _validation("end_date", end_date, reason="range_over_31_days")
    driver = driver_id.strip() if driver_id else None
    if driver and len(driver) > _MAX_DRIVER_ID_LEN:
        raise _validation("driver_id", driver[:_MAX_DRIVER_ID_LEN])

    must: List[Dict[str, Any]] = []
    if driver:
        must.append({"term": {"driver_id": driver}})
    clause = doc_range_clause("event_timestamp", date_range, z_suffix=False)
    if clause is not None:
        must.append(clause)
    base_query = inject_tenant_filter({"query": {"bool": {"must": must}}}, tenant.tenant_id)

    source = _DriverHoursSource(
        _get_es(), tenant.tenant_id, base_query, range_start, range_end,
        MAX_EXPORT_ROWS,
    )
    return await stream_csv_export(
        request=request, tenant=tenant, export_type="driver_hours",
        columns=_DRIVER_HOURS_COLUMNS, source=source,
        filters={"driver_id": driver, "start_date": start_date, "end_date": end_date},
        max_rows=MAX_EXPORT_ROWS,
    )
