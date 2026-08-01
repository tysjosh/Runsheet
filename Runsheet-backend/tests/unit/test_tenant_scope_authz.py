"""Cross-tenant authorization on endpoints that take ``tenant_id`` in the path.

Five admin endpoints accepted the target tenant as a path parameter and did not
verify the caller was entitled to act on it:

* ``GET/POST /api/ops/admin/feature-flags/{tenant_id}/order-intake-pipeline``
  checked ``"admin" in roles`` but never compared the path tenant to the
  caller's own, so a customer administrator could read and flip a *different*
  customer's order-intake kill switch.
* ``POST /api/ops/admin/feature-flags/{tenant_id}/enable|disable|rollback``
  had no authorization at all beyond being authenticated. Any caller — a driver
  included — could disable another tenant's Ops Intelligence Layer, forcibly
  disconnect their WebSocket clients, or with ``purge_data=true`` delete their
  ops data outright.

The tests below are written as attacks: each one is a request that used to
succeed and must now be refused. ``test_platform_admin_may_act_cross_tenant``
is the counterweight — without it, "deny everything" would pass.

Why ``admin`` cannot be the staff role: it is tenant-scoped, meaning
"administrator of my own company". Runsheet staff sign in through the same
application as customers, so expressing "may act outside my own tenant" needs a
separate role, which is why ``platform_admin`` was added to
``CANONICAL_ROLES``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pytest

from auth.supertokens_init import (
    CANONICAL_ROLES,
    CUSTOMER_ASSIGNABLE_ROLES,
    PLATFORM_ADMIN_ROLE,
)
from auth.tenant_scope import is_platform_admin, require_tenant_scope
from errors.exceptions import AppException, ErrorCode


@dataclass
class _Caller:
    """Minimal stand-in for ``TenantContext``."""

    tenant_id: str
    user_id: str = "user-1"
    roles: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The role vocabulary
# ---------------------------------------------------------------------------


def test_platform_admin_role_exists() -> None:
    assert PLATFORM_ADMIN_ROLE in CANONICAL_ROLES


def test_platform_admin_is_not_customer_assignable() -> None:
    """A tenant administrator must not be able to grant themselves staff rights.

    If ``platform_admin`` were customer-assignable, the cross-tenant boundary
    would be self-service and the fix below would be decorative.
    """
    assert PLATFORM_ADMIN_ROLE not in CUSTOMER_ASSIGNABLE_ROLES


def test_customer_assignable_roles_are_all_canonical() -> None:
    for role in CUSTOMER_ASSIGNABLE_ROLES:
        assert role in CANONICAL_ROLES


# ---------------------------------------------------------------------------
# The attacks
# ---------------------------------------------------------------------------


def test_tenant_admin_cannot_act_on_another_tenant() -> None:
    """The original hole: right role, wrong company."""
    caller = _Caller(tenant_id="acme", roles=["admin"])
    with pytest.raises(AppException) as exc:
        require_tenant_scope(caller, "globex", operation="Flipping a flag")
    assert exc.value.error_code == ErrorCode.FORBIDDEN
    # The message must name the path out, or an operator cannot self-diagnose.
    assert PLATFORM_ADMIN_ROLE in str(exc.value.details)


@pytest.mark.parametrize("role", ["driver", "dispatcher", "ops_manager"])
def test_non_admin_cannot_act_even_on_own_tenant(role: str) -> None:
    """The ops endpoints had no role check; a driver could call them."""
    caller = _Caller(tenant_id="acme", roles=[role])
    with pytest.raises(AppException) as exc:
        require_tenant_scope(caller, "acme", operation="Disabling the layer")
    assert exc.value.error_code == ErrorCode.INSUFFICIENT_ROLE


def test_caller_with_no_roles_is_denied() -> None:
    """Fail closed on a missing roles claim rather than treating it as empty-pass."""
    with pytest.raises(AppException):
        require_tenant_scope(_Caller(tenant_id="acme"), "acme")


def test_blank_target_tenant_is_denied_not_matched() -> None:
    """A blank path parameter must not authorize itself.

    Without this, a malformed route where ``tenant_id`` resolves to ``""``
    against a caller whose tenant is also falsy would compare equal and pass.
    """
    caller = _Caller(tenant_id="", roles=["admin"])
    with pytest.raises(AppException) as exc:
        require_tenant_scope(caller, "")
    assert exc.value.error_code == ErrorCode.FORBIDDEN


def test_role_check_precedes_tenant_check() -> None:
    """A driver aimed at another tenant fails on role, not on tenant.

    Ordering matters for the audit trail: INSUFFICIENT_ROLE says "wrong kind of
    user", FORBIDDEN says "right kind of user, wrong company". Conflating them
    hides deliberate cross-tenant probing among ordinary permission noise.
    """
    caller = _Caller(tenant_id="acme", roles=["driver"])
    with pytest.raises(AppException) as exc:
        require_tenant_scope(caller, "globex")
    assert exc.value.error_code == ErrorCode.INSUFFICIENT_ROLE


# ---------------------------------------------------------------------------
# The legitimate paths
# ---------------------------------------------------------------------------


def test_tenant_admin_may_act_on_own_tenant() -> None:
    require_tenant_scope(_Caller(tenant_id="acme", roles=["admin"]), "acme")


def test_platform_admin_may_act_cross_tenant() -> None:
    """Guards against over-tightening into "deny everything"."""
    caller = _Caller(tenant_id="runsheet", roles=[PLATFORM_ADMIN_ROLE])
    require_tenant_scope(caller, "globex", operation="Staff support action")


def test_platform_admin_satisfies_role_without_being_listed() -> None:
    """Staff need not be enumerated in every endpoint's required_roles."""
    caller = _Caller(tenant_id="runsheet", roles=[PLATFORM_ADMIN_ROLE])
    require_tenant_scope(caller, "acme", required_roles=("admin",))


def test_is_platform_admin_predicate() -> None:
    assert is_platform_admin(_Caller("t", roles=[PLATFORM_ADMIN_ROLE])) is True
    assert is_platform_admin(_Caller("t", roles=["admin"])) is False
    assert is_platform_admin(_Caller("t")) is False


# ---------------------------------------------------------------------------
# The endpoints actually call the guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path,expected_calls",
    [
        ("fuel.api.feature_flag_admin_endpoints", 2),
        ("ops.api.endpoints", 3),
    ],
)
def test_flag_endpoints_invoke_the_guard(module_path: str, expected_calls: int) -> None:
    """Every ``feature-flags/{tenant_id}`` handler must call the guard.

    A source assertion rather than a live request test: these handlers depend on
    a configured feature-flag service and a real session, and the regression
    being guarded is "somebody added another {tenant_id} endpoint and forgot".
    """
    import importlib
    import inspect

    module = importlib.import_module(module_path)
    source = inspect.getsource(module)
    assert source.count("require_tenant_scope(") >= expected_calls, (
        f"{module_path} calls require_tenant_scope fewer than {expected_calls} "
        f"times; a feature-flag endpoint taking tenant_id in the path is "
        f"probably unguarded."
    )


def test_purge_data_requires_platform_admin() -> None:
    """Data purge is irreversible, so a tenant admin cannot trigger it.

    Pins the asymmetry in the rollback handler: ``admin`` may roll back their
    own tenant, but only staff may delete the history.
    """
    import inspect

    from ops.api import endpoints

    source = inspect.getsource(endpoints.rollback_feature_flag)
    assert "platform_admin" in source
    assert "purge_data" in source
