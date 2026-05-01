"""
Agent REST endpoints for the Agentic AI Transformation layer.

Provides REST endpoints for the approval queue, activity log, autonomy
configuration, agent memory, feedback signals, and agent health
management under the ``/api/agent`` prefix.

Uses a ``configure_agent_endpoints()`` function to wire service
dependencies at startup (same pattern as ops and scheduling endpoints).

Requirements: 2.3, 2.4, 2.5, 8.4, 8.5, 10.4, 10.5, 11.5, 11.6,
              12.5, 12.6, 9.6
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service references, wired via configure_agent_endpoints()
# ---------------------------------------------------------------------------

_approval_queue_service = None
_activity_log_service = None
_autonomy_config_service = None
_memory_service = None
_feedback_service = None

router = APIRouter(prefix="/api/agent", tags=["agent"])

# Auth policy declaration for this router (Req 5.2)
# Default: JWT_REQUIRED for all agent endpoints
# Exception: GET /api/agent/health → PUBLIC (declared in POLICY_EXCEPTIONS)
ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ApprovalRejectRequest(BaseModel):
    """Body for the reject-approval endpoint."""
    reason: str = Field(default="", description="Optional rejection reason")


class AutonomyUpdateRequest(BaseModel):
    """Body for the autonomy-level update endpoint."""
    level: str = Field(
        ...,
        description="New autonomy level: suggest-only, auto-low, auto-medium, or full-auto",
    )


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_agent_endpoints(
    *,
    approval_queue_service,
    activity_log_service,
    autonomy_config_service,
    memory_service,
    feedback_service,
) -> None:
    """
    Wire service dependencies into the agent endpoints module.

    Called once during application startup (from ``main.py``) so that
    the router handlers can access shared services without circular
    imports.
    """
    global _approval_queue_service, _activity_log_service
    global _autonomy_config_service, _memory_service, _feedback_service

    _approval_queue_service = approval_queue_service
    _activity_log_service = activity_log_service
    _autonomy_config_service = autonomy_config_service
    _memory_service = memory_service
    _feedback_service = feedback_service


# ---------------------------------------------------------------------------
# Service accessors
# ---------------------------------------------------------------------------


def _get_approval_queue():
    if _approval_queue_service is None:
        raise RuntimeError(
            "Agent endpoints not configured. "
            "Call configure_agent_endpoints() during startup."
        )
    return _approval_queue_service


def _get_activity_log():
    if _activity_log_service is None:
        raise RuntimeError(
            "Agent endpoints not configured. "
            "Call configure_agent_endpoints() during startup."
        )
    return _activity_log_service


def _get_autonomy_config():
    if _autonomy_config_service is None:
        raise RuntimeError(
            "Agent endpoints not configured. "
            "Call configure_agent_endpoints() during startup."
        )
    return _autonomy_config_service


def _get_memory_service():
    if _memory_service is None:
        raise RuntimeError(
            "Agent endpoints not configured. "
            "Call configure_agent_endpoints() during startup."
        )
    return _memory_service


def _get_feedback_service():
    if _feedback_service is None:
        raise RuntimeError(
            "Agent endpoints not configured. "
            "Call configure_agent_endpoints() during startup."
        )
    return _feedback_service


# ===================================================================
# Approval Queue Endpoints
# Requirements: 2.3, 2.4, 2.5
# ===================================================================


@router.get("/approvals")
async def list_approvals(
    request: Request,
    tenant_id: str = Query("default", description="Tenant identifier"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """
    List pending approval requests for a tenant.

    Returns pending approval entries sorted by ``proposed_at`` descending.

    Validates: Requirement 2.3
    """
    svc = _get_approval_queue()
    try:
        result = await svc.list_pending(tenant_id=tenant_id, page=page, size=size)
        # Dual-field deprecation: add unified PaginatedResponse fields
        from schemas.common import paginated_response_dict

        if isinstance(result, dict) and "data" in result:
            pagination = result.get("pagination", {})
            return paginated_response_dict(
                items=result["data"],
                total=pagination.get("total", len(result["data"])),
                page=pagination.get("page", page),
                page_size=pagination.get("size", size),
                request_id=pagination.get("request_id", "unknown"),
            )
        return result
    except Exception as e:
        logger.error(f"Failed to list approvals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/approvals/{action_id}/approve")
async def approve_action(
    action_id: str,
    request: Request,
    reviewer_id: str = Query("admin", description="ID of the reviewing user"),
):
    """
    Approve a pending action and trigger execution.

    Validates: Requirement 2.4
    """
    svc = _get_approval_queue()
    try:
        result = await svc.approve(action_id=action_id, reviewer_id=reviewer_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # Concurrency conflict
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to approve action {action_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/approvals/{action_id}/reject")
async def reject_action(
    action_id: str,
    request: Request,
    body: ApprovalRejectRequest = None,
    reviewer_id: str = Query("admin", description="ID of the reviewing user"),
):
    """
    Reject a pending action with an optional reason.

    Validates: Requirement 2.5
    """
    svc = _get_approval_queue()
    reason = body.reason if body else ""
    try:
        result = await svc.reject(
            action_id=action_id, reviewer_id=reviewer_id, reason=reason
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to reject action {action_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===================================================================
# Activity Log Endpoints
# Requirements: 8.4, 8.5
# ===================================================================


@router.get("/activity")
async def list_activity(
    request: Request,
    tenant_id: str = Query("default", description="Tenant identifier"),
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    action_type: Optional[str] = Query(None, description="Filter by action type"),
    outcome: Optional[str] = Query(None, description="Filter by outcome"),
    time_from: Optional[str] = Query(
        None, description="Start of time range (ISO 8601)"
    ),
    time_to: Optional[str] = Query(
        None, description="End of time range (ISO 8601)"
    ),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(50, ge=1, le=200, description="Page size"),
):
    """
    Paginated activity log with filtering.

    Supports filtering by ``agent_id``, ``action_type``, ``tenant_id``,
    ``outcome``, and time range.

    Validates: Requirement 8.4
    """
    svc = _get_activity_log()

    filters = {"tenant_id": tenant_id}
    if agent_id:
        filters["agent_id"] = agent_id
    if action_type:
        filters["action_type"] = action_type
    if outcome:
        filters["outcome"] = outcome
    if time_from or time_to:
        time_range = {}
        if time_from:
            time_range["gte"] = time_from
        if time_to:
            time_range["lte"] = time_to
        filters["time_range"] = time_range

    try:
        result = await svc.query(filters=filters, page=page, size=size)
        # Dual-field deprecation: add unified PaginatedResponse fields
        from schemas.common import paginated_response_dict

        if isinstance(result, dict) and "data" in result:
            pagination = result.get("pagination", {})
            return paginated_response_dict(
                items=result["data"],
                total=pagination.get("total", len(result["data"])),
                page=pagination.get("page", page),
                page_size=pagination.get("size", size),
                request_id=pagination.get("request_id", "unknown"),
            )
        return result
    except Exception as e:
        logger.error(f"Failed to query activity log: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/activity/stats")
async def get_activity_stats(
    request: Request,
    tenant_id: str = Query("default", description="Tenant identifier"),
):
    """
    Aggregated activity statistics for a tenant.

    Returns actions per agent, success/failure rates, average duration,
    and total action count.

    Validates: Requirement 8.5
    """
    svc = _get_activity_log()
    try:
        result = await svc.get_stats(tenant_id=tenant_id)
        return result
    except Exception as e:
        logger.error(f"Failed to get activity stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===================================================================
# Autonomy Configuration Endpoint
# Requirements: 10.4, 10.5
# ===================================================================


@router.patch("/config/autonomy")
async def update_autonomy_level(
    body: AutonomyUpdateRequest,
    request: Request,
    tenant_id: str = Query("default", description="Tenant identifier"),
):
    """
    Update the tenant's autonomy level (admin-only).

    Requires admin-level JWT claims. Logs the change to the activity log.

    Validates: Requirements 10.4, 10.5
    """
    # Admin-only JWT check: inspect the request for admin claims
    # In production this would use a proper JWT dependency; here we
    # check for an ``x-user-role`` header or JWT claim.
    user_role = request.headers.get("x-user-role", "")
    user_id = request.headers.get("x-user-id", "system")

    if user_role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admin users can update the autonomy level",
        )

    svc = _get_autonomy_config()
    activity_svc = _get_activity_log()

    try:
        previous_level = await svc.set_level(tenant_id=tenant_id, level=body.level)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update autonomy level: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Log the change to the activity log (Requirement 10.5)
    try:
        await activity_svc.log({
            "agent_id": "system",
            "action_type": "autonomy_level_change",
            "tool_name": None,
            "parameters": {
                "tenant_id": tenant_id,
                "old_level": previous_level,
                "new_level": body.level,
            },
            "risk_level": None,
            "outcome": "success",
            "duration_ms": 0,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": None,
            "details": {
                "changed_by": user_id,
                "old_level": previous_level,
                "new_level": body.level,
            },
        })
    except Exception as e:
        logger.warning(f"Failed to log autonomy level change: {e}")

    return {
        "tenant_id": tenant_id,
        "previous_level": previous_level,
        "new_level": body.level,
    }


# ===================================================================
# Memory Endpoints
# Requirements: 11.5, 11.6
# ===================================================================


@router.get("/memory")
async def list_memories(
    request: Request,
    tenant_id: str = Query("default", description="Tenant identifier"),
    memory_type: Optional[str] = Query(
        None, description="Filter by memory type (pattern or preference)"
    ),
    tags: Optional[str] = Query(
        None, description="Comma-separated tags to filter by"
    ),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """
    List stored memories for a tenant.

    Supports filtering by ``memory_type`` and ``tags``.

    Validates: Requirement 11.5
    """
    svc = _get_memory_service()
    tag_list = [t.strip() for t in tags.split(",")] if tags else None

    try:
        result = await svc.list_memories(
            tenant_id=tenant_id,
            memory_type=memory_type,
            tags=tag_list,
            page=page,
            size=size,
        )
        # Dual-field deprecation: add unified PaginatedResponse fields
        from schemas.common import paginated_response_dict

        if isinstance(result, dict) and "data" in result:
            pagination = result.get("pagination", {})
            return paginated_response_dict(
                items=result["data"],
                total=pagination.get("total", len(result["data"])),
                page=pagination.get("page", page),
                page_size=pagination.get("size", size),
                request_id=pagination.get("request_id", "unknown"),
            )
        return result
    except Exception as e:
        logger.error(f"Failed to list memories: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/memory/{memory_id}")
async def delete_memory(
    memory_id: str,
    request: Request,
    tenant_id: str = Query("default", description="Tenant identifier"),
):
    """
    Delete a specific memory.

    Verifies the memory belongs to the given tenant before deleting.

    Validates: Requirement 11.6
    """
    svc = _get_memory_service()
    try:
        deleted = await svc.delete(memory_id=memory_id, tenant_id=tenant_id)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Memory {memory_id} not found or does not belong to tenant",
            )
        return {"deleted": True, "memory_id": memory_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete memory {memory_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===================================================================
# Feedback Endpoints
# Requirements: 12.5, 12.6
# ===================================================================


@router.get("/feedback")
async def list_feedback(
    request: Request,
    tenant_id: str = Query("default", description="Tenant identifier"),
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    action_type: Optional[str] = Query(None, description="Filter by action type"),
    time_from: Optional[str] = Query(
        None, description="Start of time range (ISO 8601)"
    ),
    time_to: Optional[str] = Query(
        None, description="End of time range (ISO 8601)"
    ),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """
    List feedback signals for a tenant.

    Supports filtering by ``agent_id``, ``action_type``, and time range.

    Validates: Requirement 12.5
    """
    svc = _get_feedback_service()

    time_range = None
    if time_from or time_to:
        time_range = {}
        if time_from:
            time_range["gte"] = time_from
        if time_to:
            time_range["lte"] = time_to

    try:
        result = await svc.list_feedback(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action_type=action_type,
            time_range=time_range,
            page=page,
            size=size,
        )
        # Dual-field deprecation: add unified PaginatedResponse fields
        from schemas.common import paginated_response_dict

        if isinstance(result, dict) and "data" in result:
            pagination = result.get("pagination", {})
            return paginated_response_dict(
                items=result["data"],
                total=pagination.get("total", len(result["data"])),
                page=pagination.get("page", page),
                page_size=pagination.get("size", size),
                request_id=pagination.get("request_id", "unknown"),
            )
        return result
    except Exception as e:
        logger.error(f"Failed to list feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/feedback/stats")
async def get_feedback_stats(
    request: Request,
    tenant_id: str = Query("default", description="Tenant identifier"),
):
    """
    Aggregated feedback statistics for a tenant.

    Returns rejection rate, override count, rejections per agent, and
    common action types.

    Validates: Requirement 12.6
    """
    svc = _get_feedback_service()
    try:
        result = await svc.get_stats(tenant_id=tenant_id)
        return result
    except Exception as e:
        logger.error(f"Failed to get feedback stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===================================================================
# Agent Health Endpoints
# Requirements: 9.6
# ===================================================================


@router.get("/health")
async def get_agent_health(request: Request):
    """
    Status of all autonomous agents.

    Returns the status (running, paused/stopped, error) and agent_id
    for each registered autonomous agent.

    Validates: Requirement 9.6
    """
    agents = getattr(request.app.state, "autonomous_agents", {})
    health = {}
    for agent_id, agent in agents.items():
        health[agent_id] = {
            "agent_id": agent_id,
            "status": agent.status,
        }
    return {"agents": health}


@router.post("/{agent_id}/pause")
async def pause_agent(agent_id: str, request: Request):
    """
    Pause an autonomous agent.

    Stops the agent's polling loop. The agent can be resumed later.

    Validates: Requirement 9.6
    """
    agents = getattr(request.app.state, "autonomous_agents", {})
    agent = agents.get(agent_id)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )

    if agent.status == "stopped":
        return {"agent_id": agent_id, "status": "already_stopped"}

    try:
        await agent.stop()
        # Log the pause to activity log
        activity_svc = _get_activity_log()
        await activity_svc.log({
            "agent_id": agent_id,
            "action_type": "agent_paused",
            "tool_name": None,
            "parameters": None,
            "risk_level": None,
            "outcome": "success",
            "duration_ms": 0,
            "tenant_id": None,
            "user_id": request.headers.get("x-user-id", "system"),
            "session_id": None,
            "details": {"action": "pause"},
        })
    except Exception as e:
        logger.error(f"Failed to pause agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {"agent_id": agent_id, "status": "stopped"}


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: str, request: Request):
    """
    Resume a paused autonomous agent.

    Restarts the agent's polling loop.

    Validates: Requirement 9.6
    """
    agents = getattr(request.app.state, "autonomous_agents", {})
    agent = agents.get(agent_id)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )

    if agent.status == "running":
        return {"agent_id": agent_id, "status": "already_running"}

    try:
        await agent.start()
        # Log the resume to activity log
        activity_svc = _get_activity_log()
        await activity_svc.log({
            "agent_id": agent_id,
            "action_type": "agent_resumed",
            "tool_name": None,
            "parameters": None,
            "risk_level": None,
            "outcome": "success",
            "duration_ms": 0,
            "tenant_id": None,
            "user_id": request.headers.get("x-user-id", "system"),
            "session_id": None,
            "details": {"action": "resume"},
        })
    except Exception as e:
        logger.error(f"Failed to resume agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {"agent_id": agent_id, "status": "running"}
