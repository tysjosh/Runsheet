"""Structured ``portal_audit`` log lines (design §8.1, R8).

One line per matched request, written by :class:`PortalAuditMiddleware`, plus
extra lines for events with no request of their own (the central deny, the
payment reconciler). Records go through ``logging.getLogger("portal_audit")``
with the ``extra={"extra_data": {...}}`` convention of
``services/csv_export.py``.

Values are restricted to ids, enums and path templates. The field names are a
fixed allowlist, and any string value that contains ``@`` or whitespace is
redacted, so an email or free text can never reach the line (R8.2).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("portal_audit")

#: Every field a ``portal_audit`` record may carry.
AUDIT_FIELDS: frozenset[str] = frozenset(
    {
        "portal_audit",
        "timestamp",
        "tenant_id",
        "actor_user_id",
        "customer_id",
        "action",
        "target_ids",
        "outcome",
        "request_id",
        # Optional extras (``**extra_ids``)
        "channel",
        "error_code",
        "method",
        "path_template",
        "status_code",
        "reason",
        "payment_attempt_id",
        "grant_id",
        "order_id",
        "invoice_id",
    }
)

_REDACTED = "{redacted}"
_UNSAFE = re.compile(r"[@\s]")


def _safe(value: Any) -> Any:
    if isinstance(value, str) and _UNSAFE.search(value):
        return _REDACTED
    return value


def emit_portal_audit(
    *,
    level: int,
    tenant_id: Optional[str],
    actor_user_id: Optional[str],
    customer_id: Optional[str],
    action: str,
    target_ids: Mapping[str, str],
    outcome: str,
    request_id: Optional[str],
    **extra_ids: Any,
) -> None:
    """Write one ``portal_audit`` record.

    Unknown ``extra_ids`` keys are dropped (the field allowlist is closed).
    """
    data: dict[str, Any] = {
        "portal_audit": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_id": _safe(tenant_id),
        "actor_user_id": _safe(actor_user_id),
        "customer_id": _safe(customer_id),
        "action": _safe(action),
        "target_ids": {
            str(k): _safe(v) for k, v in (target_ids or {}).items()
        },
        "outcome": _safe(outcome),
        "request_id": _safe(request_id),
    }
    for key, value in extra_ids.items():
        if key in AUDIT_FIELDS and key not in data:
            data[key] = _safe(value)
    logger.log(level, "portal_audit", extra={"extra_data": data})


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

_ADMIN_PATH = re.compile(r"^/api/commerce/customers/[^/]+/portal-users")
_PORTAL_PREFIX = "/api/portal/"


def is_audited_path(path: str) -> bool:
    """Paths that get exactly one audit line per request."""
    return path.startswith(_PORTAL_PREFIX) or bool(_ADMIN_PATH.match(path))


def outcome_for_status(status: int) -> str:
    """Map an HTTP status to the audit ``outcome`` enum."""
    if 200 <= status < 300:
        return "ok"
    if status >= 500:
        return "error"
    return {
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "invalid",
        429: "rate_limited",
    }.get(status, "invalid" if status == 400 else "error")


def _level_for_status(status: int) -> int:
    if 200 <= status < 300:
        return logging.INFO
    if status >= 500:
        return logging.ERROR
    return logging.WARNING


def _state(request: Request, *names: str) -> Optional[str]:
    for name in names:
        value = getattr(request.state, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def _audit_request(request: Request, status: int) -> None:
    route = request.scope.get("route")
    action = getattr(route, "name", None) or "unmatched"
    path_params = request.scope.get("path_params") or {}
    target_ids = {
        k: str(v) for k, v in path_params.items() if k.endswith("_id")
    }
    # Ids a handler created (e.g. the invite's grant_id), not in the path.
    extra_targets = getattr(request.state, "portal_audit_target_ids", None)
    if isinstance(extra_targets, dict):
        target_ids.update(
            {str(k): str(v) for k, v in extra_targets.items() if str(k).endswith("_id")}
        )
    emit_portal_audit(
        level=_level_for_status(status),
        tenant_id=_state(request, "portal_tenant_id", "tenant_id"),
        actor_user_id=_state(request, "portal_user_id", "auth_user_id"),
        customer_id=_state(request, "portal_customer_id"),
        action=action,
        target_ids=target_ids,
        outcome=outcome_for_status(status),
        request_id=_state(request, "request_id"),
        method=request.method,
        status_code=status,
        path_template=getattr(route, "path", None) or "unmatched",
    )


class PortalAuditMiddleware(BaseHTTPMiddleware):
    """One ``portal_audit`` line per portal / portal-admin request.

    Registered in ``main.py`` immediately before the auth gate, so it sits
    inside the auth gate and inside RequestID (``request_id`` is set). It also
    sets ``Cache-Control: no-store`` on every matched response.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or not is_audited_path(path):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            _audit_request(request, 500)
            raise
        _audit_request(request, response.status_code)
        response.headers["Cache-Control"] = "no-store"
        return response


__all__ = [
    "AUDIT_FIELDS",
    "PortalAuditMiddleware",
    "emit_portal_audit",
    "is_audited_path",
    "outcome_for_status",
]
