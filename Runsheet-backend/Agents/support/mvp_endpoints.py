"""
REST API endpoints for the Fuel Distribution MVP pipeline.

Provides endpoints for triggering pipeline runs, retrieving plans,
initiating replanning, querying forecasts and priorities,
configuring truck compartments, plan approval/rejection, driver
check-ins, outcome retrieval, and cost analysis.

Uses a ``configure_mvp_endpoints()`` function to wire service
dependencies at startup (same pattern as agent_endpoints.py).

Validates: Requirements 1.1–1.5, 2.1–2.6, 3.1–3.9, 4.1–4.7, 5.1–5.6, 6.1, 6.3, 8.1–8.6
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ops.middleware.tenant_guard import TenantContext, get_tenant_context

from errors.exceptions import (
    AppException,
    internal_error,
    resource_not_found,
    validation_error,
)
from errors.codes import ErrorCode
from services.time_utils import utcnow
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service references, wired via configure_mvp_endpoints()
# ---------------------------------------------------------------------------

_pipeline = None
_es_service = None
_exception_replanning_agent = None
_fleet_registration_service = None
_plan_execution_service = None
_plan_execution_ws_manager = None

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


class CheckinRequest(BaseModel):
    """Body for POST /plan/{plan_id}/checkin."""
    route_id: str = Field(..., description="Route identifier")
    station_id: str = Field(..., description="Station being checked into")
    sequence: int = Field(..., ge=0, description="Stop sequence number")
    actual_quantities: Dict[str, float] = Field(
        ..., description="Fuel grade to liters delivered mapping"
    )


class RejectRequest(BaseModel):
    """Body for POST /plan/{plan_id}/reject."""
    reason: Optional[str] = Field(
        default=None, description="Optional rejection reason"
    )


class CostConfigRequest(BaseModel):
    """Body for PUT /cost-config."""
    fuel_consumption_rate: float = Field(
        ..., ge=0, description="Fuel consumption rate in liters per km"
    )
    fuel_price_per_liter: float = Field(
        ..., ge=0, description="Fuel price per liter"
    )
    driver_hourly_rate: float = Field(
        ..., ge=0, description="Driver hourly rate"
    )
    currency: str = Field(
        default="USD", description="Currency code"
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
    plan_execution_service=None,
    plan_execution_ws_manager=None,
) -> None:
    """Wire service dependencies into the MVP endpoints module.

    Called once during application startup so that the router handlers
    can access shared services without circular imports.
    """
    global _pipeline, _es_service, _exception_replanning_agent
    global _fleet_registration_service, _plan_execution_service
    global _plan_execution_ws_manager
    _pipeline = pipeline
    _es_service = es_service
    _exception_replanning_agent = exception_replanning_agent
    _fleet_registration_service = fleet_registration_service
    _plan_execution_service = plan_execution_service
    _plan_execution_ws_manager = plan_execution_ws_manager


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


def _get_execution_service():
    if _plan_execution_service is None:
        raise RuntimeError(
            "PlanExecutionService not configured. "
            "Call configure_mvp_endpoints() with plan_execution_service."
        )
    return _plan_execution_service


def _get_ws_manager():
    if _plan_execution_ws_manager is None:
        raise RuntimeError(
            "PlanExecutionWSManager not configured. "
            "Call configure_mvp_endpoints() with plan_execution_ws_manager."
        )
    return _plan_execution_ws_manager


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/plan/generate (Req 8.1)
# ---------------------------------------------------------------------------


@router.post("/plan/generate")
async def generate_plan(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Trigger a full pipeline run, returning run_id and status.

    Validates: Requirement 8.1
    """
    pipeline = _get_pipeline()
    tenant_id = tenant.tenant_id
    try:
        run_id = await pipeline.run(tenant_id=tenant_id)
        status_info = await pipeline.get_status(run_id)
        return GeneratePlanResponse(
            run_id=run_id,
            status=status_info["state"] if status_info else "pending",
        )
    except Exception as e:
        logger.error("Failed to generate plan: %s", e)
        raise internal_error(message=str(e), details={"tenant_id": tenant_id})


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/plan/{plan_id} (Req 8.2)
# ---------------------------------------------------------------------------


@router.get("/plan/{plan_id}")
async def get_plan(
    plan_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Retrieve a complete plan (loading + route) by plan_id or run_id.

    Validates: Requirement 8.2
    """
    es = _get_es()
    tenant_id = tenant.tenant_id

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
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Trigger exception replanning for an existing plan.

    Validates: Requirement 8.3
    """
    if body is None:
        body = ReplanRequest()

    tenant_id = tenant.tenant_id

    if _exception_replanning_agent is None:
        raise AppException(
            error_code=ErrorCode.AI_SERVICE_UNAVAILABLE,
            message="Exception replanning agent not available",
            status_code=503,
            details={"service": "exception_replanning_agent"},
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
        raise internal_error(message=str(e), details={"plan_id": plan_id})


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/forecasts (Req 8.4 + fuel-ops hardening Req 1.1.4, 1.6.1)
# ---------------------------------------------------------------------------


@router.get("/forecasts")
async def get_forecasts(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    station_id: Optional[str] = Query(None, description="Filter by station ID"),
    fuel_grade: Optional[str] = Query(None, description="Filter by fuel grade"),
    customer_tank_id: Optional[str] = Query(
        None,
        description=(
            "Filter to a single Customer_Tank (fuel-ops hardening Req 1.1.4)."
        ),
    ),
    customer_id: Optional[str] = Query(
        None,
        description=(
            "Filter to all forecasts belonging to a customer_id "
            "(fuel-ops hardening Req 1.1.4)."
        ),
    ),
    customer_type: Optional[str] = Query(
        None,
        description=(
            "Filter by customer_type (residential | commercial | keep_full | "
            "will_call | auto_fill; Req 1.1.4)."
        ),
    ),
    fuel_type: Optional[str] = Query(
        None,
        description=(
            "Filter by fuel_type family (propane | heating_oil | diesel | "
            "generator_fuel | farm_fuel | gasoline; Req 1.1.4)."
        ),
    ),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """Retrieve the latest tank forecasts with optional filters.

    The ``customer_tank_id`` / ``customer_id`` / ``customer_type`` /
    ``fuel_type`` parameters let fuel-marketer UIs slice forecasts per
    residential / commercial / keep-full customer without pulling the
    full retail-station list (fuel-ops hardening Req 1.1.4 and 1.6.1).

    When ``fuel_grade`` is supplied, legacy NG aliases (``AGO``, ``PMS``,
    ``ATK``, ``LPG``) are canonicalized before matching so queries work
    regardless of whether the tenant has migrated to the US product
    catalog. Unknown codes degrade to the raw value so they return an
    empty result rather than a 400.

    Validates: Requirements 8.4, 1.1.4, 1.6.1.
    """
    es = _get_es()

    tenant_id = tenant.tenant_id
    must_clauses = [{"term": {"tenant_id": tenant_id}}]
    if station_id:
        must_clauses.append({"term": {"station_id": station_id}})
    if fuel_grade:
        # Canonicalize legacy aliases so AGO → DIESEL_2 matches the
        # canonicalized ``fuel_grade`` column written by the agent
        # (Req 6.1.4). Unknown codes fall back to raw-value matching so
        # the caller sees an empty result set rather than a 400.
        try:
            normalized_grade = canonicalize(fuel_grade)
        except UnknownFuelProductError:
            normalized_grade = fuel_grade
        must_clauses.append({"term": {"fuel_grade": normalized_grade}})
    if customer_tank_id:
        must_clauses.append({"term": {"customer_tank_id": customer_tank_id}})
    if customer_id:
        must_clauses.append({"term": {"customer_id": customer_id}})
    if customer_type:
        must_clauses.append({"term": {"customer_type": customer_type}})
    if fuel_type:
        must_clauses.append({"term": {"fuel_type": fuel_type}})

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
        total_count = total.get("value", 0) if hasattr(total, "get") or isinstance(total, dict) else total

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
        raise internal_error(message=str(e), details={"tenant_id": tenant_id})


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/priorities (moved — see fuel.api.fuel_ops_endpoints)
# ---------------------------------------------------------------------------
#
# Task 5.6 of the fuel-ops-hardening spec migrated the priorities endpoint
# to :mod:`fuel.api.fuel_ops_endpoints` so it can share the same JWT-backed
# tenant context used by the rest of the fuel-ops surface and gain the
# Capability-3 ``safe_to_delay_bucket`` filter. Keeping the handler here
# would create a duplicate FastAPI route registration at
# ``/api/fuel/mvp/priorities`` because both routers are mounted during
# bootstrap. See ``fuel/api/fuel_ops_endpoints.list_priorities``.

# ---------------------------------------------------------------------------
# PUT /api/fuel/mvp/compartments/{truck_id} (Req 6.1, 6.3)
# ---------------------------------------------------------------------------


@router.put("/compartments/{truck_id}")
async def configure_compartments(
    truck_id: str,
    body: CompartmentConfigRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Configure compartments for a fuel tanker.

    Writes compartment documents to the truck_compartments index and
    ensures the truck is registered in the fleet (trucks) index.

    Validates: Requirements 6.1, 6.3
    """
    es = _get_es()
    tenant_id = tenant.tenant_id

    from Agents.support.mvp_es_mappings import TRUCK_COMPARTMENTS_INDEX

    try:
        # Write each compartment document to the truck_compartments index
        written_compartments = []
        for compartment in body.compartments:
            # Canonicalize every allowed_grade before persistence so legacy
            # NG aliases (AGO/PMS/ATK/LPG) and US codes
            # (DIESEL_2/GASOLINE_REG/KEROSENE/PROPANE) both land as the
            # canonical product_code in truck_compartments (Req 6.1.4).
            # Unknown codes propagate as a 400 VALIDATION_ERROR with the
            # offending value surfaced back to the caller.
            canonical_grades: List[str] = []
            for grade in compartment.allowed_grades:
                try:
                    canonical_grades.append(canonicalize(grade))
                except UnknownFuelProductError as exc:
                    raise validation_error(
                        message="Unknown fuel product in allowed_grades",
                        details={
                            "compartment_id": compartment.compartment_id,
                            "fuel_grade": exc.code_or_alias,
                        },
                    ) from exc

            doc = {
                "compartment_id": compartment.compartment_id,
                "truck_id": truck_id,
                "capacity_liters": compartment.capacity_liters,
                "allowed_grades": canonical_grades,
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
    except AppException:
        # Surface the original status code (e.g. 400 for unknown_product_code)
        # rather than hiding it behind a generic 500.
        raise
    except Exception as e:
        logger.error("Failed to configure compartments for %s: %s", truck_id, e)
        raise internal_error(message=str(e), details={"truck_id": truck_id})


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/plans (Req 1.1–1.5)
# ---------------------------------------------------------------------------


@router.get("/plans")
async def list_plans(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    status: Optional[str] = Query(None, description="Filter by plan status"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """List plans for a tenant with optional status filter, paginated.

    Queries mvp_load_plans by tenant_id, optionally filtered by status,
    sorted by created_at descending.

    Validates: Requirements 1.1, 1.3, 1.4, 1.5
    """
    es = _get_es()

    tenant_id = tenant.tenant_id
    must_clauses = [{"term": {"tenant_id": tenant_id}}]
    if status:
        must_clauses.append({"term": {"status": status}})

    query = {
        "query": {"bool": {"must": must_clauses}},
        "sort": [{"created_at": {"order": "desc"}}],
        "from": (page - 1) * size,
        "size": size,
    }

    try:
        resp = await es.search_documents("mvp_load_plans", query, size)
        hits = resp.get("hits", {}).get("hits", [])
        total = resp.get("hits", {}).get("total", {})
        total_count = total.get("value", 0) if hasattr(total, "get") or isinstance(total, dict) else total

        items = [hit["_source"] for hit in hits]

        from schemas.common import paginated_response_dict
        return paginated_response_dict(
            items=items,
            total=total_count,
            page=page,
            page_size=size,
        )
    except Exception as e:
        logger.error("Failed to list plans: %s", e)
        raise internal_error(message=str(e), details={"tenant_id": tenant_id})


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/plan/{plan_id}/approve (Req 2.1, 2.3, 2.4)
# ---------------------------------------------------------------------------


@router.post("/plan/{plan_id}/approve")
async def approve_plan(
    plan_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Approve a plan, transitioning from draft to dispatched.

    Validates plan is in "draft" status, updates to "dispatched" with
    dispatcher_id and approved_at timestamp, then creates execution
    records for each route associated with the plan.

    The ``dispatcher_id`` is derived server-side from the verified
    session (``tenant.user_id``); it is never accepted from the client.

    Returns 409 if plan is not in draft status.

    Validates: Requirements 2.1, 2.3, 2.4
    """
    es = _get_es()
    execution_service = _get_execution_service()

    tenant_id = tenant.tenant_id
    dispatcher_id = tenant.user_id

    # Fetch the plan
    plan_query = {
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

    try:
        resp = await es.search_documents("mvp_load_plans", plan_query, 1)
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            raise resource_not_found(
                message=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )

        plan_doc = hits[0]["_source"]
        plan_status = plan_doc.get("status", "")

        if plan_status != "draft" and plan_status != "proposed":
            raise AppException(
                error_code=ErrorCode.INVALID_STATUS_TRANSITION,
                message=f"Plan {plan_id} is not in 'draft' or 'proposed' status (current: {plan_status}). "
                       "Only draft/proposed plans can be approved.",
                status_code=409,
                details={"plan_id": plan_id, "current_status": plan_status},
            )

        # Update plan status to dispatched
        now = utcnow().isoformat()
        update_doc = {
            "status": "dispatched",
            "approved_by": dispatcher_id,
            "approved_at": now,
        }
        await es.update_document("mvp_load_plans", plan_id, update_doc)

        # Fetch routes for this plan and create execution records
        route_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                    ],
                },
            },
            "size": 20,
        }
        route_resp = await es.search_documents("mvp_routes", route_query, 20)
        route_hits = route_resp.get("hits", {}).get("hits", [])

        executions_created = []
        for route_hit in route_hits:
            route_doc = route_hit["_source"]
            route_id = route_doc.get("route_id", "")
            stops = route_doc.get("stops", [])

            execution = await execution_service.create_execution(
                plan_id=plan_id,
                route_id=route_id,
                tenant_id=tenant_id,
                stops=stops,
            )
            executions_created.append(execution["execution_id"])

        logger.info(
            "Approved plan %s (tenant=%s, dispatcher=%s), created %d executions",
            plan_id,
            tenant_id,
            dispatcher_id,
            len(executions_created),
        )

        return {
            "plan_id": plan_id,
            "status": "dispatched",
            "approved_by": dispatcher_id,
            "approved_at": now,
            "executions_created": len(executions_created),
        }

    except AppException:
        raise
    except Exception as e:
        logger.error("Failed to approve plan %s: %s", plan_id, e)
        raise internal_error(message=str(e), details={"plan_id": plan_id})


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/plan/{plan_id}/reject (Req 2.2, 2.3, 2.5)
# ---------------------------------------------------------------------------


@router.post("/plan/{plan_id}/reject")
async def reject_plan(
    plan_id: str,
    request: Request,
    body: Optional[RejectRequest] = None,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Reject a plan, transitioning from draft to rejected.

    Validates plan is in "draft" status, updates to "rejected" with
    dispatcher_id, rejected_at, and optional reason.

    The ``dispatcher_id`` is derived server-side from the verified
    session (``tenant.user_id``); it is never accepted from the client.

    Returns 409 if plan is not in draft status.

    Validates: Requirements 2.2, 2.3, 2.5
    """
    es = _get_es()

    tenant_id = tenant.tenant_id
    dispatcher_id = tenant.user_id

    # Fetch the plan
    plan_query = {
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

    try:
        resp = await es.search_documents("mvp_load_plans", plan_query, 1)
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            raise resource_not_found(
                message=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )

        plan_doc = hits[0]["_source"]
        plan_status = plan_doc.get("status", "")

        if plan_status != "draft" and plan_status != "proposed":
            raise AppException(
                error_code=ErrorCode.INVALID_STATUS_TRANSITION,
                message=f"Plan {plan_id} is not in 'draft' or 'proposed' status (current: {plan_status}). "
                       "Only draft/proposed plans can be rejected.",
                status_code=409,
                details={"plan_id": plan_id, "current_status": plan_status},
            )

        # Update plan status to rejected
        now = utcnow().isoformat()
        reason = body.reason if body else None

        update_doc = {
            "status": "rejected",
            "rejected_by": dispatcher_id,
            "rejected_at": now,
        }
        if reason:
            update_doc["rejection_reason"] = reason

        await es.update_document("mvp_load_plans", plan_id, update_doc)

        logger.info(
            "Rejected plan %s (tenant=%s, dispatcher=%s, reason=%s)",
            plan_id,
            tenant_id,
            dispatcher_id,
            reason or "none",
        )

        return {
            "plan_id": plan_id,
            "status": "rejected",
            "rejected_by": dispatcher_id,
            "rejected_at": now,
            "rejection_reason": reason,
        }

    except AppException:
        raise
    except Exception as e:
        logger.error("Failed to reject plan %s: %s", plan_id, e)
        raise internal_error(message=str(e), details={"plan_id": plan_id})


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/plan/{plan_id}/checkin (Req 3.1–3.7)
# ---------------------------------------------------------------------------


@router.post("/plan/{plan_id}/checkin")
async def driver_checkin(
    plan_id: str,
    body: CheckinRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Record a driver check-in at a stop.

    Validates plan is "dispatched", validates stop not already completed,
    records check-in via PlanExecutionService, broadcasts via WebSocket,
    and transitions to "completed" if all stops are done (triggering
    outcome computation and actual cost calculation).

    Returns 409 if plan is not dispatched or stop already completed.

    Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
    """
    execution_service = _get_execution_service()
    ws_manager = _get_ws_manager()

    tenant_id = tenant.tenant_id

    try:
        # record_checkin validates plan status and stop state internally
        result = await execution_service.record_checkin(
            plan_id=plan_id,
            route_id=body.route_id,
            station_id=body.station_id,
            sequence=body.sequence,
            actual_quantities=body.actual_quantities,
            tenant_id=tenant_id,
        )

        # Broadcast execution update via WebSocket
        stop_data = {
            "station_id": body.station_id,
            "sequence": body.sequence,
            "status": "completed",
        }
        await ws_manager.broadcast_execution_update(
            plan_id=plan_id,
            route_id=body.route_id,
            stop_data=stop_data,
            completed_stops=result["completed_stops"],
            total_stops=result["total_stops"],
        )

        # If all stops are complete, transition plan to "completed" and
        # trigger outcome computation and actual cost calculation
        if result.get("all_complete"):
            es = _get_es()
            now = utcnow().isoformat()
            await es.update_document(
                "mvp_load_plans", plan_id, {"status": "completed"}
            )

            # Trigger outcome computation (Req 4.1–4.4)
            try:
                await execution_service.compute_outcomes(plan_id, tenant_id)
            except Exception as e:
                logger.error(
                    "Failed to compute outcomes for plan %s: %s", plan_id, e
                )

            # Trigger actual cost calculation (Req 5.3)
            try:
                await execution_service.compute_actual_cost(plan_id, tenant_id)
            except Exception as e:
                logger.error(
                    "Failed to compute actual cost for plan %s: %s", plan_id, e
                )

            logger.info(
                "Plan %s completed - all stops done, outcomes and costs computed",
                plan_id,
            )

        return {
            "plan_id": plan_id,
            "route_id": body.route_id,
            "station_id": body.station_id,
            "sequence": body.sequence,
            "completed_stops": result["completed_stops"],
            "total_stops": result["total_stops"],
            "all_complete": result["all_complete"],
            "updated_at": result["updated_at"],
        }

    except ValueError as e:
        # PlanExecutionService raises ValueError for state conflicts
        error_msg = str(e)
        if "not in 'dispatched' status" in error_msg:
            raise AppException(
                error_code=ErrorCode.INVALID_STATUS_TRANSITION,
                message=error_msg,
                status_code=409,
                details={"plan_id": plan_id},
            )
        elif "already completed" in error_msg:
            raise AppException(
                error_code=ErrorCode.INVALID_STATUS_TRANSITION,
                message=error_msg,
                status_code=409,
                details={"plan_id": plan_id},
            )
        else:
            raise validation_error(message=error_msg, details={"plan_id": plan_id})
    except AppException:
        raise
    except Exception as e:
        logger.error("Failed to record check-in for plan %s: %s", plan_id, e)
        raise internal_error(message=str(e), details={"plan_id": plan_id})


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/plan/{plan_id}/outcomes (Req 4.5, 4.6)
# ---------------------------------------------------------------------------


@router.get("/plan/{plan_id}/outcomes")
async def get_plan_outcomes(
    plan_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Retrieve outcome data for a completed plan.

    Returns per-stop variance metrics and aggregates.
    Returns 400 if plan is not in "completed" status.

    Validates: Requirements 4.5, 4.6
    """
    es = _get_es()
    tenant_id = tenant.tenant_id

    # Verify plan is completed
    plan_query = {
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

    try:
        resp = await es.search_documents("mvp_load_plans", plan_query, 1)
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            raise resource_not_found(
                message=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )

        plan_doc = hits[0]["_source"]
        plan_status = plan_doc.get("status", "")

        if plan_status != "completed":
            raise validation_error(
                message=f"Plan {plan_id} is not completed (current status: {plan_status}). "
                       "Outcome data is only available for completed plans.",
                details={"plan_id": plan_id, "current_status": plan_status},
            )

        # Fetch outcome data from mvp_plan_outcomes
        outcome_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"plan_id": plan_id}},
                    ],
                },
            },
            "sort": [{"created_at": {"order": "desc"}}],
            "size": 1,
        }
        outcome_resp = await es.search_documents(
            "mvp_plan_outcomes", outcome_query, 1
        )
        outcome_hits = outcome_resp.get("hits", {}).get("hits", [])

        if not outcome_hits:
            raise resource_not_found(
                message=f"No outcome data found for plan {plan_id}",
                details={"plan_id": plan_id},
            )

        return outcome_hits[0]["_source"]

    except AppException:
        raise
    except Exception as e:
        logger.error("Failed to get outcomes for plan %s: %s", plan_id, e)
        raise internal_error(message=str(e), details={"plan_id": plan_id})


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/plan/{plan_id}/costs (Req 5.1–5.5)
# ---------------------------------------------------------------------------


@router.get("/plan/{plan_id}/costs")
async def get_plan_costs(
    plan_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Retrieve cost data for a plan.

    Returns estimated costs (always available after generation) and
    actual costs if the plan is completed.
    Returns 400 if cost config is not found for the tenant.

    Validates: Requirements 5.1, 5.2, 5.4, 5.5
    """
    es = _get_es()
    execution_service = _get_execution_service()

    tenant_id = tenant.tenant_id

    # Check cost config exists for tenant
    cost_config = await execution_service.get_cost_config(tenant_id)
    if cost_config is None:
        raise validation_error(
            message=f"Cost configuration not found for tenant {tenant_id}. "
                   "Please configure cost parameters first.",
            details={"tenant_id": tenant_id},
        )

    # Fetch the plan
    plan_query = {
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

    try:
        resp = await es.search_documents("mvp_load_plans", plan_query, 1)
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            raise resource_not_found(
                message=f"Plan {plan_id} not found",
                details={"plan_id": plan_id},
            )

        plan_doc = hits[0]["_source"]
        plan_status = plan_doc.get("status", "")

        result = {
            "plan_id": plan_id,
            "status": plan_status,
            "estimated_cost": plan_doc.get("estimated_cost"),
            "actual_cost": None,
            "cost_variance_pct": None,
        }

        # If plan is completed, include actual costs
        if plan_status == "completed":
            result["actual_cost"] = plan_doc.get("actual_cost")
            result["cost_variance_pct"] = plan_doc.get("cost_variance_pct")

        return result

    except AppException:
        raise
    except Exception as e:
        logger.error("Failed to get costs for plan %s: %s", plan_id, e)
        raise internal_error(message=str(e), details={"plan_id": plan_id})


# ---------------------------------------------------------------------------
# PUT /api/fuel/mvp/cost-config (Req 5.5)
# ---------------------------------------------------------------------------


@router.put("/cost-config")
async def update_cost_config(
    request: Request,
    body: CostConfigRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Upsert tenant cost configuration.

    Creates or updates the cost configuration for a tenant, used
    for computing estimated and actual plan costs.

    Validates: Requirement 5.5
    """
    execution_service = _get_execution_service()

    tenant_id = tenant.tenant_id

    try:
        config = {
            "fuel_consumption_rate": body.fuel_consumption_rate,
            "fuel_price_per_liter": body.fuel_price_per_liter,
            "driver_hourly_rate": body.driver_hourly_rate,
            "currency": body.currency,
        }

        result = await execution_service.upsert_cost_config(tenant_id, config)

        logger.info("Updated cost config for tenant %s", tenant_id)

        return {
            "tenant_id": tenant_id,
            "status": "success",
            "config": result,
        }

    except Exception as e:
        logger.error("Failed to update cost config for tenant %s: %s", tenant_id, e)
        raise internal_error(message=str(e), details={"tenant_id": tenant_id})
