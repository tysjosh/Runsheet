"""Authorization for endpoints acting on a caller-supplied tenant.

The problem this exists to prevent
----------------------------------

Several admin endpoints accept the target tenant in the URL rather than deriving
it from the caller's verified session::

    POST /api/ops/admin/feature-flags/{tenant_id}/rollback?purge_data=true

``tenant_id`` there is attacker-controlled. Deriving identity from the session
(as ``get_tenant_context`` does) is not sufficient on its own — the handler must
also assert that the caller is *allowed to act on the tenant named in the path*.
Five endpoints were missing that assertion:

* ``GET/POST /api/ops/admin/feature-flags/{tenant_id}/order-intake-pipeline``
  checked ``"admin" in roles`` but never compared the path tenant to the
  caller's, so a customer administrator could read and flip another customer's
  order-intake kill switch.
* ``POST /api/ops/admin/feature-flags/{tenant_id}/enable|disable|rollback``
  had no role check at all, so **any** authenticated caller — including a
  driver — could disable another tenant's Ops Intelligence Layer, forcibly
  disconnect their WebSocket clients, or, with ``purge_data=true``, delete their
  ops data.

The second shape: an identifier in the request body
---------------------------------------------------

The same hole also arrives without a ``tenant_id`` anywhere in the URL. When a
handler takes an identifier in the **request body** — typically a user's email —
and resolves it against ``auth_users`` with no tenant filter, it acts on
whichever row it finds, in whatever tenant. The rule is identical: the resolved
row's ``tenant_id`` must be the caller's unless they hold ``platform_admin``.
Two endpoints were fixed this way:

* ``POST /api/auth/admin/password-reset-link`` looked up ``body.email`` in
  ``auth_users`` unscoped, so a tenant-A ``admin`` could mint a working reset
  link for any provisioned account on the platform — including a
  ``platform_admin`` staff account — and take it over. That surface scopes the
  lookup rather than calling :func:`require_tenant_scope`, so an out-of-tenant
  email is reported as "not provisioned" and cannot be enumerated.
* ``POST /api/ops/drivers/{driver_id}/app-access`` read the row for
  ``body.email`` unscoped and then upserted ``ON CONFLICT (email) DO UPDATE SET
  tenant_id = EXCLUDED.tenant_id``, so the same caller could rewrite another
  tenant's user into their own tenant. It now compares the row's ``tenant_id``
  to the caller's — via :func:`is_platform_admin` rather than
  :func:`require_tenant_scope` — before any write, and reports a denial as the
  same conflict a legitimately unavailable email produces, again so the
  address cannot be probed.

The two use opposite mechanics for a reason. The reset link only *reads*, so
narrowing the query is enough — a scoped miss means nothing happens. The
app-access grant *writes* through ``ON CONFLICT (email)``, so narrowing its read
would hide the out-of-tenant row without stopping the write from landing on it;
there, authorization has to see the row the write will touch and refuse
explicitly.

Why a role and a tenant check, not one or the other
---------------------------------------------------

``admin`` is tenant-scoped: it means "administrator of my own company". Runsheet
staff sign in through the same application as customers, so the only way to
express "may act on a tenant that is not mine" is a distinct role,
:data:`~auth.supertokens_init.PLATFORM_ADMIN_ROLE`.

So the rule is: **the caller must hold one of the required roles, AND the target
tenant must be their own unless they are a platform admin.** Checking only the
role permits cross-tenant action; checking only the tenant permits any driver to
administer their own company.

Division of labour
------------------

:func:`auth.authorization.require_role` answers "may you do this at all";
:func:`require_tenant_scope` answers "may you do it to *this* tenant". The two
questions are separate, and so are the roles that answer them: a staff account
holds ``admin`` for the first and ``platform_admin`` for the second.
``require_role`` is exact-match with no implication graph, so ``platform_admin``
alone reaches nothing. See
:data:`~auth.supertokens_init.PLATFORM_STAFF_ROLES` for the bundle and why it is
two roles rather than one.

Fail closed
-----------

A missing or empty ``roles`` list denies. A blank target ``tenant_id`` denies
rather than matching a blank session tenant, so a malformed route — or a blank
scope derived from a body identifier — cannot authorize itself.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from auth.supertokens_init import PLATFORM_ADMIN_ROLE
from errors.exceptions import forbidden, insufficient_role

logger = logging.getLogger(__name__)

__all__ = [
    "is_platform_admin",
    "require_tenant_scope",
]


def _roles_of(tenant: Any) -> list[str]:
    """Roles from a ``TenantContext``-like object; empty when absent.

    Uses ``getattr`` because several call sites pass duck-typed test doubles
    rather than the real dataclass.
    """
    roles = getattr(tenant, "roles", None) or []
    return [r for r in roles if isinstance(r, str)]


def is_platform_admin(tenant: Any) -> bool:
    """True when the caller holds the Runsheet-staff role."""
    return PLATFORM_ADMIN_ROLE in _roles_of(tenant)


def require_tenant_scope(
    tenant: Any,
    target_tenant_id: Optional[str],
    *,
    required_roles: Sequence[str] = ("admin",),
    operation: str = "this operation",
) -> None:
    """Authorize acting on ``target_tenant_id``, or raise.

    Args:
        tenant: The verified ``TenantContext`` for the caller.
        target_tenant_id: The tenant named in the request path.
        required_roles: Any one of these satisfies the role requirement.
            ``platform_admin`` always satisfies it, so it need not be listed.
        operation: Human-readable operation name used in the error message.

    Raises:
        AppException: ``INSUFFICIENT_ROLE`` when the caller holds none of the
            required roles; ``FORBIDDEN`` when the caller is in-scope
            role-wise but is targeting a tenant other than their own without
            being a platform admin.

    The two failures are deliberately distinguishable: the first is "you are the
    wrong kind of user", the second is "you are the right kind of user pointed
    at the wrong company". Collapsing them would make the cross-tenant attempt
    indistinguishable from an ordinary permission error in the audit log.
    """
    roles = _roles_of(tenant)
    platform = PLATFORM_ADMIN_ROLE in roles

    if not platform and not any(r in roles for r in required_roles):
        raise insufficient_role(
            message=f"{operation} requires one of: {', '.join(required_roles)}",
            details={
                "required_roles": list(required_roles),
                "operation": operation,
            },
        )

    if platform:
        # Staff acting cross-tenant is legitimate, and worth a log line: it is
        # the one path that can touch another company's data.
        caller_tenant = getattr(tenant, "tenant_id", None)
        if target_tenant_id and target_tenant_id != caller_tenant:
            logger.info(
                "platform_admin cross-tenant action: user=%s home_tenant=%s "
                "target_tenant=%s operation=%s",
                getattr(tenant, "user_id", "?"),
                caller_tenant,
                target_tenant_id,
                operation,
            )
        return

    caller_tenant = getattr(tenant, "tenant_id", None)
    # Fail closed on a blank path parameter rather than letting "" == "" pass.
    if not target_tenant_id or not caller_tenant or target_tenant_id != caller_tenant:
        logger.warning(
            "cross-tenant attempt denied: user=%s home_tenant=%s "
            "target_tenant=%s operation=%s",
            getattr(tenant, "user_id", "?"),
            caller_tenant,
            target_tenant_id,
            operation,
        )
        raise forbidden(
            message=(
                f"{operation} is limited to your own tenant. Acting on another "
                f"tenant requires the {PLATFORM_ADMIN_ROLE} role."
            ),
            details={
                "operation": operation,
                "required_role_for_cross_tenant": PLATFORM_ADMIN_ROLE,
            },
        )
