"""Dispatch Board REST surface: ``/api/fuel/board`` (design K11).

Every route declares ``dependencies=_guards(strict)``: the board flag guard
first, then ``roles_dependency("admin", "dispatcher")``. FastAPI resolves a
route's dependency list in order, so a user of any role in a ``disabled``
tenant gets 404, never 403 (``auth/router_guards.py``, K11.1).

* ``board_mode_guard(strict=False)`` (reads): ``get_overlay_state``; disabled,
  unset or unreadable is 404 ``DISPATCH_BOARD_DISABLED``.
* ``board_mode_guard(strict=True)`` (writes): ``get_overlay_state_strict``;
  unset is 404, a store error is 503 ``DISPATCH_BOARD_MODE_UNAVAILABLE``,
  ``shadow`` is 409 ``DISPATCH_BOARD_READ_ONLY``.

Bodies are ``Body(Any)`` and validated in the handler with ``parse_body`` so a
422 never echoes submitted values (External input validation). Tenant and
actor ids come only from the verified session.

Publish answers 200 for a dry run and 202 when a publish is accepted (K7.1).
Publish and suggestion reject (Phase 3) parse and validate their bodies here;
until their services are configured they answer 503.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Callable, List, Optional

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from auth.router_guards import roles_dependency
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services.dispatch_board_models import (
    ID_PATTERN,
    CommandBody,
    HistoryQuery,
    RejectSuggestionBody,
    ValidateBody,
    invalid,
    parse_body,
    parse_filter_params,
    parse_lanes_param,
    publish_body_model,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

ROUTER_AUTH_POLICY = "jwt_required"
#: Overlay-state store key for the board flag (K13).
BOARD_FLAG_KEY = "dispatch_board"
BOARD_ROLES = ("admin", "dispatcher")

router = APIRouter(prefix="/api/fuel/board", tags=["dispatch-board"])

_board_service: Optional[Any] = None
_feature_flags: Optional[Any] = None
_publish_service: Optional[Any] = None
_suggestion_service: Optional[Any] = None

_ID_RE = re.compile(ID_PATTERN)


def configure_dispatch_board_endpoints(
    *,
    board_service: Any,
    feature_flag_service: Any,
    publish_service: Any = None,
    suggestion_service: Any = None,
) -> None:
    """Wire the board services (called from ``bootstrap/agents.py``)."""
    global _board_service, _feature_flags, _publish_service, _suggestion_service
    _board_service = board_service
    _feature_flags = feature_flag_service
    _publish_service = publish_service
    _suggestion_service = suggestion_service


def configured_board_service() -> Optional[Any]:
    return _board_service


def _disabled() -> AppException:
    return AppException(ErrorCode.DISPATCH_BOARD_DISABLED, "Dispatch Board is not enabled.", status_code=404)


def board_mode_guard(strict: bool) -> Callable[..., Any]:
    """K11.1 flag guard; stores the mode on ``request.state.board_mode``."""

    async def _guard(request: Request, tenant: TenantContext = Depends(get_tenant_context)) -> str:
        flags = _feature_flags
        if strict:
            if flags is None:
                raise AppException(
                    ErrorCode.DISPATCH_BOARD_MODE_UNAVAILABLE,
                    "Dispatch Board is unavailable. Retry shortly.",
                    status_code=503,
                )
            try:
                state = await flags.get_overlay_state_strict(BOARD_FLAG_KEY, tenant.tenant_id)
            except Exception as exc:
                logger.warning("dispatch board flag read failed: %s", type(exc).__name__)
                raise AppException(
                    ErrorCode.DISPATCH_BOARD_MODE_UNAVAILABLE,
                    "Dispatch Board is unavailable. Retry shortly.",
                    status_code=503,
                ) from None
            state = state or "disabled"
            if state == "disabled":
                raise _disabled()
            if state == "shadow":
                raise AppException(
                    ErrorCode.DISPATCH_BOARD_READ_ONLY,
                    "Preview mode, changes are not saved.",
                    status_code=409,
                    details={"reason": "shadow"},
                )
        else:
            state = "disabled"
            if flags is not None:
                try:
                    state = await flags.get_overlay_state(BOARD_FLAG_KEY, tenant.tenant_id)
                except Exception as exc:
                    logger.warning("dispatch board flag read failed: %s", type(exc).__name__)
                    state = "disabled"
            if state not in ("shadow", "active_gated", "active_auto"):
                raise _disabled()
        request.state.board_mode = state
        return state

    _guard.__name__ = f"board_mode_guard_{'strict' if strict else 'read'}"
    return _guard


def _guards(strict: bool) -> List[Any]:
    """Flag guard before role guard (K11.1)."""
    return [Depends(board_mode_guard(strict)), Depends(roles_dependency(*BOARD_ROLES))]


def _service() -> Any:
    if _board_service is None:
        raise AppException(
            ErrorCode.ELASTICSEARCH_UNAVAILABLE,
            "Dispatch Board is not available.",
            status_code=503,
            details={"reason": "service_not_configured"},
        )
    return _board_service


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise invalid("invalid_date", fields=["service_date"]) from None


def _check_id(raw: str, field: str) -> str:
    if not _ID_RE.match(raw or ""):
        raise invalid("invalid_id", fields=[field])
    return raw


def _tz(tenant: TenantContext) -> str:
    return _service().timezone_for(tenant.tenant_id, getattr(tenant, "settings", None))


def _envelope(request: Request, data: Any) -> dict:
    return {"data": data, "request_id": getattr(request.state, "request_id", "")}


@router.get("/status", dependencies=_guards(False))
async def get_board_status(request: Request) -> dict:
    """Mode for tab visibility (R1). 404 when disabled, 403 for other roles."""
    return _envelope(request, {"mode": request.state.board_mode})


@router.get("/{service_date}", dependencies=_guards(False))
async def get_board(
    service_date: str,
    request: Request,
    lanes: Optional[str] = None,
    call_type: Optional[str] = None,
    product: Optional[str] = None,
    window: Optional[str] = None,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Board snapshot (K5)."""
    day = _parse_date(service_date)
    lane_ids = parse_lanes_param(lanes)
    filters = parse_filter_params(call_type, product, window)
    data = await _service().snapshot(
        tenant.tenant_id,
        day,
        mode=request.state.board_mode,
        tz=_tz(tenant),
        lanes=lane_ids,
        filters=filters,
    )
    return _envelope(request, data)


@router.post("/{service_date}/validate", dependencies=_guards(False))
async def validate_board(
    service_date: str,
    request: Request,
    body: Any = Body(None),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Drag-time dry run (K3.5)."""
    day = _parse_date(service_date)
    parsed = parse_body(ValidateBody, body)
    data = await _service().validate(tenant.tenant_id, day, parsed, tz=_tz(tenant))
    return _envelope(request, data)


@router.post("/{service_date}/commands", dependencies=_guards(True))
async def send_board_command(
    service_date: str,
    request: Request,
    body: Any = Body(None),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """One command (K4)."""
    day = _parse_date(service_date)
    command = parse_body(CommandBody, body).root
    data = await _service().handle_command(
        tenant_id=tenant.tenant_id,
        user_id=tenant.user_id,
        service_date=day,
        command=command,
        tz=_tz(tenant),
    )
    return _envelope(request, data)


@router.post("/{service_date}/publish", dependencies=_guards(True))
async def publish_board(
    service_date: str,
    request: Request,
    body: Any = Body(None),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Publish, or with ``dry_run: true`` the review-dialog preview (K7.1)."""
    day = _parse_date(service_date)
    model = publish_body_model(body)
    parsed = parse_body(model, body)
    if _publish_service is None:
        raise AppException(
            ErrorCode.ELASTICSEARCH_UNAVAILABLE,
            "Publishing is not available yet.",
            status_code=503,
            details={"reason": "service_not_configured"},
        )
    data = await _publish_service.handle(
        tenant_id=tenant.tenant_id,
        user_id=tenant.user_id,
        service_date=day,
        body=parsed,
        tz=_tz(tenant),
    )
    if isinstance(data, dict) and data.get("publish_id"):
        # Accepted: the worker runs in the background (K7.1).
        return JSONResponse(status_code=202, content=_envelope(request, data))
    return _envelope(request, data)


@router.get("/{service_date}/publish/{publish_id}", dependencies=_guards(False))
async def get_publish(
    service_date: str,
    publish_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Publish status (K7.1)."""
    day = _parse_date(service_date)
    _check_id(publish_id, "publish_id")
    if _publish_service is None:
        raise AppException(ErrorCode.RESOURCE_NOT_FOUND, "Publish not found.", status_code=404)
    data = await _publish_service.status(tenant_id=tenant.tenant_id, service_date=day, publish_id=publish_id)
    return _envelope(request, data)


@router.post("/{service_date}/suggestions/{plan_id}/reject", dependencies=_guards(True))
async def reject_suggestion(
    service_date: str,
    plan_id: str,
    request: Request,
    body: Any = Body(None),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Reject an agent suggestion (K9)."""
    day = _parse_date(service_date)
    _check_id(plan_id, "plan_id")
    parsed = parse_body(RejectSuggestionBody, body if body is not None else {})
    if _suggestion_service is None:
        raise AppException(
            ErrorCode.ELASTICSEARCH_UNAVAILABLE,
            "Suggestions are not available yet.",
            status_code=503,
            details={"reason": "service_not_configured"},
        )
    data = await _suggestion_service.reject(
        tenant_id=tenant.tenant_id, user_id=tenant.user_id, service_date=day, plan_id=plan_id, reason=parsed.reason
    )
    return _envelope(request, data)


@router.get("/{service_date}/history", dependencies=_guards(False))
async def get_board_history(
    service_date: str,
    request: Request,
    truck_id: Optional[str] = None,
    cursor: Optional[str] = None,
    size: Optional[str] = None,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Command log for the History tab (R22.3), newest first."""
    day = _parse_date(service_date)
    raw = {k: v for k, v in {"truck_id": truck_id, "cursor": cursor, "size": size}.items() if v is not None}
    query = parse_body(HistoryQuery, raw)
    data = await _service().history(tenant.tenant_id, day, query, tz=_tz(tenant))
    return _envelope(request, data)


__all__ = [
    "router",
    "configure_dispatch_board_endpoints",
    "configured_board_service",
    "board_mode_guard",
    "BOARD_FLAG_KEY",
    "ROUTER_AUTH_POLICY",
]
