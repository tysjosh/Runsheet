"""
REST API endpoints for the Fuel Distribution MVP pipeline.

Provides endpoints for triggering pipeline runs, retrieving plans,
initiating replanning, querying forecasts and priorities, and
configuring truck compartments.

Uses a ``configure_mvp_endpoints()`` function to wire service
dependencies at startup (same pattern as agent_endpoints.py).

Validates: Requirements 6.1, 6.3, 8.1–8.6
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service references, wired via configure_mvp_endpoints()
# ---------------------------------------------------------------------------

_pipeline = None
_es_service = None
_exception_replanning_agent = None
_fleet_registration_service = None

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


class CompartmentConfig(BaseModel):
    """A single compartment configuration entry."""
    compartment_id: str = Field(..., description="Unique compartment identifier")
    capacity_liters: float = Field(..., ge=0, description="Compartment capacity in liters")
    allowed_grades: List[str] = Field(default_factory=list, description="Allowed fuel grades")
    position_index: int = Field(default=0, ge=0, description="Position index on the truck")


class CompartmentConfigRequest(BaseModel):
    """Body for PUT /compartments/{truck_id}."""
    compartments: List[CompartmentConfig] = Field(
        ..., description="List of compartment configurations for the truck"
    )


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_mvp_endpoints(
    *,
    pipeline,
    es_service,
    exception_replanning_agent=None,
    fleet_registration_service=None,
) -> None:
    """Wire service dependencies into the MVP endpoints module.

    Called once during application startup so that the router handlers
    can access shared services without circular imports.
    """
    global _pipeline, _es_service, _exception_replanning_agent, _fleet_registration_service
    _pipeline = pipeline
    _es_service = es_service
    _exception_replanning_agent = exception_replanning_agent
    _fleet_registration_service = fleet_registration_service


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
    """Retrieve a complete plan (loading + route) by plan_id or run_id.

    Validates: Requirement 8.2
    """
    es = _get_es()

    # Try plan_id first, then fall back to run_id (the generate endpoint
    # returns run_id, which is what the frontend passes here).
    loading_plan = None
    for field in ("plan_id", "run_id"):
        loading_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {field: plan_id}},
                    ],
                },
            },
            "size": 1,
        }
        try:
            resp = await es.search_documents("mvp_load_plans", loading_query, 1)
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                loading_plan = hits[0]["_source"]
                break
        except Exception as e:
            logger.error("Failed to query loading plan %s (field=%s): %s", plan_id, field, e)

    if loading_plan is None:
        # No loading plan found — could be a run_id for a pipeline that
        # completed without producing plans (e.g. no truck compartments
        # configured). Return an empty plan instead of 404.
        return {
            "plan_id": plan_id,
            "loading_plan": None,
            "route_plan": None,
        }

    # Query associated route plan(s) using the same fallback strategy
    route_plan = None
    for field in ("plan_id", "run_id"):
        route_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {field: plan_id}},
                    ],
                },
            },
            "size": 20,
        }
        try:
            resp = await es.search_documents("mvp_routes", route_query, 20)
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                routes = [hit["_source"] for hit in hits]
                route_plan = {
                    "plan_id": plan_id,
                    "tenant_id": tenant_id,
                    "routes": routes,
                    "timestamp": routes[0].get("timestamp", ""),
                }
                break
        except Exception as e:
            logger.error("Failed to query route plan for %s (field=%s): %s", plan_id, field, e)

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

# ---------------------------------------------------------------------------
# PUT /api/fuel/mvp/compartments/{truck_id} (Req 6.1, 6.3)
# ---------------------------------------------------------------------------


@router.put("/compartments/{truck_id}")
async def configure_compartments(
    truck_id: str,
    body: CompartmentConfigRequest,
    request: Request,
    tenant_id: str = Query(..., description="Tenant identifier"),
):
    """Configure compartments for a fuel tanker.

    Writes compartment documents to the truck_compartments index and
    ensures the truck is registered in the fleet (trucks) index.

    Validates: Requirements 6.1, 6.3
    """
    es = _get_es()

    from Agents.support.mvp_es_mappings import TRUCK_COMPARTMENTS_INDEX

    try:
        # Write each compartment document to the truck_compartments index
        written_compartments = []
        for compartment in body.compartments:
            doc = {
                "compartment_id": compartment.compartment_id,
                "truck_id": truck_id,
                "capacity_liters": compartment.capacity_liters,
                "allowed_grades": compartment.allowed_grades,
                "position_index": compartment.position_index,
                "tenant_id": tenant_id,
            }
            # Use composite key: truck_id + compartment_id
            doc_id = f"{truck_id}_{compartment.compartment_id}"
            await es.index_document(TRUCK_COMPARTMENTS_INDEX, doc_id, doc)
            written_compartments.append(doc)

        logger.info(
            "Configured %d compartments for truck %s (tenant=%s)",
            len(written_compartments),
            truck_id,
            tenant_id,
        )

        # After successful compartment write, ensure fleet registration (Req 6.1, 6.3)
        if _fleet_registration_service is not None:
            try:
                await _fleet_registration_service.ensure_fleet_registration(
                    truck_id=truck_id,
                    tenant_id=tenant_id,
                    compartments=[c.model_dump() for c in body.compartments],
                )
            except Exception as e:
                # Fleet registration failure must not block compartment operation
                logger.error(
                    "Fleet registration failed for truck %s after compartment write: %s",
                    truck_id,
                    e,
                )
        else:
            logger.warning(
                "FleetRegistrationService not configured; skipping fleet registration for %s",
                truck_id,
            )

        return {
            "truck_id": truck_id,
            "compartments_configured": len(written_compartments),
            "status": "success",
        }
    except Exception as e:
        logger.error("Failed to configure compartments for %s: %s", truck_id, e)
        raise HTTPException(status_code=500, detail=str(e))
