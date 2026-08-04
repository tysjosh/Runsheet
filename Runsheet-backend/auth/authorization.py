"""
Role_Authorizer — the single shared authorization helper.

This module replaces the inconsistent per-router role checks (the exact-match
``_require_admin`` / ``_require_admin_role`` / ``_require_write_role`` helpers
and the over-permissive substring-match ``_ensure_storm_mode_override_role``)
with one consistent mechanism.

Authorization decisions are made against the verified ``TenantContext``
(the ``Auth_Context`` of the requirements glossary), whose ``roles`` and
``has_pii_access`` fields are derived exclusively from the verified session.

Design: see ``.kiro/specs/supertokens-auth-migration/design.md`` §Role_Authorizer.

Validates: Requirements 4.1, 4.2, 4.3, 4.5
- 4.1: a single shared mechanism for verifying a required role
- 4.2: exact role-name matching, never substring matching
- 4.3: reject with an authorization error (403) when the role is absent
- 4.5: gate PII access on the ``has_pii_access`` flag

The driver surface adds one composed gate on top of the same mechanism.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Driver-surface guards.

Validates: Requirements 1.5, 1.6, 15.14
- 1.5: require the exact role ``driver`` on every ``/api/driver`` endpoint
- 1.6: reject with 403 ``DRIVER_IDENTITY_MISSING`` when the context has no
  ``driver_id``
- 15.14: rejections echo only the requirement, never held roles or identities
"""

from errors.exceptions import driver_identity_missing, forbidden, insufficient_role
from ops.middleware.tenant_guard import TenantContext


def require_role(tenant: TenantContext, *allowed: str) -> None:
    """Exact-match role gate shared across every router.

    Grants access if and only if the ``TenantContext`` holds at least one of
    the ``allowed`` role names by **exact** membership. Matching is never
    substring-based: a held role of ``admin_ops`` does NOT satisfy a
    requirement for ``admin`` (Req 4.2). Raises an HTTP 403 authorization
    error when none of the required roles is present (Req 4.3).

    Args:
        tenant: The verified Auth_Context for the request.
        *allowed: One or more role names; holding any one grants access.

    Raises:
        AppException: ``insufficient_role`` (HTTP 403) when the context holds
            none of the required roles.

    Validates: Requirements 4.1, 4.2, 4.3
    """
    held = {role for role in (tenant.roles or []) if isinstance(role, str)}
    if held.isdisjoint(allowed):
        # Do not echo the caller's held roles back — that would leak the
        # tenant's role lexicon to a probing attacker. Only the requirement
        # is surfaced.
        raise insufficient_role(
            message="Caller lacks a required role for this operation",
            details={"required_roles": list(allowed)},
        )


def require_pii_access(tenant: TenantContext) -> None:
    """Gate access to personally identifiable information.

    Permits the operation if and only if the verified ``TenantContext`` carries
    ``has_pii_access`` truthy. Raises an HTTP 403 authorization error otherwise
    (Req 4.5).

    Args:
        tenant: The verified Auth_Context for the request.

    Raises:
        AppException: ``forbidden`` (HTTP 403) when ``has_pii_access`` is false.

    Validates: Requirements 4.5
    """
    if not tenant.has_pii_access:
        raise forbidden(
            message="PII access is required for this operation",
            details={"required": "has_pii_access"},
        )


def require_driver_identity(tenant: TenantContext) -> str:
    """Gate a driver surface and return the caller's canonical ``driver_id``.

    The single entry point for every ``/api/driver/*`` handler, called as the
    handler's first statement. It composes the two checks that a driver surface
    always needs:

    1. the exact role ``driver`` via :func:`require_role` — HTTP 403
       ``INSUFFICIENT_ROLE`` when absent (Req 1.5);
    2. a canonical driver identity on the verified context — HTTP 403
       ``DRIVER_IDENTITY_MISSING`` when ``driver_id`` is falsy (Req 1.6).

    The returned value is the ``drivers_current.driver_id`` carried by the
    verified session claim, never anything the client asserted, so handlers pass
    it straight into their services instead of reading an identifier off the
    request.

    Rejections carry only the requirement (``required_roles`` from
    :func:`require_role`, or no identity detail at all): never the caller's held
    roles, and never the identity of the driver a resource is assigned to
    (Req 15.14).

    Args:
        tenant: The verified Auth_Context for the request.

    Returns:
        The caller's canonical driver identifier.

    Raises:
        AppException: ``insufficient_role`` (HTTP 403) when the ``driver`` role
            is absent, or ``driver_identity_missing`` (HTTP 403) when the
            context carries no ``driver_id``.

    Validates: Requirements 1.5, 1.6, 15.14
    """
    require_role(tenant, "driver")
    if not tenant.driver_id:
        raise driver_identity_missing()
    return tenant.driver_id
