"""
Unit tests for the Role_Authorizer (``auth/authorization.py``).

Covers:
- ``require_role`` grants access on exact role membership
- ``require_role`` rejects (403) when the required role is absent
- exact-match only — superstrings like ``admin_ops`` never satisfy ``admin``
  and substrings like ``admin`` never satisfy a held ``admin_ops`` requirement
- multiple allowed roles: holding any one grants access
- non-string / empty / None role lists are handled safely
- ``require_pii_access`` permits iff ``has_pii_access`` is true
- raised exceptions carry HTTP 403 status and do not leak the held role list

Validates: Requirements 4.1, 4.2, 4.3, 4.5
"""

import pytest

from auth.authorization import require_pii_access, require_role
from errors.codes import ErrorCode
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext


def _ctx(roles=None, has_pii_access=False) -> TenantContext:
    """Build a minimal TenantContext for authorization tests."""
    return TenantContext(
        tenant_id="tenant-a",
        user_id="user-1",
        has_pii_access=has_pii_access,
        roles=list(roles) if roles is not None else [],
    )


# ---------------------------------------------------------------------------
# require_role — grant path
# ---------------------------------------------------------------------------


def test_require_role_grants_on_exact_membership():
    """A held role that exactly matches the requirement grants access."""
    require_role(_ctx(roles=["admin"]), "admin")  # no exception


def test_require_role_grants_when_any_allowed_role_held():
    """Holding any one of several allowed roles grants access."""
    require_role(_ctx(roles=["dispatcher"]), "admin", "dispatcher")  # no exception


def test_require_role_grants_with_extra_unrelated_roles():
    """Extra roles beyond the requirement do not block access."""
    require_role(_ctx(roles=["driver", "ops_manager", "admin"]), "admin")


# ---------------------------------------------------------------------------
# require_role — reject path (exact match, never substring)
# ---------------------------------------------------------------------------


def test_require_role_rejects_when_role_absent():
    """A context lacking the required role is rejected with 403."""
    with pytest.raises(AppException) as exc_info:
        require_role(_ctx(roles=["driver"]), "admin")
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == ErrorCode.INSUFFICIENT_ROLE


def test_require_role_superstring_does_not_satisfy():
    """A held 'admin_ops' must NOT satisfy a requirement for 'admin' (Req 4.2)."""
    with pytest.raises(AppException) as exc_info:
        require_role(_ctx(roles=["admin_ops"]), "admin")
    assert exc_info.value.status_code == 403


def test_require_role_substring_requirement_not_satisfied_by_longer_role():
    """Requiring 'admin_ops' is not satisfied by a held 'admin'."""
    with pytest.raises(AppException):
        require_role(_ctx(roles=["admin"]), "admin_ops")


def test_require_role_is_case_sensitive():
    """Exact matching is case-sensitive: 'Admin' does not satisfy 'admin'."""
    with pytest.raises(AppException):
        require_role(_ctx(roles=["Admin"]), "admin")


def test_require_role_rejects_empty_role_list():
    """A context with no roles is rejected."""
    with pytest.raises(AppException) as exc_info:
        require_role(_ctx(roles=[]), "admin")
    assert exc_info.value.status_code == 403


def test_require_role_handles_none_roles():
    """A context whose roles attribute is None is rejected, not crashed."""
    ctx = _ctx()
    ctx.roles = None  # type: ignore[assignment]
    with pytest.raises(AppException):
        require_role(ctx, "admin")


def test_require_role_ignores_non_string_roles():
    """Non-string entries in the roles list are ignored, not matched."""
    ctx = _ctx()
    ctx.roles = [None, 123, ["admin"]]  # type: ignore[list-item]
    with pytest.raises(AppException):
        require_role(ctx, "admin")


def test_require_role_does_not_leak_held_roles():
    """The 403 payload exposes only the requirement, never the held roles."""
    with pytest.raises(AppException) as exc_info:
        require_role(_ctx(roles=["secret_internal_role"]), "admin")
    details = exc_info.value.details or {}
    assert details.get("required_roles") == ["admin"]
    # The caller's actual roles must not appear anywhere in the payload.
    assert "secret_internal_role" not in str(details)


# ---------------------------------------------------------------------------
# require_pii_access
# ---------------------------------------------------------------------------


def test_require_pii_access_permits_when_flag_true():
    """Access is permitted when has_pii_access is true."""
    require_pii_access(_ctx(has_pii_access=True))  # no exception


def test_require_pii_access_rejects_when_flag_false():
    """Access is rejected with 403 when has_pii_access is false."""
    with pytest.raises(AppException) as exc_info:
        require_pii_access(_ctx(has_pii_access=False))
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == ErrorCode.FORBIDDEN
