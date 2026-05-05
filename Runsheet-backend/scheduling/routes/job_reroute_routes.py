"""
REST API endpoint for ad-hoc job rerouting.

Provides a POST endpoint for dispatchers to reroute any cargo truck
to an alternate destination through the ConfirmationProtocol.

Uses a ``configure_job_reroute_routes()`` function to wire service
dependencies at startup (same pattern as scheduling/api/endpoints.py).

Validates: Requirement 2.8
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request

from config.settings import get_settings
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from scheduling.models import RerouteJobRequest
from scheduling.services.job_reroute_service import JobRerouteService

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_scheduling_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service reference, wired via configure_job_reroute_routes()
_job_reroute_service: Optional[JobRerouteService] = None

router = APIRouter(prefix="/api/v1/scheduling", tags=["scheduling-reroute"])

# Auth policy declaration for this router (Req 2.8)
ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_job_reroute_routes(
    *,
    job_reroute_service: JobRerouteService,
) -> None:
    """
    Wire service dependencies into the job reroute routes module.

    Called once during application startup so that the router handlers
    can access the shared JobRerouteService.
    """
    global _job_reroute_service
    _job_reroute_service = job_reroute_service


def _get_job_reroute_service() -> JobRerouteService:
    """Return the configured JobRerouteService or raise."""
    if _job_reroute_service is None:
        raise RuntimeError(
            "Job reroute routes not configured. "
            "Call configure_job_reroute_routes() during startup."
        )
    return _job_reroute_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# POST /api/v1/scheduling/jobs/{job_id}/reroute
# Validates: Requirement 2.8
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/reroute")
@limiter.limit(_scheduling_rate)
async def reroute_job(
    job_id: str,
    body: RerouteJobRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Reroute a job to a new destination.

    Accepts a RerouteJobRequest body and extracts tenant_id from the
    JWT context. Delegates to JobRerouteService.reroute_job() which
    validates the job status, submits through the ConfirmationProtocol,
    and broadcasts the update.

    Validates: Requirement 2.8
    """
    svc = _get_job_reroute_service()
    result = await svc.reroute_job(
        job_id=job_id,
        new_destination=body.new_destination,
        tenant_id=tenant.tenant_id,
        reason=body.reason,
        new_destination_location=(
            body.new_destination_location.model_dump()
            if body.new_destination_location
            else None
        ),
        actor_id=tenant.user_id,
    )
    return {
        "data": result,
        "request_id": _get_request_id(request),
    }
