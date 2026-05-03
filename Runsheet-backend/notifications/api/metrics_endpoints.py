"""
Communication SLA metrics REST endpoint.

Provides a GET endpoint for querying communication SLA metrics including
ack latency, notification send latency, driver response latency, and
failed notification rate.

Validates: Requirements 13.5
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from config.settings import get_settings
from errors.exceptions import internal_error, AppException
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_metrics_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service reference, wired via configure_metrics_endpoints()
_metrics_service = None

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

# Auth policy declaration for this router
ROUTER_AUTH_POLICY = "jwt_required"


def configure_metrics_endpoints(*, metrics_service) -> None:
    """Wire the CommunicationMetricsService into the metrics endpoints.

    Called once during application startup so that the router handlers
    can access the service without circular imports.
    """
    global _metrics_service
    _metrics_service = metrics_service


def _get_metrics_service():
    if _metrics_service is None:
        raise RuntimeError(
            "Metrics endpoints not configured. "
            "Call configure_metrics_endpoints() during startup."
        )
    return _metrics_service


@router.get("/communications")
@limiter.limit(_metrics_rate)
async def get_communication_metrics(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    start_date: Optional[str] = Query(
        None, description="Start of date range (ISO 8601)"
    ),
    end_date: Optional[str] = Query(
        None, description="End of date range (ISO 8601)"
    ),
    interval: str = Query(
        "1d", description="Aggregation interval (e.g. '1h', '1d')"
    ),
):
    """Return all communication SLA metrics for the tenant.

    Includes ack_latency, notification_send_latency,
    driver_response_latency, and failed_notification_rate.

    Validates: Requirements 13.5
    """
    svc = _get_metrics_service()
    try:
        result = await svc.get_all_metrics(
            tenant_id=tenant.tenant_id,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
        )
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error("Failed to get communication metrics: %s", e)
        raise internal_error(
            message="Failed to get communication metrics",
            details={"error": str(e)},
        )
