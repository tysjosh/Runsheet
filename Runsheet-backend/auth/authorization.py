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
"""

from errors.exceptions import forbidden, insufficient_role
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
