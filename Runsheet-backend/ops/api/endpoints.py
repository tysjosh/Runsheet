"""
Ops API read endpoints for the Ops Intelligence Layer.

Provides normalized REST endpoints for shipment, rider, and event data
with tenant-scoped query guards and consistent JSON response envelopes.

All endpoints return responses in the envelope:
    {data: [...], pagination: {page, size, total, total_pages}, request_id: "..."}

Validates: Requirements 8.1-8.6
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config.settings import get_settings
from errors.exceptions import validation_error
from middleware.rate_limiter import limiter
from ops.middleware.pii_masker import PIIMasker, log_pii_access
from ops.middleware.tenant_guard import TenantContext, get_tenant_context, inject_tenant_filter
from ops.services.ops_es_service import OpsElasticsearchService
from ops.services.feature_flags import FeatureFlagService
from ops.ingestion.replay import ReplayService, ReplayJobStatus, get_replay_service
from ops.services.drift_detector import DriftResult, get_drift_detector
from ops.services.ops_metrics import generate_metrics
from schemas.common import paginated_response_dict

logger = logging.getLogger(__name__)

# Shared PII masker instance
_pii_masker = PIIMasker()

# Load rate limit settings
_settings = get_settings()
_ops_rate = f"{_settings.ops_api_rate_limit}/minute"
_metrics_rate = f"{_settings.ops_metrics_rate_limit}/minute"

# Module-level service references, wired via configure_ops_api()
_ops_es_service: Optional[OpsElasticsearchService] = None
_feature_flag_service: Optional[FeatureFlagService] = None

router = APIRouter(prefix="/api/ops", tags=["ops"])

# Auth policy declaration for this router (Req 5.2)
# Default: JWT_REQUIRED for all ops endpoints
ROUTER_AUTH_POLICY = "jwt_required"


def configure_ops_api(
    *,
    ops_es_service: OpsElasticsearchService,
    feature_flag_service: Optional[FeatureFlagService] = None,
) -> None:
    """
    Wire service dependencies into the ops API module.

    Called once during application startup (from main.py) so that the
    router handlers can access shared services without circular imports.
    """
    global _ops_es_service, _feature_flag_service
    _ops_es_service = ops_es_service
    _feature_flag_service = feature_flag_service


async def require_ops_enabled(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """
    FastAPI dependency that checks the feature flag for the tenant.

    Raises HTTPException(404) with TENANT_DISABLED code when the Ops
    Intelligence Layer is disabled for the requesting tenant.

    Validates: Requirement 27.3
    """
    if _feature_flag_service is not None:
        enabled = await _feature_flag_service.is_enabled(tenant.tenant_id)
        if not enabled:
            logger.info(
                "Ops API request blocked: tenant_id=%s is disabled",
                tenant.tenant_id,
            )
            raise HTTPException(
                status_code=404,
                detail={
                    "error_code": "TENANT_DISABLED",
                    "message": "Ops intelligence is not enabled for this tenant",
                },
            )
    return tenant


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


def _get_es() -> OpsElasticsearchService:
    """Return the configured OpsElasticsearchService or raise."""
    if _ops_es_service is None:
        raise RuntimeError("Ops API not configured. Call configure_ops_api() during startup.")
    return _ops_es_service


def _mask_response_data(
    data: list | dict,
    tenant: TenantContext,
    request: Request,
) -> list | dict:
    """
    Apply role-based PII masking to ops API response data.

    Internal ops endpoints mask by default; unmask only if the JWT
    contains ``has_pii_access: true``.

    When unmasked data is returned, a PII access event is logged for
    compliance audit (Requirement 22.5).

    Validates: Requirements 22.1, 22.2, 22.4, 22.5
    """
    if tenant.has_pii_access:
        # Log the PII access event for audit
        pii_fields = _detect_pii_fields(data)
        if pii_fields:
            log_pii_access(
                user_id=tenant.user_id,
                tenant_id=tenant.tenant_id,
                fields_accessed=pii_fields,
                endpoint=f"{request.method} {request.url.path}",
            )
        return data
    return _pii_masker.mask_response(data)


def _detect_pii_fields(data: list | dict) -> list[str]:
    """Return a list of PII field names present in the response data."""
    found: set[str] = set()
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, val in item.items():
            if key in PIIMasker.NAME_FIELDS and val:
                found.add(key)
            elif isinstance(val, str):
                if PIIMasker.PHONE_PATTERN.fullmatch(val.strip()):
                    found.add(key)
                elif PIIMasker.EMAIL_PATTERN.fullmatch(val.strip()):
                    found.add(key)
    return sorted(found)


# Valid shipment statuses for filter validation (Req 10.1, 10.6)
VALID_SHIPMENT_STATUSES = {"pending", "in_transit", "delivered", "failed", "returned"}

# Valid rider statuses for filter validation
VALID_RIDER_STATUSES = {"active", "idle", "offline"}


def _validate_status(value: Optional[str], valid_values: set[str], field_name: str = "status") -> None:
    """Raise 400 if the status value is not in the allowed set. Validates: Req 10.6"""
    if value is not None and value not in valid_values:
        raise validation_error(
            message=f"Invalid {field_name} value '{value}'. Must be one of: {', '.join(sorted(valid_values))}",
            details={field_name: value, "valid_values": sorted(valid_values)},
        )


def _validate_date(value: Optional[str], field_name: str) -> None:
    """Raise 400 if the date string is not a valid ISO 8601 date. Validates: Req 10.6"""
    if value is None:
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise validation_error(
            message=f"Invalid {field_name} value '{value}'. Must be a valid ISO 8601 date string.",
            details={field_name: value},
        )


# ---------------------------------------------------------------------------
# GET /ops/shipments/sla-breaches — shipments past estimated delivery
# Validates: Requirement 10.2
# IMPORTANT: Must be defined BEFORE /shipments/{shipment_id} to avoid
# FastAPI treating "sla-breaches" as a shipment_id path parameter.
# ---------------------------------------------------------------------------
@router.get("/shipments/sla-breaches")
@limiter.limit(_ops_rate)
async def get_sla_breaches(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    status: Optional[str] = Query(None, description="Filter by shipment status"),
    rider_id: Optional[str] = Query(None, description="Filter by rider_id"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
) -> dict:
    """Return shipments where current time exceeds estimated_delivery."""
    _validate_status(status, VALID_SHIPMENT_STATUSES)
    es = _get_es()

    now = datetime.now(timezone.utc).isoformat()

    # SLA breach: estimated_delivery exists and is in the past
    filters: list[dict] = [
        {"range": {"estimated_delivery": {"lt": now}}},
        {"exists": {"field": "estimated_delivery"}},
    ]
    if status:
        filters.append({"term": {"status": status}})
    if rider_id:
        filters.append({"term": {"rider_id": rider_id}})

    inner_query = {"query": {"bool": {"must": filters}}}
    query = inject_tenant_filter(inner_query, tenant.tenant_id)

    from_offset = (page - 1) * size
    query["from"] = from_offset
    query["size"] = size
    query["sort"] = [{"estimated_delivery": {"order": "asc"}}]

    result = es.client.search(
        index=OpsElasticsearchService.SHIPMENTS_CURRENT, body=query
    )

    hits = result["hits"]["hits"]
    total = result["hits"]["total"]["value"]

    data = _mask_response_data(
        [hit["_source"] for hit in hits], tenant, request,
    )

    return paginated_response_dict(
        items=data,
        total=total,
        page=page,
        page_size=size,
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# GET /ops/shipments/failures — failed shipments with failure reason
# Validates: Requirement 10.4
# IMPORTANT: Must be defined BEFORE /shipments/{shipment_id} to avoid
# FastAPI treating "failures" as a shipment_id path parameter.
# ---------------------------------------------------------------------------
@router.get("/shipments/failures")
@limiter.limit(_ops_rate)
async def get_shipment_failures(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    rider_id: Optional[str] = Query(None, description="Filter by rider_id"),
    start_date: Optional[str] = Query(None, description="Filter from this ISO date"),
    end_date: Optional[str] = Query(None, description="Filter until this ISO date"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
) -> dict:
    """Return failed shipments with failure reason from the latest event."""
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    es = _get_es()

    filters: list[dict] = [
        {"term": {"status": "failed"}},
    ]
    if rider_id:
        filters.append({"term": {"rider_id": rider_id}})
    if start_date or end_date:
        date_range: dict = {}
        if start_date:
            date_range["gte"] = start_date
        if end_date:
            date_range["lte"] = end_date
        filters.append({"range": {"updated_at": date_range}})

    inner_query = {"query": {"bool": {"must": filters}}}
    query = inject_tenant_filter(inner_query, tenant.tenant_id)

    from_offset = (page - 1) * size
    query["from"] = from_offset
    query["size"] = size
    query["sort"] = [{"updated_at": {"order": "desc"}}]

    result = es.client.search(
        index=OpsElasticsearchService.SHIPMENTS_CURRENT, body=query
    )

    hits = result["hits"]["hits"]
    total = result["hits"]["total"]["value"]

    # Enrich each failed shipment with failure_reason from the latest event
    data = []
    for hit in hits:
        shipment = hit["_source"]
        # If failure_reason is not already on the shipment doc, try to get it
        # from the latest event in shipment_events
        if not shipment.get("failure_reason"):
            sid = shipment.get("shipment_id")
            if sid:
                event_query = inject_tenant_filter(
                    {"query": {"bool": {"must": [
                        {"term": {"shipment_id": sid}},
                        {"term": {"event_type": "shipment_failed"}},
                    ]}}},
                    tenant.tenant_id,
                )
                event_query["size"] = 1
                event_query["sort"] = [{"event_timestamp": {"order": "desc"}}]
                event_result = es.client.search(
                    index=OpsElasticsearchService.SHIPMENT_EVENTS, body=event_query
                )
                if event_result["hits"]["hits"]:
                    latest_event = event_result["hits"]["hits"][0]["_source"]
                    payload = latest_event.get("event_payload", {})
                    if isinstance(payload, dict):
                        shipment["failure_reason"] = payload.get("failure_reason", "unknown")
        data.append(shipment)

    return paginated_response_dict(
        items=_mask_response_data(data, tenant, request),
        total=total,
        page=page,
        page_size=size,
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# GET /ops/shipments — paginated shipments with filters and sorting
# Validates: Requirement 8.1, 10.1, 10.5
# ---------------------------------------------------------------------------
@router.get("/shipments")
@limiter.limit(_ops_rate)
async def list_shipments(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    status: Optional[str] = Query(None, description="Filter by shipment status"),
    rider_id: Optional[str] = Query(None, description="Filter by rider_id"),
    start_date: Optional[str] = Query(None, description="Filter events from this ISO date"),
    end_date: Optional[str] = Query(None, description="Filter events until this ISO date"),
    sort_by: str = Query("updated_at", description="Field to sort by"),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
) -> dict:
    """Return paginated shipments from shipments_current index."""
    _validate_status(status, VALID_SHIPMENT_STATUSES)
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    es = _get_es()

    # Build the inner query with optional filters
    filters: list[dict] = []
    if status:
        filters.append({"term": {"status": status}})
    if rider_id:
        filters.append({"term": {"rider_id": rider_id}})
    if start_date or end_date:
        date_range: dict = {}
        if start_date:
            date_range["gte"] = start_date
        if end_date:
            date_range["lte"] = end_date
        filters.append({"range": {"updated_at": date_range}})

    if filters:
        inner_query = {"query": {"bool": {"must": filters}}}
    else:
        inner_query = {"query": {"match_all": {}}}

    # Inject tenant scoping
    query = inject_tenant_filter(inner_query, tenant.tenant_id)

    # Pagination
    from_offset = (page - 1) * size
    query["from"] = from_offset
    query["size"] = size
    query["sort"] = [{sort_by: {"order": sort_order}}]

    result = es.client.search(index=OpsElasticsearchService.SHIPMENTS_CURRENT, body=query)

    hits = result["hits"]["hits"]
    total = result["hits"]["total"]["value"]

    return paginated_response_dict(
        items=_mask_response_data([hit["_source"] for hit in hits], tenant, request),
        total=total,
        page=page,
        page_size=size,
        request_id=_get_request_id(request),
    )



# ---------------------------------------------------------------------------
# GET /ops/shipments/{shipment_id} — single shipment with full event history
# Validates: Requirement 8.2
# ---------------------------------------------------------------------------
@router.get("/shipments/{shipment_id}")
@limiter.limit(_ops_rate)
async def get_shipment(
    shipment_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
) -> dict:
    """Return a single shipment with its full event history."""
    es = _get_es()

    # Fetch the shipment document (tenant-scoped)
    shipment_query = inject_tenant_filter(
        {"query": {"term": {"shipment_id": shipment_id}}},
        tenant.tenant_id,
    )
    shipment_query["size"] = 1

    shipment_result = es.client.search(
        index=OpsElasticsearchService.SHIPMENTS_CURRENT, body=shipment_query
    )

    if not shipment_result["hits"]["hits"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Shipment not found")

    shipment_data = shipment_result["hits"]["hits"][0]["_source"]

    # Fetch the full event history for this shipment (tenant-scoped)
    events_query = inject_tenant_filter(
        {"query": {"term": {"shipment_id": shipment_id}}},
        tenant.tenant_id,
    )
    events_query["size"] = 1000
    events_query["sort"] = [{"event_timestamp": {"order": "asc"}}]

    events_result = es.client.search(
        index=OpsElasticsearchService.SHIPMENT_EVENTS, body=events_query
    )

    events = [hit["_source"] for hit in events_result["hits"]["hits"]]

    shipment_data["events"] = events

    return {
        "data": _mask_response_data(shipment_data, tenant, request),
        "request_id": _get_request_id(request),
    }



# ---------------------------------------------------------------------------
# GET /ops/riders/utilization — riders with calculated utilization metrics
# Validates: Requirement 10.3
# IMPORTANT: Must be defined BEFORE /riders/{rider_id} to avoid
# FastAPI treating "utilization" as a rider_id path parameter.
# ---------------------------------------------------------------------------
@router.get("/riders/utilization")
@limiter.limit(_ops_rate)
async def get_rider_utilization(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    status: Optional[str] = Query(None, description="Filter by rider status"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
) -> dict:
    """Return rider records with calculated utilization metrics."""
    _validate_status(status, VALID_RIDER_STATUSES)
    es = _get_es()

    filters: list[dict] = []
    if status:
        filters.append({"term": {"status": status}})

    if filters:
        inner_query = {"query": {"bool": {"must": filters}}}
    else:
        inner_query = {"query": {"match_all": {}}}

    query = inject_tenant_filter(inner_query, tenant.tenant_id)

    from_offset = (page - 1) * size
    query["from"] = from_offset
    query["size"] = size
    query["sort"] = [{"last_seen": {"order": "desc"}}]

    result = es.client.search(
        index=OpsElasticsearchService.RIDERS_CURRENT, body=query
    )

    hits = result["hits"]["hits"]
    total = result["hits"]["total"]["value"]

    # Enrich each rider with utilization metrics
    data = []
    for hit in hits:
        rider = hit["_source"]
        active_count = rider.get("active_shipment_count", 0) or 0
        completed = rider.get("completed_today", 0) or 0

        # Calculate idle time based on last_seen
        idle_minutes = None
        last_seen_str = rider.get("last_seen")
        if last_seen_str:
            try:
                last_seen_dt = datetime.fromisoformat(
                    last_seen_str.replace("Z", "+00:00") if isinstance(last_seen_str, str) else last_seen_str
                )
                idle_minutes = max(
                    0,
                    int((datetime.now(timezone.utc) - last_seen_dt).total_seconds() / 60),
                )
            except (ValueError, TypeError):
                idle_minutes = None

        rider["utilization"] = {
            "active_shipments": active_count,
            "completed_today": completed,
            "idle_minutes": idle_minutes,
        }
        data.append(rider)

    return paginated_response_dict(
        items=_mask_response_data(data, tenant, request),
        total=total,
        page=page,
        page_size=size,
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# GET /ops/riders — paginated riders
# Validates: Requirement 8.3
# ---------------------------------------------------------------------------
@router.get("/riders")
@limiter.limit(_ops_rate)
async def list_riders(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    status: Optional[str] = Query(None, description="Filter by rider status"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
) -> dict:
    """Return paginated rider records from riders_current index."""
    es = _get_es()

    if status:
        inner_query = {"query": {"term": {"status": status}}}
    else:
        inner_query = {"query": {"match_all": {}}}

    query = inject_tenant_filter(inner_query, tenant.tenant_id)

    from_offset = (page - 1) * size
    query["from"] = from_offset
    query["size"] = size
    query["sort"] = [{"last_seen": {"order": "desc"}}]

    result = es.client.search(
        index=OpsElasticsearchService.RIDERS_CURRENT, body=query
    )

    hits = result["hits"]["hits"]
    total = result["hits"]["total"]["value"]

    return paginated_response_dict(
        items=_mask_response_data([hit["_source"] for hit in hits], tenant, request),
        total=total,
        page=page,
        page_size=size,
        request_id=_get_request_id(request),
    )



# ---------------------------------------------------------------------------
# GET /ops/riders/{rider_id} — single rider with assigned shipment details
# Validates: Requirement 8.4
# ---------------------------------------------------------------------------
@router.get("/riders/{rider_id}")
@limiter.limit(_ops_rate)
async def get_rider(
    rider_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
) -> dict:
    """Return a single rider with their assigned shipment details."""
    es = _get_es()

    # Fetch the rider document (tenant-scoped)
    rider_query = inject_tenant_filter(
        {"query": {"term": {"rider_id": rider_id}}},
        tenant.tenant_id,
    )
    rider_query["size"] = 1

    rider_result = es.client.search(
        index=OpsElasticsearchService.RIDERS_CURRENT, body=rider_query
    )

    if not rider_result["hits"]["hits"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Rider not found")

    rider_data = rider_result["hits"]["hits"][0]["_source"]

    # Fetch shipments currently assigned to this rider (tenant-scoped)
    shipments_query = inject_tenant_filter(
        {"query": {"bool": {"must": [
            {"term": {"rider_id": rider_id}},
            {"terms": {"status": ["pending", "in_transit"]}},
        ]}}},
        tenant.tenant_id,
    )
    shipments_query["size"] = 100
    shipments_query["sort"] = [{"updated_at": {"order": "desc"}}]

    shipments_result = es.client.search(
        index=OpsElasticsearchService.SHIPMENTS_CURRENT, body=shipments_query
    )

    rider_data["assigned_shipments"] = [
        hit["_source"] for hit in shipments_result["hits"]["hits"]
    ]

    return {
        "data": _mask_response_data(rider_data, tenant, request),
        "request_id": _get_request_id(request),
    }



# ---------------------------------------------------------------------------
# GET /ops/events — paginated events with filters
# Validates: Requirement 8.5
# ---------------------------------------------------------------------------
@router.get("/events")
@limiter.limit(_ops_rate)
async def list_events(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    shipment_id: Optional[str] = Query(None, description="Filter by shipment_id"),
    event_type: Optional[str] = Query(None, description="Filter by event_type"),
    start_date: Optional[str] = Query(None, description="Filter events from this ISO date"),
    end_date: Optional[str] = Query(None, description="Filter events until this ISO date"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
) -> dict:
    """Return paginated event records from shipment_events index."""
    es = _get_es()

    filters: list[dict] = []
    if shipment_id:
        filters.append({"term": {"shipment_id": shipment_id}})
    if event_type:
        filters.append({"term": {"event_type": event_type}})
    if start_date or end_date:
        date_range: dict = {}
        if start_date:
            date_range["gte"] = start_date
        if end_date:
            date_range["lte"] = end_date
        filters.append({"range": {"event_timestamp": date_range}})

    if filters:
        inner_query = {"query": {"bool": {"must": filters}}}
    else:
        inner_query = {"query": {"match_all": {}}}

    query = inject_tenant_filter(inner_query, tenant.tenant_id)

    from_offset = (page - 1) * size
    query["from"] = from_offset
    query["size"] = size
    query["sort"] = [{"event_timestamp": {"order": "desc"}}]

    result = es.client.search(
        index=OpsElasticsearchService.SHIPMENT_EVENTS, body=query
    )

    hits = result["hits"]["hits"]
    total = result["hits"]["total"]["value"]

    return paginated_response_dict(
        items=_mask_response_data([hit["_source"] for hit in hits], tenant, request),
        total=total,
        page=page,
        page_size=size,
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# Aggregated Metrics Endpoints
# Validates: Requirements 11.1-11.6
# ---------------------------------------------------------------------------

VALID_BUCKETS = {"hourly", "daily"}


def _resolve_bucket(
    bucket: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> str:
    """
    Return the effective bucket granularity.

    If the requested time range exceeds 90 days, force daily granularity
    regardless of the caller's preference (Req 11.5).
    """
    if start_date and end_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if (end_dt - start_dt).days > 90:
                return "daily"
        except (ValueError, TypeError):
            pass
    return bucket


def _bucket_interval(bucket: str) -> str:
    """Map bucket name to ES calendar_interval value."""
    return "1h" if bucket == "hourly" else "1d"


def _build_date_range_filter(
    start_date: Optional[str],
    end_date: Optional[str],
    field: str = "updated_at",
) -> Optional[dict]:
    """Build an ES range filter dict, or None if no dates supplied."""
    if not start_date and not end_date:
        return None
    date_range: dict = {}
    if start_date:
        date_range["gte"] = start_date
    if end_date:
        date_range["lte"] = end_date
    return {"range": {field: date_range}}


@router.get("/metrics/shipments")
@limiter.limit(_metrics_rate)
async def get_shipment_metrics(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    bucket: str = Query("hourly", description="Bucket granularity: hourly or daily"),
    start_date: Optional[str] = Query(None, description="Start of time range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of time range (ISO 8601)"),
):
    """
    Shipment counts by status in hourly/daily buckets.
    Validates: Req 11.1, 11.5, 11.6
    """
    _validate_status(bucket, VALID_BUCKETS, field_name="bucket")
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    es = _get_es()

    effective_bucket = _resolve_bucket(bucket, start_date, end_date)
    interval = _bucket_interval(effective_bucket)

    filters: list[dict] = []
    dr = _build_date_range_filter(start_date, end_date, field="updated_at")
    if dr:
        filters.append(dr)

    if filters:
        inner_query = {"query": {"bool": {"must": filters}}}
    else:
        inner_query = {"query": {"match_all": {}}}

    query = inject_tenant_filter(inner_query, tenant.tenant_id)
    query["size"] = 0
    query["aggs"] = {
        "over_time": {
            "date_histogram": {
                "field": "updated_at",
                "calendar_interval": interval,
            },
            "aggs": {
                "by_status": {
                    "terms": {"field": "status", "size": 20}
                }
            },
        }
    }

    result = es.client.search(
        index=OpsElasticsearchService.SHIPMENTS_CURRENT, body=query
    )

    from ops.models import MetricsBucket, MetricsResponse

    buckets_data: list[MetricsBucket] = []
    for b in result.get("aggregations", {}).get("over_time", {}).get("buckets", []):
        values: dict = {}
        for status_bucket in b.get("by_status", {}).get("buckets", []):
            values[status_bucket["key"]] = status_bucket["doc_count"]
        values["total"] = b["doc_count"]
        buckets_data.append(MetricsBucket(timestamp=b["key_as_string"], values=values))

    return MetricsResponse(
        data=buckets_data,
        bucket=effective_bucket,
        start_date=start_date,
        end_date=end_date,
        request_id=_get_request_id(request),
    )


@router.get("/metrics/sla")
@limiter.limit(_metrics_rate)
async def get_sla_metrics(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    bucket: str = Query("hourly", description="Bucket granularity: hourly or daily"),
    start_date: Optional[str] = Query(None, description="Start of time range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of time range (ISO 8601)"),
):
    """
    SLA compliance percentage and breach counts in time buckets.
    Validates: Req 11.2, 11.5, 11.6
    """
    _validate_status(bucket, VALID_BUCKETS, field_name="bucket")
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    es = _get_es()

    effective_bucket = _resolve_bucket(bucket, start_date, end_date)
    interval = _bucket_interval(effective_bucket)

    # We need shipments that have an estimated_delivery so we can compare
    filters: list[dict] = [{"exists": {"field": "estimated_delivery"}}]
    dr = _build_date_range_filter(start_date, end_date, field="updated_at")
    if dr:
        filters.append(dr)

    inner_query = {"query": {"bool": {"must": filters}}}
    query = inject_tenant_filter(inner_query, tenant.tenant_id)
    query["size"] = 0
    query["aggs"] = {
        "over_time": {
            "date_histogram": {
                "field": "updated_at",
                "calendar_interval": interval,
            },
            "aggs": {
                "sla_breached": {
                    "filter": {
                        "script": {
                            "script": {
                                "source": (
                                    "doc['estimated_delivery'].size() > 0 && "
                                    "doc['last_event_timestamp'].size() > 0 && "
                                    "doc['last_event_timestamp'].value.isAfter(doc['estimated_delivery'].value)"
                                ),
                                "lang": "painless",
                            }
                        }
                    }
                },
            },
        }
    }

    result = es.client.search(
        index=OpsElasticsearchService.SHIPMENTS_CURRENT, body=query
    )

    from ops.models import MetricsBucket, MetricsResponse

    buckets_data: list[MetricsBucket] = []
    for b in result.get("aggregations", {}).get("over_time", {}).get("buckets", []):
        total = b["doc_count"]
        breached = b.get("sla_breached", {}).get("doc_count", 0)
        compliant = total - breached
        compliance_pct = round((compliant / total) * 100, 2) if total > 0 else 100.0
        buckets_data.append(
            MetricsBucket(
                timestamp=b["key_as_string"],
                values={
                    "total": total,
                    "breached": breached,
                    "compliant": compliant,
                    "compliance_pct": compliance_pct,
                },
            )
        )

    return MetricsResponse(
        data=buckets_data,
        bucket=effective_bucket,
        start_date=start_date,
        end_date=end_date,
        request_id=_get_request_id(request),
    )


@router.get("/metrics/riders")
@limiter.limit(_metrics_rate)
async def get_rider_metrics(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    bucket: str = Query("hourly", description="Bucket granularity: hourly or daily"),
    start_date: Optional[str] = Query(None, description="Start of time range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of time range (ISO 8601)"),
):
    """
    Rider utilization and availability metrics in time buckets.
    Validates: Req 11.3, 11.5, 11.6
    """
    _validate_status(bucket, VALID_BUCKETS, field_name="bucket")
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    es = _get_es()

    effective_bucket = _resolve_bucket(bucket, start_date, end_date)
    interval = _bucket_interval(effective_bucket)

    filters: list[dict] = []
    dr = _build_date_range_filter(start_date, end_date, field="last_seen")
    if dr:
        filters.append(dr)

    if filters:
        inner_query = {"query": {"bool": {"must": filters}}}
    else:
        inner_query = {"query": {"match_all": {}}}

    query = inject_tenant_filter(inner_query, tenant.tenant_id)
    query["size"] = 0
    query["aggs"] = {
        "over_time": {
            "date_histogram": {
                "field": "last_seen",
                "calendar_interval": interval,
            },
            "aggs": {
                "by_status": {
                    "terms": {"field": "status", "size": 20}
                },
                "avg_active_shipments": {
                    "avg": {"field": "active_shipment_count"}
                },
                "avg_completed_today": {
                    "avg": {"field": "completed_today"}
                },
            },
        }
    }

    result = es.client.search(
        index=OpsElasticsearchService.RIDERS_CURRENT, body=query
    )

    from ops.models import MetricsBucket, MetricsResponse

    buckets_data: list[MetricsBucket] = []
    for b in result.get("aggregations", {}).get("over_time", {}).get("buckets", []):
        values: dict = {"total_riders": b["doc_count"]}
        for status_bucket in b.get("by_status", {}).get("buckets", []):
            values[f"status_{status_bucket['key']}"] = status_bucket["doc_count"]
        values["avg_active_shipments"] = b.get("avg_active_shipments", {}).get("value")
        values["avg_completed_today"] = b.get("avg_completed_today", {}).get("value")
        buckets_data.append(MetricsBucket(timestamp=b["key_as_string"], values=values))

    return MetricsResponse(
        data=buckets_data,
        bucket=effective_bucket,
        start_date=start_date,
        end_date=end_date,
        request_id=_get_request_id(request),
    )


@router.get("/metrics/failures")
@limiter.limit(_metrics_rate)
async def get_failure_metrics(
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
    bucket: str = Query("hourly", description="Bucket granularity: hourly or daily"),
    start_date: Optional[str] = Query(None, description="Start of time range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of time range (ISO 8601)"),
):
    """
    Failure counts by reason in time buckets.
    Validates: Req 11.4, 11.5, 11.6
    """
    _validate_status(bucket, VALID_BUCKETS, field_name="bucket")
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")
    es = _get_es()

    effective_bucket = _resolve_bucket(bucket, start_date, end_date)
    interval = _bucket_interval(effective_bucket)

    filters: list[dict] = [{"term": {"status": "failed"}}]
    dr = _build_date_range_filter(start_date, end_date, field="updated_at")
    if dr:
        filters.append(dr)

    inner_query = {"query": {"bool": {"must": filters}}}
    query = inject_tenant_filter(inner_query, tenant.tenant_id)
    query["size"] = 0
    query["aggs"] = {
        "over_time": {
            "date_histogram": {
                "field": "updated_at",
                "calendar_interval": interval,
            },
            "aggs": {
                "by_reason": {
                    "terms": {"field": "failure_reason", "size": 50}
                }
            },
        }
    }

    result = es.client.search(
        index=OpsElasticsearchService.SHIPMENTS_CURRENT, body=query
    )

    from ops.models import MetricsBucket, MetricsResponse

    buckets_data: list[MetricsBucket] = []
    for b in result.get("aggregations", {}).get("over_time", {}).get("buckets", []):
        values: dict = {"total_failures": b["doc_count"]}
        for reason_bucket in b.get("by_reason", {}).get("buckets", []):
            values[reason_bucket["key"]] = reason_bucket["doc_count"]
        buckets_data.append(MetricsBucket(timestamp=b["key_as_string"], values=values))

    return MetricsResponse(
        data=buckets_data,
        bucket=effective_bucket,
        start_date=start_date,
        end_date=end_date,
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# Prometheus Metrics Endpoint
# Validates: Requirements 23.4-23.6
# ---------------------------------------------------------------------------

@router.get("/metrics/prometheus")
@limiter.limit(_metrics_rate)
async def get_prometheus_metrics(request: Request):
    """
    Expose Prometheus-compatible metrics for scraping.

    Returns metrics in Prometheus text exposition format.
    Validates: Req 23.6
    """
    from fastapi.responses import Response

    return Response(
        content=generate_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Monitoring Endpoints
# Validates: Requirements 23.1-23.3
# These return simple dicts (not paginated) since they're operational metrics.
# ---------------------------------------------------------------------------

@router.get("/monitoring/ingestion")
@limiter.limit(_ops_rate)
async def get_ingestion_metrics(
    request: Request,
    window: str = Query("5m", description="Time window for metrics (e.g. 5m, 1h, 24h)"),
):
    """
    Ingestion health: events received, processed, failed, avg latency.
    Validates: Req 23.1
    """
    es = _get_es()

    # Parse window into an ES range value (e.g. "5m" -> "now-5m")
    range_value = f"now-{window}"

    # Count events ingested in the window across shipment_events
    events_query = {
        "query": {"range": {"ingested_at": {"gte": range_value}}},
        "size": 0,
        "aggs": {
            "avg_latency": {
                "avg": {
                    "script": {
                        "source": (
                            "if (doc['ingested_at'].size() > 0 && doc['event_timestamp'].size() > 0) {"
                            "  return doc['ingested_at'].value.toInstant().toEpochMilli() "
                            "    - doc['event_timestamp'].value.toInstant().toEpochMilli();"
                            "} return 0;"
                        ),
                        "lang": "painless",
                    }
                }
            },
        },
    }

    events_result = es.client.search(
        index=OpsElasticsearchService.SHIPMENT_EVENTS, body=events_query
    )

    total_events = events_result["hits"]["total"]["value"]
    avg_latency_ms = events_result.get("aggregations", {}).get("avg_latency", {}).get("value")

    # Count poison queue entries in the window (failed events)
    poison_query = {
        "query": {"range": {"created_at": {"gte": range_value}}},
        "size": 0,
    }
    poison_result = es.client.search(
        index=OpsElasticsearchService.POISON_QUEUE, body=poison_query
    )
    failed_events = poison_result["hits"]["total"]["value"]

    return {
        "data": {
            "window": window,
            "events_received": total_events + failed_events,
            "events_processed": total_events,
            "events_failed": failed_events,
            "avg_processing_latency_ms": round(avg_latency_ms, 2) if avg_latency_ms is not None else None,
        },
        "request_id": _get_request_id(request),
    }


@router.get("/monitoring/indexing")
@limiter.limit(_ops_rate)
async def get_indexing_metrics(
    request: Request,
    window: str = Query("5m", description="Time window for metrics (e.g. 5m, 1h, 24h)"),
):
    """
    Indexing health: documents indexed, errors, bulk success rate, avg latency.
    Validates: Req 23.2
    """
    es = _get_es()

    range_value = f"now-{window}"

    # Count documents indexed across all ops indices in the window
    indices = [
        OpsElasticsearchService.SHIPMENTS_CURRENT,
        OpsElasticsearchService.SHIPMENT_EVENTS,
        OpsElasticsearchService.RIDERS_CURRENT,
    ]

    total_indexed = 0
    per_index: dict = {}
    for index_name in indices:
        count_query = {
            "query": {"range": {"ingested_at": {"gte": range_value}}},
            "size": 0,
            "aggs": {
                "avg_latency": {
                    "avg": {
                        "script": {
                            "source": (
                                "if (doc['ingested_at'].size() > 0 && doc['last_event_timestamp'].size() > 0) {"
                                "  return doc['ingested_at'].value.toInstant().toEpochMilli() "
                                "    - doc['last_event_timestamp'].value.toInstant().toEpochMilli();"
                                "} return 0;"
                            ),
                            "lang": "painless",
                        }
                    }
                },
            },
        }
        try:
            result = es.client.search(index=index_name, body=count_query)
            count = result["hits"]["total"]["value"]
            avg_lat = result.get("aggregations", {}).get("avg_latency", {}).get("value")
            total_indexed += count
            per_index[index_name] = {
                "documents_indexed": count,
                "avg_indexing_latency_ms": round(avg_lat, 2) if avg_lat is not None else None,
            }
        except Exception as exc:
            logger.warning("Failed to query index %s for monitoring: %s", index_name, exc)
            per_index[index_name] = {"documents_indexed": 0, "error": str(exc)}

    # Count indexing errors from poison queue
    poison_query = {
        "query": {
            "bool": {
                "must": [
                    {"range": {"created_at": {"gte": range_value}}},
                    {"term": {"error_type": "indexing_error"}},
                ]
            }
        },
        "size": 0,
    }
    try:
        poison_result = es.client.search(
            index=OpsElasticsearchService.POISON_QUEUE, body=poison_query
        )
        indexing_errors = poison_result["hits"]["total"]["value"]
    except Exception:
        indexing_errors = 0

    total_attempted = total_indexed + indexing_errors
    success_rate = round((total_indexed / total_attempted) * 100, 2) if total_attempted > 0 else 100.0

    return {
        "data": {
            "window": window,
            "total_documents_indexed": total_indexed,
            "indexing_errors": indexing_errors,
            "bulk_success_rate_pct": success_rate,
            "per_index": per_index,
        },
        "request_id": _get_request_id(request),
    }


@router.get("/monitoring/poison-queue")
@limiter.limit(_ops_rate)
async def get_poison_queue_metrics(
    request: Request,
):
    """
    Poison queue health: depth, oldest event age, retry stats.
    Validates: Req 23.3
    """
    es = _get_es()

    # Total queue depth (pending + retrying)
    depth_query = {
        "query": {
            "terms": {"status": ["pending", "retrying"]}
        },
        "size": 0,
        "aggs": {
            "oldest_event": {
                "min": {"field": "created_at"}
            },
            "by_status": {
                "terms": {"field": "status", "size": 10}
            },
            "avg_retry_count": {
                "avg": {"field": "retry_count"}
            },
            "max_retry_count": {
                "max": {"field": "retry_count"}
            },
            "by_error_type": {
                "terms": {"field": "error_type", "size": 20}
            },
        },
    }

    result = es.client.search(
        index=OpsElasticsearchService.POISON_QUEUE, body=depth_query
    )

    total_depth = result["hits"]["total"]["value"]

    # Calculate oldest event age in seconds
    oldest_ts = result.get("aggregations", {}).get("oldest_event", {}).get("value")
    oldest_age_seconds = None
    if oldest_ts is not None:
        oldest_age_seconds = round(
            (datetime.now(timezone.utc).timestamp() * 1000 - oldest_ts) / 1000, 1
        )

    # Status breakdown
    status_counts: dict = {}
    for sb in result.get("aggregations", {}).get("by_status", {}).get("buckets", []):
        status_counts[sb["key"]] = sb["doc_count"]

    # Error type breakdown
    error_type_counts: dict = {}
    for eb in result.get("aggregations", {}).get("by_error_type", {}).get("buckets", []):
        error_type_counts[eb["key"]] = eb["doc_count"]

    avg_retries = result.get("aggregations", {}).get("avg_retry_count", {}).get("value")
    max_retries = result.get("aggregations", {}).get("max_retry_count", {}).get("value")

    return {
        "data": {
            "queue_depth": total_depth,
            "oldest_event_age_seconds": oldest_age_seconds,
            "status_breakdown": status_counts,
            "error_type_breakdown": error_type_counts,
            "retry_stats": {
                "avg_retry_count": round(avg_retries, 2) if avg_retries is not None else None,
                "max_retry_count": int(max_retries) if max_retries is not None else None,
            },
        },
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Feature Flag Management Endpoints
# Validates: Requirements 27.1, 27.5
# ---------------------------------------------------------------------------


@router.post("/admin/feature-flags/{tenant_id}/enable")
@limiter.limit(_ops_rate)
async def enable_feature_flag(
    tenant_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Enable the Ops Intelligence Layer for a tenant.

    Validates: Req 27.1
    """
    if _feature_flag_service is None:
        raise HTTPException(status_code=503, detail="Feature flag service not configured")

    await _feature_flag_service.enable(tenant_id, tenant.user_id)

    return {
        "data": {"tenant_id": tenant_id, "status": "enabled"},
        "request_id": _get_request_id(request),
    }


@router.post("/admin/feature-flags/{tenant_id}/disable")
@limiter.limit(_ops_rate)
async def disable_feature_flag(
    tenant_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Disable the Ops Intelligence Layer for a tenant.

    Also disconnects any existing WebSocket clients for the tenant.

    Validates: Req 27.1
    """
    if _feature_flag_service is None:
        raise HTTPException(status_code=503, detail="Feature flag service not configured")

    await _feature_flag_service.disable(tenant_id, tenant.user_id)

    # Disconnect existing WebSocket clients for the disabled tenant
    from ops.websocket.ops_ws import get_ops_ws_manager

    ops_ws_manager = get_ops_ws_manager()
    disconnected = await ops_ws_manager.disconnect_tenant(tenant_id)
    logger.info(
        "Disabled tenant_id=%s: disconnected %d WebSocket clients",
        tenant_id,
        disconnected,
    )

    return {
        "data": {
            "tenant_id": tenant_id,
            "status": "disabled",
            "ws_clients_disconnected": disconnected,
        },
        "request_id": _get_request_id(request),
    }


@router.post("/admin/feature-flags/{tenant_id}/rollback")
@limiter.limit(_ops_rate)
async def rollback_feature_flag(
    tenant_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    purge_data: bool = Query(False, description="Purge tenant data from ops indices"),
) -> dict:
    """
    Rollback the Ops Intelligence Layer for a tenant.

    Disables the feature flag and optionally purges the tenant's data
    from all ops Elasticsearch indices.

    Validates: Req 27.5
    """
    if _feature_flag_service is None:
        raise HTTPException(status_code=503, detail="Feature flag service not configured")

    await _feature_flag_service.rollback(tenant_id, tenant.user_id, purge_data=purge_data)

    # Disconnect existing WebSocket clients for the rolled-back tenant
    from ops.websocket.ops_ws import get_ops_ws_manager

    ops_ws_manager = get_ops_ws_manager()
    disconnected = await ops_ws_manager.disconnect_tenant(tenant_id)
    logger.info(
        "Rolled back tenant_id=%s (purge_data=%s): disconnected %d WebSocket clients",
        tenant_id,
        purge_data,
        disconnected,
    )

    return {
        "data": {
            "tenant_id": tenant_id,
            "status": "rolled_back",
            "purge_data": purge_data,
            "ws_clients_disconnected": disconnected,
        },
        "request_id": _get_request_id(request),
    }


# Replay / Backfill Endpoints
# Validates: Requirements 3.1, 3.5
# ---------------------------------------------------------------------------


class ReplayTriggerRequest(BaseModel):
    """Request body for triggering a replay backfill job."""

    tenant_id: str = Field(..., description="Tenant to backfill")
    start_time: str = Field(..., description="Start of time range (ISO 8601)")
    end_time: str = Field(..., description="End of time range (ISO 8601)")


@router.post("/replay/trigger")
@limiter.limit(_ops_rate)
async def trigger_replay(
    body: ReplayTriggerRequest,
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
) -> dict:
    """
    Trigger a backfill job for a specified tenant and time range.

    The job runs in the background. Use GET /ops/replay/status/{job_id}
    to poll progress.

    Validates: Req 3.1
    """
    replay_svc = get_replay_service()
    if replay_svc is None:
        raise HTTPException(
            status_code=503,
            detail="Replay service not configured",
        )

    # Parse and validate time range
    try:
        start_dt = datetime.fromisoformat(body.start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(body.end_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise validation_error(
            message="start_time and end_time must be valid ISO 8601 date strings",
            details={"start_time": body.start_time, "end_time": body.end_time},
        )

    if end_dt <= start_dt:
        raise validation_error(
            message="end_time must be after start_time",
            details={"start_time": body.start_time, "end_time": body.end_time},
        )

    # Use the tenant_id from the JWT-verified context for security,
    # but allow the body tenant_id if the caller is the same tenant
    effective_tenant = tenant.tenant_id

    job = await replay_svc.trigger_backfill(
        tenant_id=effective_tenant,
        start_time=start_dt,
        end_time=end_dt,
    )

    return {
        "data": job.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


@router.get("/replay/status/{job_id}")
@limiter.limit(_ops_rate)
async def get_replay_status(
    job_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
) -> dict:
    """
    Get the progress of a backfill job.

    Validates: Req 3.5
    """
    replay_svc = get_replay_service()
    if replay_svc is None:
        raise HTTPException(
            status_code=503,
            detail="Replay service not configured",
        )

    job = await replay_svc.get_job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Replay job not found")

    # Ensure the caller can only see their own tenant's jobs
    if job.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Replay job not found")

    return {
        "data": job.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }

# ---------------------------------------------------------------------------
# Drift Detection Endpoints
# Validates: Requirements 25.4, 25.5
# ---------------------------------------------------------------------------


class DriftRunRequest(BaseModel):
    """Request body for triggering a drift detection run."""

    tenant_id: str = Field(..., description="Tenant to check for drift")
    start_time: Optional[str] = Field(
        None, description="Start of time range (ISO 8601). Defaults to last 24 hours."
    )
    end_time: Optional[str] = Field(
        None, description="End of time range (ISO 8601). Defaults to now."
    )


@router.post("/drift/run")
@limiter.limit(_ops_rate)
async def run_drift_detection(
    body: DriftRunRequest,
    request: Request,
    tenant: TenantContext = Depends(require_ops_enabled),
) -> dict:
    """
    Trigger a drift detection run for a tenant and optional time range.

    Compares Dinee source state against the Runsheet read-model
    (Elasticsearch) and returns divergence results.

    Validates: Req 25.4
    """
    detector = get_drift_detector()

    # Parse optional time range
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None

    if body.start_time is not None:
        try:
            start_dt = datetime.fromisoformat(body.start_time.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            raise validation_error(
                message="start_time must be a valid ISO 8601 date string",
                details={"start_time": body.start_time},
            )

    if body.end_time is not None:
        try:
            end_dt = datetime.fromisoformat(body.end_time.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            raise validation_error(
                message="end_time must be a valid ISO 8601 date string",
                details={"end_time": body.end_time},
            )

    if start_dt and end_dt and end_dt <= start_dt:
        raise validation_error(
            message="end_time must be after start_time",
            details={"start_time": body.start_time, "end_time": body.end_time},
        )

    # Use the tenant_id from the JWT-verified context for security
    effective_tenant = tenant.tenant_id

    result = await detector.run_detection(
        tenant_id=effective_tenant,
        start_time=start_dt,
        end_time=end_dt,
    )

    return {
        "data": result.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }
