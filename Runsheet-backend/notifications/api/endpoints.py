"""
Notification REST endpoints for the Customer Notification Pipeline.

Provides REST endpoints for querying notification history, managing
notification rules, customer preferences, and templates under the
``/api/notifications`` prefix.

Uses a ``configure_notification_endpoints()`` function to wire service
dependencies at startup (same pattern as agent_endpoints and scheduling
endpoints).

All endpoints are rate-limited and tenant-scoped via JWT.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 7.1, 7.2,
              4.2, 4.3, 4.4, 5.2, 5.3, 10.4
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from config.settings import get_settings
from errors.codes import ErrorCode
from errors.exceptions import (
    AppException,
    internal_error,
    resource_not_found,
    validation_error,
)
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_notification_rate = f"{_settings.ops_api_rate_limit}/minute"

# ---------------------------------------------------------------------------
# Module-level service references, wired via configure_notification_endpoints()
# ---------------------------------------------------------------------------

_notification_service = None
_rule_engine = None
_preference_resolver = None
_template_renderer = None

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# Auth policy declaration for this router
# Default: JWT_REQUIRED for all notification endpoints
ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RuleUpdateRequest(BaseModel):
    """Body for the PATCH notification rule endpoint."""

    enabled: Optional[bool] = Field(None, description="Whether the rule is enabled")
    default_channels: Optional[list[str]] = Field(
        None, description="Default channels for the rule"
    )
    template_id: Optional[str] = Field(
        None, description="Template ID associated with the rule"
    )


class PreferenceUpsertRequest(BaseModel):
    """Body for the PUT customer preference endpoint."""

    customer_name: Optional[str] = Field(None, description="Customer display name")
    channels: Optional[dict[str, str]] = Field(
        None,
        description="Map of channel name to contact detail, e.g. {'sms': '+254...'}",
    )
    event_preferences: Optional[list[dict]] = Field(
        None,
        description="List of event preferences with event_type and enabled_channels",
    )


class TemplateUpdateRequest(BaseModel):
    """Body for the PUT template endpoint."""

    subject_template: Optional[str] = Field(
        None, description="Subject template string"
    )
    body_template: Optional[str] = Field(None, description="Body template string")


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_notification_endpoints(
    *,
    notification_service,
    rule_engine,
    preference_resolver,
    template_renderer,
) -> None:
    """
    Wire service dependencies into the notification endpoints module.

    Called once during application startup so that the router handlers
    can access shared services without circular imports.
    """
    global _notification_service, _rule_engine
    global _preference_resolver, _template_renderer

    _notification_service = notification_service
    _rule_engine = rule_engine
    _preference_resolver = preference_resolver
    _template_renderer = template_renderer


# ---------------------------------------------------------------------------
# Service accessors
# ---------------------------------------------------------------------------


def _get_notification_service():
    if _notification_service is None:
        raise RuntimeError(
            "Notification endpoints not configured. "
            "Call configure_notification_endpoints() during startup."
        )
    return _notification_service


def _get_rule_engine():
    if _rule_engine is None:
        raise RuntimeError(
            "Notification endpoints not configured. "
            "Call configure_notification_endpoints() during startup."
        )
    return _rule_engine


def _get_preference_resolver():
    if _preference_resolver is None:
        raise RuntimeError(
            "Notification endpoints not configured. "
            "Call configure_notification_endpoints() during startup."
        )
    return _preference_resolver


def _get_template_renderer():
    if _template_renderer is None:
        raise RuntimeError(
            "Notification endpoints not configured. "
            "Call configure_notification_endpoints() during startup."
        )
    return _template_renderer


# ===================================================================
# Notification History Endpoints
# Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 10.4
# ===================================================================


@router.get("")
@limiter.limit(_notification_rate)
async def list_notifications(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    notification_type: Optional[str] = Query(None, description="Filter by notification type"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
    delivery_status: Optional[str] = Query(None, description="Filter by delivery status"),
    related_entity_id: Optional[str] = Query(None, description="Filter by related entity ID"),
    recipient_reference: Optional[str] = Query(None, description="Filter by recipient reference"),
    start_date: Optional[str] = Query(None, description="Start of date range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of date range (ISO 8601)"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """
    Paginated list of notifications with optional filters.

    Validates: Requirements 6.1, 6.7, 6.8, 10.4
    """
    svc = _get_notification_service()
    try:
        filters = {}
        if notification_type:
            filters["notification_type"] = notification_type
        if channel:
            filters["channel"] = channel
        if delivery_status:
            filters["delivery_status"] = delivery_status
        if related_entity_id:
            filters["related_entity_id"] = related_entity_id
        if recipient_reference:
            filters["recipient_reference"] = recipient_reference
        if start_date:
            filters["start_date"] = start_date
        if end_date:
            filters["end_date"] = end_date

        result = await svc.list_notifications(
            tenant_id=tenant.tenant_id,
            filters=filters,
            page=page,
            size=size,
        )
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to list notifications: {e}")
        raise internal_error(
            message="Failed to list notifications",
            details={"error": str(e)},
        )


@router.get("/summary")
@limiter.limit(_notification_rate)
async def get_notification_summary(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    start_date: Optional[str] = Query(None, description="Start of date range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of date range (ISO 8601)"),
):
    """
    Aggregate notification counts by type, channel, and status.

    Validates: Requirement 6.5
    """
    svc = _get_notification_service()
    try:
        result = await svc.get_summary(
            tenant_id=tenant.tenant_id,
            start_date=start_date,
            end_date=end_date,
        )
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to get notification summary: {e}")
        raise internal_error(
            message="Failed to get notification summary",
            details={"error": str(e)},
        )


# ===================================================================
# Notification Rules Endpoints
# Requirements: 7.1, 7.2
# NOTE: These MUST be defined before /{notification_id} to avoid
#       FastAPI matching "/rules" as a notification_id parameter.
# ===================================================================


@router.get("/rules")
@limiter.limit(_notification_rate)
async def list_rules(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    List all notification rules for the tenant.

    Validates: Requirement 7.1
    """
    svc = _get_rule_engine()
    try:
        result = await svc.list_rules(tenant.tenant_id)
        return {"items": result}
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to list notification rules: {e}")
        raise internal_error(
            message="Failed to list notification rules",
            details={"error": str(e)},
        )


@router.patch("/rules/{rule_id}")
@limiter.limit(_notification_rate)
async def update_rule(
    rule_id: str,
    body: RuleUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Update a notification rule (enabled, default_channels, template_id).

    Validates: Requirement 7.2
    """
    svc = _get_rule_engine()
    try:
        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise validation_error(
                "No fields to update",
                details={"hint": "Provide at least one of: enabled, default_channels, template_id"},
            )
        result = await svc.update_rule(rule_id, tenant.tenant_id, updates)
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to update notification rule {rule_id}: {e}")
        raise internal_error(
            message="Failed to update notification rule",
            details={"rule_id": rule_id, "error": str(e)},
        )


# ===================================================================
# Customer Preferences Endpoints
# Requirements: 4.2, 4.3, 4.4
# ===================================================================


@router.get("/preferences")
@limiter.limit(_notification_rate)
async def list_preferences(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    search: Optional[str] = Query(None, description="Search by customer name"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """
    Paginated list of customer notification preferences.

    Validates: Requirement 4.2
    """
    svc = _get_preference_resolver()
    try:
        result = await svc.list_preferences(
            tenant_id=tenant.tenant_id,
            page=page,
            size=size,
            search=search,
        )
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to list notification preferences: {e}")
        raise internal_error(
            message="Failed to list notification preferences",
            details={"error": str(e)},
        )


@router.get("/preferences/{customer_id}")
@limiter.limit(_notification_rate)
async def get_preference(
    customer_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Get notification preference for a specific customer.

    Validates: Requirement 4.3
    """
    svc = _get_preference_resolver()
    try:
        result = await svc.get_preference(customer_id, tenant.tenant_id)
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to get preference for customer {customer_id}: {e}")
        raise internal_error(
            message="Failed to get notification preference",
            details={"customer_id": customer_id, "error": str(e)},
        )


@router.put("/preferences/{customer_id}")
@limiter.limit(_notification_rate)
async def upsert_preference(
    customer_id: str,
    body: PreferenceUpsertRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Create or update notification preference for a customer.

    Validates: Requirement 4.4
    """
    svc = _get_preference_resolver()
    try:
        data = body.model_dump(exclude_none=True)
        result = await svc.upsert_preference(customer_id, tenant.tenant_id, data)
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to upsert preference for customer {customer_id}: {e}")
        raise internal_error(
            message="Failed to upsert notification preference",
            details={"customer_id": customer_id, "error": str(e)},
        )


# ===================================================================
# Template Endpoints
# Requirements: 5.2, 5.3
# ===================================================================


@router.get("/templates")
@limiter.limit(_notification_rate)
async def list_templates(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
):
    """
    List notification templates, optionally filtered by event_type and channel.

    Validates: Requirement 5.2
    """
    svc = _get_template_renderer()
    try:
        result = await svc.list_templates(
            tenant_id=tenant.tenant_id,
            event_type=event_type,
            channel=channel,
        )
        return {"items": result}
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to list notification templates: {e}")
        raise internal_error(
            message="Failed to list notification templates",
            details={"error": str(e)},
        )


@router.put("/templates/{template_id}")
@limiter.limit(_notification_rate)
async def update_template(
    template_id: str,
    body: TemplateUpdateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Update a notification template (subject_template, body_template).

    Validates: Requirement 5.3
    """
    svc = _get_template_renderer()
    try:
        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise validation_error(
                "No fields to update",
                details={"hint": "Provide at least one of: subject_template, body_template"},
            )
        result = await svc.update_template(template_id, tenant.tenant_id, updates)
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to update notification template {template_id}: {e}")
        raise internal_error(
            message="Failed to update notification template",
            details={"template_id": template_id, "error": str(e)},
        )


# ===================================================================
# Single Notification Endpoints (MUST be last — catch-all path param)
# Requirements: 6.2, 6.3, 6.4
# ===================================================================


@router.get("/{notification_id}")
@limiter.limit(_notification_rate)
async def get_notification(
    notification_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Get a single notification with full audit trail.

    Validates: Requirement 6.2
    """
    svc = _get_notification_service()
    try:
        result = await svc.get_notification(notification_id, tenant.tenant_id)
        return result
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Failed to get notification {notification_id}: {e}")
        raise internal_error(
            message="Failed to get notification",
            details={"notification_id": notification_id, "error": str(e)},
        )


@router.post("/{notification_id}/retry")
@limiter.limit(_notification_rate)
async def retry_notification(
    notification_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Retry a failed notification.

    Returns 409 if the notification is not in ``failed`` status.

    Validates: Requirements 6.3, 6.4
    """
    svc = _get_notification_service()
    try:
        result = await svc.retry_notification(notification_id, tenant.tenant_id)
        return result
    except AppException as e:
        # The service raises validation_error for non-retryable state.
        # Re-raise with 409 status for the retry-specific case.
        if "not in a retryable state" in str(e.message):
            raise AppException(
                error_code=ErrorCode.VALIDATION_ERROR,
                message=e.message,
                status_code=409,
                details=e.details,
            )
        raise
    except Exception as e:
        logger.error(f"Failed to retry notification {notification_id}: {e}")
        raise internal_error(
            message="Failed to retry notification",
            details={"notification_id": notification_id, "error": str(e)},
        )
