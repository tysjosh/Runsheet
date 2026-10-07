"""The portal guard, scope, and rate-limit helpers (design §2.3, §8.2).

Every portal route:

* sits under a router created with ``dependencies=[Depends(reject_scope_params)]``;
* takes ``request: Request`` (slowapi requires it) and
  ``scope: PortalScope = Depends(require_portal_customer)``;
* carries a per-user limit keyed by :func:`portal_rate_key` (reads share the
  ``portal_read`` bucket).

``require_portal_customer`` runs, in order: the flag (404 ``PORTAL_DISABLED``),
the exact ``customer`` role (403 ``INSUFFICIENT_ROLE``), the identity shape
(403 ``PORTAL_IDENTITY_INVALID``), and the live-binding check (403
``PORTAL_ACCESS_SUSPENDED`` / 503 ``PORTAL_UNAVAILABLE``). ``get_tenant_context``
resolves first, so an unauthenticated caller still gets 401.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from auth.authorization import require_role
from config.settings import get_settings
from errors.codes import ErrorCode
from errors.exceptions import (
    AppException,
    portal_access_suspended,
    portal_disabled,
    portal_identity_invalid,
    portal_unavailable,
)
from middleware.rate_limiter import get_client_ip
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from portal.scope import PORTAL_ROLE, portal_enabled


@dataclass(frozen=True)
class PortalScope:
    """The verified (tenant, customer, user) a portal request acts for.

    Built only by :func:`require_portal_customer` from the verified session.
    An empty id here would silently widen a store filter, so construction
    refuses one.
    """

    tenant_id: str
    customer_id: str
    user_id: str
    #: For helpers that take a context (csv_export, the intake pipeline).
    tenant: TenantContext

    def __post_init__(self) -> None:
        for name in ("tenant_id", "customer_id", "user_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"PortalScope.{name} must be non-empty")


async def require_portal_customer(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> PortalScope:
    """Resolve the :class:`PortalScope` for a portal request (E4, E5, E6)."""
    # 1. Flag before role (R2.6): portal flag AND commerce backbone.
    if not portal_enabled(get_settings()):
        raise portal_disabled()

    # 2. Exact-match customer role (E5).
    require_role(tenant, PORTAL_ROLE)

    # 3. Identity shape. Normally unreachable after the central deny (E2).
    if list(tenant.roles or []) != [PORTAL_ROLE] or not tenant.customer_id:
        raise portal_identity_invalid()

    # 4. The binding is still live (E6). Fails closed when unwired.
    from portal.services.principal import get_principal_checker

    checker = get_principal_checker()
    if checker is None:
        raise portal_unavailable()
    verdict = await checker.check(tenant.tenant_id, tenant.user_id, tenant.customer_id)
    if verdict != "ok":
        raise portal_access_suspended()

    # 5. Stamps for the rate-limit key, the audit middleware, and csv_export.
    request.state.portal_tenant_id = tenant.tenant_id
    request.state.portal_user_id = tenant.user_id
    request.state.portal_customer_id = tenant.customer_id
    request.state.export_tenant_id = tenant.tenant_id
    request.state.export_user_id = tenant.user_id

    # 6.
    return PortalScope(
        tenant_id=tenant.tenant_id,
        customer_id=tenant.customer_id,
        user_id=tenant.user_id,
        tenant=tenant,
    )


#: Query parameters a portal caller may never supply (R2.7, E7).
_SCOPE_PARAMS = ("tenant_id", "customer_id")


async def reject_scope_params(request: Request) -> None:
    """Router dependency: 422 when ``tenant_id``/``customer_id`` is in the query."""
    present = [name for name in _SCOPE_PARAMS if name in request.query_params]
    if present:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "Scope parameters are not accepted on portal routes",
            status_code=422,
            details={"fields": present},
        )


# ---------------------------------------------------------------------------
# Rate limits (design §8.2)
# ---------------------------------------------------------------------------


def portal_rate_key(request: Request) -> str:
    """``portal:{tenant}:{user}`` once the guard has run, else the client IP."""
    tenant_id = getattr(request.state, "portal_tenant_id", None)
    user_id = getattr(request.state, "portal_user_id", None)
    if tenant_id and user_id:
        return f"portal:{tenant_id}:{user_id}"
    return get_client_ip(request)


_settings = get_settings()

#: Shared by every portal read route (``scope="portal_read"``).
PORTAL_READ_LIMIT: str = f"{_settings.portal_read_rate_limit}/minute"
PORTAL_READ_SCOPE: str = "portal_read"
PORTAL_ORDER_LIMIT: str = f"{_settings.portal_order_rate_limit}/minute"
PORTAL_CANCEL_LIMIT: str = f"{_settings.portal_order_cancel_rate_limit}/minute"
PORTAL_PAYMENT_LIMIT: str = f"{_settings.portal_payment_rate_limit}/minute"

del _settings


__all__ = [
    "PORTAL_CANCEL_LIMIT",
    "PORTAL_ORDER_LIMIT",
    "PORTAL_PAYMENT_LIMIT",
    "PORTAL_READ_LIMIT",
    "PORTAL_READ_SCOPE",
    "PortalScope",
    "portal_rate_key",
    "reject_scope_params",
    "require_portal_customer",
]
