"""
REST API endpoints for the Fuel Distribution MVP pipeline.

Provides endpoints for triggering pipeline runs, retrieving plans,
initiating replanning, and querying forecasts and priorities.

Uses a ``configure_mvp_endpoints()`` function to wire service
dependencies at startup (same pattern as agent_endpoints.py).

Validates: Requirements 8.1–8.6
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service references, wired via configure_mvp_endpoints()
# ---------------------------------------------------------------------------

_pipeline = None
_es_service = None
_exception_replanning_agent = None

router = APIRouter(prefix="/api/fuel/mvp", tags=["fuel-mvp"])

# Auth policy: JWT_REQUIRED for all MVP endpoints (Req 8.6)
ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class GeneratePlanResponse(BaseModel):
    """Response for POST /plan/generate."""
    run_id: str
    status: str


class ReplanRequest(BaseModel):
    """Body for POST /plan/{plan_id}/replan."""
    disruption_type: str = Field(
        default="delay",
        description="Type of disruption: truck_breakdown, station_outage, demand_spike, delay",
    )
    description: str = Field(
        default="",
        description="Human-readable description of the disruption",
    )
    entity_id: str = Field(
        default="",
        description="ID of the affected entity (truck_id, station_id, etc.)",
    )


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_mvp_endpoints(
    *,
    pipeline,
    es_service,
    exception_replanning_agent=None,
) -> None:
    """Wire service dependencies into the MVP endpoints module.

    Called once during application startup so that the router handlers
    can access shared services without circular imports.
    """
    global _pipeline, _es_service, _exception_replanning_agent
    _pipeline = pipeline
    _es_service = es_service
    _exception_replanning_agent = exception_replanning_agent


# ---------------------------------------------------------------------------
# Service accessors
# ---------------------------------------------------------------------------


def _get_pipeline():
    if _pipeline is None:
        raise RuntimeError(
            "MVP endpoints not configured. "
            "Call configure_mvp_endpoints() during startup."
        )
    return _pipeline


def _get_es():
    if _es_service is None:
        raise RuntimeError(
            "MVP endpoints not configured. "
            "Call configure_mvp_endpoints() during startup."
        )
    return _es_service


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/plan/generate (Req 8.1)
# ---------------------------------------------------------------------------


@router.post("/plan/generate")
async def generate_plan(
    request: Request,
    tenant_id: str = Query(..., description="Tenant identifier"),
):
    """Trigger a full pipeline run, returning run_id and status.

    Validates: Requirement 8.1
    """
    pipeline = _get_pipeline()
    try:
        run_id = await pipeline.run(tenant_id=tenant_id)
        status_info = await pipeline.get_status(run_id)
        return GeneratePlanResponse(
            run_id=run_id,
            status=status_info["state"] if status_info else "pending",
        )
    except Exception as e:
        logger.error("Failed to generate plan: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/plan/{plan_id} (Req 8.2)
# ---------------------------------------------------------------------------


@router.get("/plan/{plan_id}")
async def get_plan(
    plan_id: str,
    request: Request,
    tenant_id: str = Query(..., description="Tenant identifier"),
):
    """Retrieve a complete plan (loading + route) by plan_id.

    Validates: Requirement 8.2
    """
    es = _get_es()

    # Query loading plan
    loading_query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"plan_id": plan_id}},
                ],
            },
        },
        "size": 1,
    }

    loading_plan = None
    try:
        resp = await es.search_documents("mvp_load_plans", loading_query, 1)
        hits = resp.get("hits", {}).get("hits", [])
        if hits:
            loading_plan = hits[0]["_source"]
    except Exception as e:
        logger.error("Failed to query loading plan %s: %s", plan_id, e)

    if loading_plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")

    # Query associated route plan
    route_query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"plan_id": plan_id}},
                ],
            },
        },
        "size": 1,
    }

    route_plan = None
    try:
        resp = await es.search_documents("mvp_routes", route_query, 1)
        hits = resp.get("hits", {}).get("hits", [])
        if hits:
            route_plan = hits[0]["_source"]
    except Exception as e:
        logger.error("Failed to query route plan for %s: %s", plan_id, e)

    return {
        "plan_id": plan_id,
        "loading_plan": loading_plan,
        "route_plan": route_plan,
    }


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/plan/{plan_id}/replan (Req 8.3)
# ---------------------------------------------------------------------------


@router.post("/plan/{plan_id}/replan")
async def replan(
    plan_id: str,
    request: Request,
    body: ReplanRequest = None,
    tenant_id: str = Query(..., description="Tenant identifier"),
):
    """Trigger exception replanning for an existing plan.

    Validates: Requirement 8.3
    """
    if body is None:
        body = ReplanRequest()

    if _exception_replanning_agent is None:
        raise HTTPException(
            status_code=503,
            detail="Exception replanning agent not available",
        )

    try:
        # Trigger the replanning agent's evaluation cycle
        from Agents.overlay.data_contracts import RiskSignal, Severity

        signal = RiskSignal(
            source_agent="mvp_api",
            entity_id=body.entity_id or plan_id,
            entity_type=body.disruption_type,
            severity=Severity.HIGH,
            confidence=0.9,
            ttl_seconds=3600,
            tenant_id=tenant_id,
            context={
                "disruption_type": body.disruption_type,
                "description": body.description,
                "plan_id": plan_id,
            },
        )

        # Feed the signal to the replanning agent
        await _exception_replanning_agent._on_signal(signal)
        await _exception_replanning_agent.monitor_cycle()

        return {
            "plan_id": plan_id,
            "status": "replan_triggered",
            "disruption_type": body.disruption_type,
        }
    except Exception as e:
        logger.error("Failed to trigger replan for %s: %s", plan_id, e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/forecasts (Req 8.4)
# ---------------------------------------------------------------------------


@router.get("/forecasts")
async def get_forecasts(
    request: Request,
    tenant_id: str = Query(..., description="Tenant identifier"),
    station_id: Optional[str] = Query(None, description="Filter by station ID"),
    fuel_grade: Optional[str] = Query(None, description="Filter by fuel grade"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """Retrieve the latest tank forecasts with optional filters.

    Validates: Requirement 8.4
    """
    es = _get_es()

    must_clauses = [{"term": {"tenant_id": tenant_id}}]
    if station_id:
        must_clauses.append({"term": {"station_id": station_id}})
    if fuel_grade:
        must_clauses.append({"term": {"fuel_grade": fuel_grade}})

    query = {
        "query": {"bool": {"must": must_clauses}},
        "sort": [{"timestamp": {"order": "desc"}}],
        "from": (page - 1) * size,
        "size": size,
    }

    try:
        resp = await es.search_documents("mvp_tank_forecasts", query, size)
        hits = resp.get("hits", {}).get("hits", [])
        total = resp.get("hits", {}).get("total", {})
        total_count = total.get("value", 0) if isinstance(total, dict) else total

        items = [hit["_source"] for hit in hits]

        from schemas.common import paginated_response_dict
        return paginated_response_dict(
            items=items,
            total=total_count,
            page=page,
            page_size=size,
        )
    except Exception as e:
        logger.error("Failed to query forecasts: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/priorities (Req 8.5)
# ---------------------------------------------------------------------------


@router.get("/priorities")
async def get_priorities(
    request: Request,
    tenant_id: str = Query(..., description="Tenant identifier"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """Retrieve the latest delivery priority rankings.

    Validates: Requirement 8.5
    """
    es = _get_es()

    query = {
        "query": {"bool": {"must": [{"term": {"tenant_id": tenant_id}}]}},
        "sort": [{"timestamp": {"order": "desc"}}],
        "from": (page - 1) * size,
        "size": size,
    }

    try:
        resp = await es.search_documents("mvp_delivery_priorities", query, size)
        hits = resp.get("hits", {}).get("hits", [])
        total = resp.get("hits", {}).get("total", {})
        total_count = total.get("value", 0) if isinstance(total, dict) else total

        items = [hit["_source"] for hit in hits]

        from schemas.common import paginated_response_dict
        return paginated_response_dict(
            items=items,
            total=total_count,
            page=page,
            page_size=size,
        )
    except Exception as e:
        logger.error("Failed to query priorities: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
