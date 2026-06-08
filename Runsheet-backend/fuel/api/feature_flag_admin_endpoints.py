"""
Admin endpoints for the Order Intake Pipeline feature flag rollout.

Provides a single endpoint to flip the ``overlay.order_intake_pipeline``
flag state within 60 seconds:

    POST /api/ops/admin/feature-flags/{tenant_id}/order-intake-pipeline/{new_state}

Role-gated to admin. Broadcasts the flag change to all active WS clients
so UIs update without a refresh.

Validates: Requirement 9.3.5.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from errors.exceptions import forbidden, invalid_request
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from ops.services.feature_flags import VALID_OVERLAY_STATES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ops/admin", tags=["admin", "feature-flags"])

# Module-level service references, wired via configure_feature_flag_admin()
_feature_flag_service: Optional[Any] = None
_orders_ws_manager: Optional[Any] = None


def configure_feature_flag_admin(
    *,
    feature_flag_service: Any,
    orders_ws_manager: Optional[Any] = None,
) -> None:
    """Wire service dependencies for the admin feature flag endpoints.

    Called during bootstrap to inject the FeatureFlagService and
    OrdersWSManager instances.
    """
    global _feature_flag_service, _orders_ws_manager
    _feature_flag_service = feature_flag_service
    _orders_ws_manager = orders_ws_manager


#: The overlay flag key for the order intake pipeline.
ORDER_INTAKE_PIPELINE_FLAG_KEY = "order_intake_pipeline"

#: Valid states for the order intake pipeline overlay flag.
VALID_STATES = frozenset({"disabled", "shadow", "active_gated", "active_auto"})


@router.get("/feature-flags/{tenant_id}/order-intake-pipeline")
async def get_order_intake_pipeline_state(
    tenant_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Return the current order-intake-pipeline overlay state for a tenant.

    One of ``disabled``, ``shadow``, ``active_gated``, ``active_auto``
    (defaults to ``disabled`` when never set). Role-gated to admin, matching
    the setter.

    Validates: Requirement 9.3.5.
    """
    roles = getattr(tenant, "roles", None) or []
    if "admin" not in roles:
        raise forbidden(
            message="Order intake pipeline flag management requires the admin role",
            details={"required_role": "admin"},
        )

    if _feature_flag_service is None:
        from errors.exceptions import elasticsearch_unavailable

        raise elasticsearch_unavailable(
            message="Feature flag service not configured",
            details={"service": "feature_flag_service"},
        )

    state = await _feature_flag_service.get_overlay_state(
        ORDER_INTAKE_PIPELINE_FLAG_KEY, tenant_id
    )
    return {
        "data": {
            "tenant_id": tenant_id,
            "flag_key": ORDER_INTAKE_PIPELINE_FLAG_KEY,
            "state": state,
        },
        "request_id": getattr(request.state, "request_id", ""),
    }


@router.post("/feature-flags/{tenant_id}/order-intake-pipeline/{new_state}")
async def set_order_intake_pipeline_state(
    tenant_id: str,
    new_state: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Flip the order intake pipeline overlay flag for a tenant.

    The flag change takes effect within 60 seconds (Redis write is
    immediate; downstream consumers poll or receive WS notification).

    Args:
        tenant_id: The target tenant.
        new_state: One of ``disabled``, ``shadow``, ``active_gated``,
                   ``active_auto``.

    Returns:
        JSON with the previous and new state.

    Raises:
        403: If the caller does not have the ``admin`` role.
        400: If ``new_state`` is not a valid overlay state.
        503: If the feature flag service is not configured.

    Validates: Requirement 9.3.5.
    """
    # Role-gate: admin only
    roles = getattr(tenant, "roles", None) or []
    if "admin" not in roles:
        raise forbidden(
            message="Order intake pipeline flag management requires the admin role",
            details={"required_role": "admin"},
        )

    # Validate the new state
    if new_state not in VALID_STATES:
        raise invalid_request(
            message=f"Invalid state '{new_state}'. Must be one of: {', '.join(sorted(VALID_STATES))}",
            details={"valid_states": sorted(VALID_STATES), "provided": new_state},
        )

    # Ensure the service is configured
    if _feature_flag_service is None:
        from errors.exceptions import elasticsearch_unavailable
        raise elasticsearch_unavailable(
            message="Feature flag service not configured",
            details={"service": "feature_flag_service"},
        )

    # Set the overlay state — returns the previous state
    previous_state = await _feature_flag_service.set_overlay_state(
        ORDER_INTAKE_PIPELINE_FLAG_KEY,
        tenant_id,
        new_state,
        tenant.user_id,
    )

    logger.info(
        "Order intake pipeline flag changed: tenant_id=%s, "
        "previous=%s, new=%s, user_id=%s",
        tenant_id,
        previous_state,
        new_state,
        tenant.user_id,
    )

    # Broadcast the flag change to all active WS clients so UIs
    # update without a refresh.
    ws_notified = 0
    if _orders_ws_manager is not None:
        try:
            await _orders_ws_manager.broadcast(
                event_type="feature_flag_changed",
                data={
                    "flag_key": ORDER_INTAKE_PIPELINE_FLAG_KEY,
                    "tenant_id": tenant_id,
                    "previous_state": previous_state,
                    "new_state": new_state,
                },
                tenant_id=tenant_id,
            )
            ws_notified = 1
        except Exception as exc:
            logger.warning(
                "Failed to broadcast flag change to WS clients for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )

    return {
        "data": {
            "tenant_id": tenant_id,
            "flag_key": ORDER_INTAKE_PIPELINE_FLAG_KEY,
            "previous_state": previous_state,
            "new_state": new_state,
            "ws_broadcast": ws_notified > 0,
        },
        "request_id": getattr(request.state, "request_id", ""),
    }


__all__ = [
    "router",
    "configure_feature_flag_admin",
    "ORDER_INTAKE_PIPELINE_FLAG_KEY",
    "VALID_STATES",
]
