"""
Scheduling API endpoints for the Logistics Scheduling & Dispatch module.

Provides REST endpoints for job lifecycle management, cargo tracking,
ETA queries, and scheduling metrics under the /scheduling prefix.

All endpoints are rate-limited and tenant-scoped via JWT.

Validates: Requirements 2.1, 3.1-3.6, 4.1-4.8, 5.1-5.7, 6.1-6.6,
           7.2, 7.5, 8.1-8.5, 13.1-13.5, 15.2
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from config.settings import get_settings
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from scheduling.models import (
    AssignAsset,
    CreateJob,
    StatusTransition,
    UpdateCargoItemStatus,
    UpdateCargoManifest,
)
from scheduling.services.cargo_service import CargoService
from scheduling.services.delay_detection_service import DelayDetectionService
from scheduling.services.job_service import JobService
from scheduling.services.scheduling_es_mappings import JOBS_CURRENT_INDEX

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_scheduling_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service references, wired via configure_scheduling_api()
_job_service: Optional[JobService] = None
_cargo_service: Optional[CargoService] = None
_delay_service: Optional[DelayDetectionService] = None

router = APIRouter(prefix="/api/scheduling", tags=["scheduling"])

# Auth policy declaration for this router (Req 5.2)
# Default: JWT_REQUIRED for all scheduling endpoints
ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_scheduling_api(
    *,
    job_service: JobService,
    cargo_service: CargoService,
    delay_service: DelayDetectionService,
) -> None:
    """
    Wire service dependencies into the scheduling API module.

    Called once during application startup (from main.py) so that the
    router handlers can access the shared services.
    """
    global _job_service, _cargo_service, _delay_service
    _job_service = job_service
    _cargo_service = cargo_service
    _delay_service = delay_service


def _get_job_service() -> JobService:
    """Return the configured JobService or raise."""
    if _job_service is None:
        raise RuntimeError(
            "Scheduling API not configured. Call configure_scheduling_api() during startup."
        )
    return _job_service


def _get_cargo_service() -> CargoService:
    """Return the configured CargoService or raise."""
    if _cargo_service is None:
        raise RuntimeError(
            "Scheduling API not configured. Call configure_scheduling_api() during startup."
        )
    return _cargo_service


def _get_delay_service() -> DelayDetectionService:
    """Return the configured DelayDetectionService or raise."""
    if _delay_service is None:
        raise RuntimeError(
            "Scheduling API not configured. Call configure_scheduling_api() during startup."
        )
    return _delay_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_BUCKETS = {"hourly", "daily"}


def _validate_bucket(bucket: str) -> None:
    """Raise 400 if bucket is not hourly or daily."""
    if bucket not in VALID_BUCKETS:
        from errors.exceptions import validation_error

        raise validation_error(
            f"Invalid bucket: '{bucket}'",
            details={"bucket": bucket, "valid_values": list(VALID_BUCKETS)},
        )


def _validate_date(value: Optional[str], field_name: str) -> None:
    """Raise 400 if the date string is not valid ISO 8601."""
    if value is None:
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        from errors.exceptions import validation_error

        raise validation_error(
            f"Invalid {field_name}: '{value}'. Expected ISO 8601 format.",
            details={field_name: value},
        )


def _resolve_bucket(
    bucket: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> str:
    """
    Return the effective bucket granularity.

    If the requested time range exceeds 90 days, force daily granularity
    regardless of the caller's preference (Req 13.4).
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
    field: str = "scheduled_time",
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


# ---------------------------------------------------------------------------
# Job CRUD endpoints
# Validates: Requirements 2.1, 5.1-5.7, 15.2
# ---------------------------------------------------------------------------


@router.post("/jobs", status_code=201)
@limiter.limit(_scheduling_rate)
async def create_job(
    data: CreateJob,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Create a new logistics job.

    Validates: Requirements 2.1-2.8
    """
    svc = _get_job_service()
    job = await svc.create_job(data, tenant.tenant_id, actor_id=tenant.user_id)
    return {
        "data": job,
        "request_id": _get_request_id(request),
    }


@router.get("/jobs")
@limiter.limit(_scheduling_rate)
async def list_jobs(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    job_type: Optional[str] = Query(None, description="Filter by job type"),
    status: Optional[str] = Query(None, description="Filter by job status"),
    asset_assigned: Optional[str] = Query(None, description="Filter by assigned asset"),
    origin: Optional[str] = Query(None, description="Filter by origin"),
    destination: Optional[str] = Query(None, description="Filter by destination"),
    start_date: Optional[str] = Query(None, description="Start of date range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of date range (ISO 8601)"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
    sort_by: str = Query("scheduled_time", description="Field to sort by"),
    sort_order: str = Query("asc", description="Sort order: asc or desc"),
) -> dict:
    """
    List jobs with filters, pagination, and sorting.

    Validates: Requirements 5.1, 5.2, 5.6, 5.7
    """
    svc = _get_job_service()
    result = await svc.list_jobs(
        tenant_id=tenant.tenant_id,
        job_type=job_type,
        status=status,
        asset_assigned=asset_assigned,
        origin=origin,
        destination=destination,
        start_date=start_date,
        end_date=end_date,
        page=page,
        size=size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    # Dual-field deprecation: add unified PaginatedResponse fields
    from schemas.common import paginated_response_dict

    return paginated_response_dict(
        items=result["data"],
        total=result["pagination"]["total"],
        page=result["pagination"]["page"],
        page_size=result["pagination"]["size"],
        request_id=_get_request_id(request),
    )


@router.get("/jobs/active")
@limiter.limit(_scheduling_rate)
async def get_active_jobs(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Get all active jobs (scheduled, assigned, in_progress).

    Validates: Requirement 5.4
    """
    svc = _get_job_service()
    jobs = await svc.get_active_jobs(tenant.tenant_id)
    from schemas.common import paginated_response_dict

    return paginated_response_dict(
        items=jobs,
        total=len(jobs),
        page=1,
        page_size=len(jobs) if jobs else 1,
        request_id=_get_request_id(request),
    )


@router.get("/jobs/delayed")
@limiter.limit(_scheduling_rate)
async def get_delayed_jobs(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Get delayed jobs (in_progress past estimated_arrival).

    Validates: Requirement 5.5
    """
    svc = _get_job_service()
    jobs = await svc.get_delayed_jobs(tenant.tenant_id)
    from schemas.common import paginated_response_dict

    return paginated_response_dict(
        items=jobs,
        total=len(jobs),
        page=1,
        page_size=len(jobs) if jobs else 1,
        request_id=_get_request_id(request),
    )


@router.get("/jobs/{job_id}")
@limiter.limit(_scheduling_rate)
async def get_job(
    job_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Get a single job with its event history.

    Validates: Requirement 5.3
    """
    svc = _get_job_service()
    job = await svc.get_job(job_id, tenant.tenant_id)
    return {
        "data": job,
        "request_id": _get_request_id(request),
    }


@router.get("/jobs/{job_id}/events")
@limiter.limit(_scheduling_rate)
async def get_job_events(
    job_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Get the complete event timeline for a job.

    Validates: Requirement 15.2
    """
    svc = _get_job_service()
    events = await svc.get_job_events(job_id, tenant.tenant_id)
    from schemas.common import paginated_response_dict

    return paginated_response_dict(
        items=events,
        total=len(events),
        page=1,
        page_size=len(events) if events else 1,
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# Assignment endpoints
# Validates: Requirements 3.1-3.6
# ---------------------------------------------------------------------------


@router.patch("/jobs/{job_id}/assign")
@limiter.limit(_scheduling_rate)
async def assign_asset(
    job_id: str,
    data: AssignAsset,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Assign an asset to a scheduled job.

    Validates: Requirements 3.1-3.5
    """
    svc = _get_job_service()
    job = await svc.assign_asset(
        job_id, data.asset_id, tenant.tenant_id, actor_id=tenant.user_id
    )
    return {
        "data": job,
        "request_id": _get_request_id(request),
    }


@router.patch("/jobs/{job_id}/reassign")
@limiter.limit(_scheduling_rate)
async def reassign_asset(
    job_id: str,
    data: AssignAsset,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Reassign a different asset to a job.

    Validates: Requirement 3.6
    """
    svc = _get_job_service()
    job = await svc.reassign_asset(
        job_id, data.asset_id, tenant.tenant_id, actor_id=tenant.user_id
    )
    return {
        "data": job,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Status transition endpoint
# Validates: Requirements 4.1-4.8
# ---------------------------------------------------------------------------


@router.patch("/jobs/{job_id}/status")
@limiter.limit(_scheduling_rate)
async def transition_status(
    job_id: str,
    data: StatusTransition,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Transition a job to a new status.

    Validates: Requirements 4.1-4.8
    """
    svc = _get_job_service()
    job = await svc.transition_status(
        job_id, data, tenant.tenant_id, actor_id=tenant.user_id
    )
    return {
        "data": job,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Cargo endpoints
# Validates: Requirements 6.1-6.6
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/cargo")
@limiter.limit(_scheduling_rate)
async def get_cargo(
    job_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Get the cargo manifest for a job.

    Validates: Requirement 6.1
    """
    svc = _get_cargo_service()
    manifest = await svc.get_cargo_manifest(job_id, tenant.tenant_id)
    return {
        "data": manifest,
        "request_id": _get_request_id(request),
    }


@router.patch("/jobs/{job_id}/cargo")
@limiter.limit(_scheduling_rate)
async def update_cargo(
    job_id: str,
    data: UpdateCargoManifest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Update the cargo manifest for a job.

    Validates: Requirement 6.2
    """
    svc = _get_cargo_service()
    manifest = await svc.update_cargo_manifest(
        job_id, data.items, tenant.tenant_id, actor_id=tenant.user_id
    )
    return {
        "data": manifest,
        "request_id": _get_request_id(request),
    }


@router.patch("/jobs/{job_id}/cargo/{item_id}/status")
@limiter.limit(_scheduling_rate)
async def update_cargo_item_status(
    job_id: str,
    item_id: str,
    data: UpdateCargoItemStatus,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Update the status of a single cargo item.

    Validates: Requirements 6.3, 6.4
    """
    svc = _get_cargo_service()
    item = await svc.update_cargo_item_status(
        job_id, item_id, data.item_status, tenant.tenant_id, actor_id=tenant.user_id
    )
    return {
        "data": item,
        "request_id": _get_request_id(request),
    }


@router.get("/cargo/search")
@limiter.limit(_scheduling_rate)
async def search_cargo(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    container_number: Optional[str] = Query(None, description="Filter by container number"),
    description: Optional[str] = Query(None, description="Search by description"),
    item_status: Optional[str] = Query(None, description="Filter by item status"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
) -> dict:
    """
    Search cargo items across all jobs.

    Validates: Requirement 6.5
    """
    svc = _get_cargo_service()
    result = await svc.search_cargo(
        tenant_id=tenant.tenant_id,
        container_number=container_number,
        description=description,
        item_status=item_status,
        page=page,
        size=size,
    )
    # Dual-field deprecation: add unified PaginatedResponse fields
    from schemas.common import paginated_response_dict

    return paginated_response_dict(
        items=result.get("data", []),
        total=result.get("pagination", {}).get("total", 0),
        page=result.get("pagination", {}).get("page", page),
        page_size=result.get("pagination", {}).get("size", size),
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# ETA endpoint
# Validates: Requirement 7.2
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/eta")
@limiter.limit(_scheduling_rate)
async def get_eta(
    job_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Get the current ETA for a job.

    Validates: Requirement 7.2
    """
    svc = _get_delay_service()
    eta = await svc.get_eta(job_id, tenant.tenant_id)
    return {
        "data": eta,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Metrics endpoints
# Validates: Requirements 7.5, 13.1-13.5
# ---------------------------------------------------------------------------


@router.get("/metrics/jobs")
@limiter.limit(_scheduling_rate)
async def get_job_metrics(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    bucket: str = Query("hourly", description="Bucket granularity: hourly or daily"),
    start_date: Optional[str] = Query(None, description="Start of time range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of time range (ISO 8601)"),
) -> dict:
    """
    Job counts aggregated by status and job_type in time buckets.

    Uses ES date_histogram aggregation with the bucket parameter.

    Validates: Requirements 13.1, 13.4, 13.5
    """
    _validate_bucket(bucket)
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")

    svc = _get_job_service()
    es = svc._es

    effective_bucket = _resolve_bucket(bucket, start_date, end_date)
    interval = _bucket_interval(effective_bucket)

    # Build query with tenant filter
    must_clauses: list[dict] = [
        {"term": {"tenant_id": tenant.tenant_id}},
    ]
    dr = _build_date_range_filter(start_date, end_date)
    if dr:
        must_clauses.append(dr)

    query: dict = {
        "query": {"bool": {"must": must_clauses}},
        "size": 0,
        "aggs": {
            "over_time": {
                "date_histogram": {
                    "field": "scheduled_time",
                    "calendar_interval": interval,
                },
                "aggs": {
                    "by_status": {
                        "terms": {"field": "status", "size": 20},
                    },
                    "by_type": {
                        "terms": {"field": "job_type", "size": 20},
                    },
                },
            },
        },
    }

    result = await es.search_documents(JOBS_CURRENT_INDEX, query, size=0)

    buckets_data: list[dict] = []
    for b in result.get("aggregations", {}).get("over_time", {}).get("buckets", []):
        counts_by_status: dict[str, int] = {}
        for sb in b.get("by_status", {}).get("buckets", []):
            counts_by_status[sb["key"]] = sb["doc_count"]

        counts_by_type: dict[str, int] = {}
        for tb in b.get("by_type", {}).get("buckets", []):
            counts_by_type[tb["key"]] = tb["doc_count"]

        buckets_data.append({
            "timestamp": b["key_as_string"],
            "total": b["doc_count"],
            "counts_by_status": counts_by_status,
            "counts_by_type": counts_by_type,
        })

    return {
        "data": buckets_data,
        "bucket": effective_bucket,
        "start_date": start_date,
        "end_date": end_date,
        "request_id": _get_request_id(request),
    }


@router.get("/metrics/completion")
@limiter.limit(_scheduling_rate)
async def get_completion_metrics(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    start_date: Optional[str] = Query(None, description="Start of time range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of time range (ISO 8601)"),
) -> dict:
    """
    Completion rate and average completion time grouped by job_type.

    Uses ES terms aggregation on job_type with sub-aggregations.

    Validates: Requirement 13.2
    """
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")

    svc = _get_job_service()
    es = svc._es

    must_clauses: list[dict] = [
        {"term": {"tenant_id": tenant.tenant_id}},
    ]
    dr = _build_date_range_filter(start_date, end_date)
    if dr:
        must_clauses.append(dr)

    query: dict = {
        "query": {"bool": {"must": must_clauses}},
        "size": 0,
        "aggs": {
            "by_job_type": {
                "terms": {"field": "job_type", "size": 20},
                "aggs": {
                    "completed_count": {
                        "filter": {"term": {"status": "completed"}},
                    },
                },
            },
        },
    }

    result = await es.search_documents(JOBS_CURRENT_INDEX, query, size=0)

    # Collect job_types and their counts from the aggregation
    type_stats: dict[str, dict] = {}
    for b in result.get("aggregations", {}).get("by_job_type", {}).get("buckets", []):
        total = b["doc_count"]
        completed = b.get("completed_count", {}).get("doc_count", 0)
        type_stats[b["key"]] = {"total": total, "completed": completed}

    # Fetch completed jobs with both started_at and completed_at to compute
    # average completion time in Python (avoids scripted_metric which is
    # blocked on serverless ES clusters).
    completed_clauses = list(must_clauses) + [
        {"term": {"status": "completed"}},
        {"exists": {"field": "started_at"}},
        {"exists": {"field": "completed_at"}},
    ]
    completed_query: dict = {
        "query": {"bool": {"must": completed_clauses}},
        "_source": ["job_type", "started_at", "completed_at"],
        "size": 1000,
    }
    completed_result = await es.search_documents(JOBS_CURRENT_INDEX, completed_query, size=1000)

    # Accumulate durations per job_type
    durations_by_type: dict[str, list[float]] = {}
    for hit in completed_result.get("hits", {}).get("hits", []):
        src = hit["_source"]
        jt = src.get("job_type", "unknown")
        try:
            start_dt = datetime.fromisoformat(src["started_at"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(src["completed_at"].replace("Z", "+00:00"))
            minutes = (end_dt - start_dt).total_seconds() / 60.0
            durations_by_type.setdefault(jt, []).append(minutes)
        except (ValueError, TypeError, KeyError):
            pass

    metrics: list[dict] = []
    for job_type, stats in type_stats.items():
        total = stats["total"]
        completed = stats["completed"]
        completion_rate = round((completed / total) * 100, 2) if total > 0 else 0.0

        durs = durations_by_type.get(job_type, [])
        avg_minutes = round(sum(durs) / len(durs), 2) if durs else 0.0

        metrics.append({
            "job_type": job_type,
            "total": total,
            "completed": completed,
            "completion_rate": completion_rate,
            "avg_completion_minutes": avg_minutes,
        })

    return {
        "data": metrics,
        "request_id": _get_request_id(request),
    }


@router.get("/metrics/assets")
@limiter.limit(_scheduling_rate)
async def get_asset_utilization(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    start_date: Optional[str] = Query(None, description="Start of time range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of time range (ISO 8601)"),
) -> dict:
    """
    Asset utilization metrics: jobs per asset, idle time.

    Uses ES terms aggregation on asset_assigned with sub-aggregations.

    Validates: Requirement 13.3
    """
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")

    svc = _get_job_service()
    es = svc._es

    must_clauses: list[dict] = [
        {"term": {"tenant_id": tenant.tenant_id}},
        {"exists": {"field": "asset_assigned"}},
    ]
    dr = _build_date_range_filter(start_date, end_date)
    if dr:
        must_clauses.append(dr)

    query: dict = {
        "query": {"bool": {"must": must_clauses}},
        "size": 0,
        "aggs": {
            "by_asset": {
                "terms": {"field": "asset_assigned", "size": 200},
                "aggs": {
                    "active_jobs": {
                        "filter": {
                            "terms": {"status": ["assigned", "in_progress"]},
                        },
                    },
                    "completed_jobs": {
                        "filter": {"term": {"status": "completed"}},
                    },
                },
            },
        },
    }

    result = await es.search_documents(JOBS_CURRENT_INDEX, query, size=0)

    # Collect asset stats from aggregation
    asset_stats: dict[str, dict] = {}
    for b in result.get("aggregations", {}).get("by_asset", {}).get("buckets", []):
        asset_stats[b["key"]] = {
            "total_jobs": b["doc_count"],
            "active": b.get("active_jobs", {}).get("doc_count", 0),
            "completed": b.get("completed_jobs", {}).get("doc_count", 0),
        }

    # Fetch jobs with started_at to compute active hours in Python
    # (avoids scripted_metric which is blocked on serverless ES clusters).
    hours_clauses = list(must_clauses) + [
        {"exists": {"field": "started_at"}},
    ]
    hours_query: dict = {
        "query": {"bool": {"must": hours_clauses}},
        "_source": ["asset_assigned", "started_at", "completed_at"],
        "size": 2000,
    }
    hours_result = await es.search_documents(JOBS_CURRENT_INDEX, hours_query, size=2000)

    hours_by_asset: dict[str, float] = {}
    for hit in hours_result.get("hits", {}).get("hits", []):
        src = hit["_source"]
        asset_id = src.get("asset_assigned")
        if not asset_id:
            continue
        try:
            start_dt = datetime.fromisoformat(src["started_at"].replace("Z", "+00:00"))
            if src.get("completed_at"):
                end_dt = datetime.fromisoformat(src["completed_at"].replace("Z", "+00:00"))
            else:
                end_dt = datetime.now(start_dt.tzinfo)
            hours = (end_dt - start_dt).total_seconds() / 3600.0
            hours_by_asset[asset_id] = hours_by_asset.get(asset_id, 0.0) + hours
        except (ValueError, TypeError, KeyError):
            pass

    # Calculate total time range for idle time computation
    total_range_hours = 0.0
    if start_date and end_date:
        try:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            total_range_hours = (end_dt - start_dt).total_seconds() / 3600.0
        except (ValueError, TypeError):
            pass

    metrics: list[dict] = []
    for asset_id, stats in asset_stats.items():
        active_hrs = round(hours_by_asset.get(asset_id, 0.0), 2)
        idle_hrs = round(max(total_range_hours - active_hrs, 0.0), 2) if total_range_hours > 0 else 0.0

        metrics.append({
            "asset_id": asset_id,
            "total_jobs": stats["total_jobs"],
            "active_jobs": stats["active"],
            "completed_jobs": stats["completed"],
            "total_active_hours": active_hrs,
            "idle_hours": idle_hrs,
        })

    return {
        "data": metrics,
        "request_id": _get_request_id(request),
    }


@router.get("/metrics/delays")
@limiter.limit(_scheduling_rate)
async def get_delay_metrics(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    start_date: Optional[str] = Query(None, description="Start of time range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of time range (ISO 8601)"),
) -> dict:
    """
    Delay statistics: count, average duration, delays by job_type.

    Validates: Requirement 7.5
    """
    _validate_date(start_date, "start_date")
    _validate_date(end_date, "end_date")

    svc = _get_delay_service()
    metrics = await svc.get_delay_metrics(
        tenant_id=tenant.tenant_id,
        start_date=start_date,
        end_date=end_date,
    )
    return {
        "data": metrics,
        "request_id": _get_request_id(request),
    }
