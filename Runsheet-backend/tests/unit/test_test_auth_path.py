"""
Unit tests for the Test_Auth_Path guard (``auth/test_auth.py``).

These cover the three behaviours called out by task 5.2:

1. **Entry points raise in production** — every public entry point
   (:func:`issue_test_context`, :func:`override_auth`) raises
   :class:`TestAuthPathDisabledError` outside the test/development
   environments (most importantly ``production``, but also ``staging``), and
   :func:`is_test_auth_bypass_active` always returns ``False`` there so a stray
   bypass can never weaken production enforcement (Req 11.3).
2. **Produced context carries the requested identity** — the issued
   :class:`TenantContext` carries exactly the requested ``tenant_id`` / roles /
   ``has_pii_access`` (Req 11.1).
3. **Isolation helper scopes to one tenant** — :func:`override_auth` installs
   ``app.dependency_overrides[get_tenant_context]`` so a context scoped to one
   tenant resolves only that tenant's data, demonstrating the tenant-isolation
   assertion the path is built to support (Req 11.4).

The environment is the only external input the guard reads, so each test pins
it by patching ``auth.test_auth.get_settings`` (the symbol the module actually
calls) with the desired :class:`Environment`.

Validates: Requirements 11.1, 11.3, 11.4
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import auth.test_auth as test_auth
from auth.test_auth import (
    is_test_auth_bypass_active,
    issue_test_context,
    override_auth,
)
from config.settings import Environment
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

# Referenced via the module alias (``test_auth.TestAuthPathDisabledError``)
# rather than a module-level ``Test``-prefixed name so pytest does not try to
# collect the exception class as a test case.
_DisabledError = test_auth.TestAuthPathDisabledError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _force_environment(environment: Environment) -> Iterator[None]:
    """Pin ``settings.environment`` for the body of the ``with`` block.

    Patches the ``get_settings`` symbol imported into ``auth.test_auth`` so the
    guard reads the chosen environment regardless of how the test process was
    launched.
    """
    fake_settings = SimpleNamespace(environment=environment)
    with patch.object(test_auth, "get_settings", return_value=fake_settings):
        yield


@pytest.fixture(autouse=True)
def _clear_bypass_registry() -> Iterator[None]:
    """Ensure the module-level bypass registry is clean around every test."""
    test_auth._active_app_refcounts.clear()
    yield
    test_auth._active_app_refcounts.clear()


#: Environments in which the Test_Auth_Path must refuse to operate.
_DISABLED_ENVIRONMENTS = [Environment.PRODUCTION, Environment.STAGING]
#: Environments in which it is permitted.
_ENABLED_ENVIRONMENTS = [Environment.TEST, Environment.DEVELOPMENT]


# ===========================================================================
# 1. Entry points raise outside test/development (Req 11.3)
# ===========================================================================


class TestEntryPointsFailClosed:
    """Every public entry point is unusable outside test/development (Req 11.3)."""

    @pytest.mark.parametrize("environment", _DISABLED_ENVIRONMENTS)
    def test_issue_test_context_raises(self, environment):
        """issue_test_context refuses to mint a context in prod/staging."""
        with _force_environment(environment):
            with pytest.raises(_DisabledError) as exc_info:
                issue_test_context("tenant-a", roles=["admin"], has_pii_access=True)
        # The message names the offending environment for a clear failure.
        assert environment.value in str(exc_info.value)

    @pytest.mark.parametrize("environment", _DISABLED_ENVIRONMENTS)
    def test_override_auth_raises(self, environment):
        """override_auth refuses to install an override in prod/staging."""
        app = FastAPI()
        with _force_environment(environment):
            with pytest.raises(_DisabledError):
                with override_auth(app, tenant_id="tenant-a"):
                    pass  # pragma: no cover - body must never run
        # A failed entry must leave no override and no bypass registration.
        assert get_tenant_context not in app.dependency_overrides
        assert test_auth._active_app_refcounts == {}

    def test_bypass_predicate_false_in_production_even_if_registered(self):
        """A stray bypass registration cannot weaken production (Req 11.3).

        Even with an app id present in the registry, the predicate returns
        ``False`` in production because it is keyed off the environment.
        """
        app = FastAPI()
        test_auth._active_app_refcounts[id(app)] = 1
        with _force_environment(Environment.PRODUCTION):
            assert is_test_auth_bypass_active(app) is False
            assert is_test_auth_bypass_active(None) is False

    @pytest.mark.parametrize("environment", _ENABLED_ENVIRONMENTS)
    def test_entry_points_allowed_in_test_and_development(self, environment):
        """The path works in both enabled environments (control case)."""
        with _force_environment(environment):
            ctx = issue_test_context("tenant-a", roles=["admin"])
        assert ctx.tenant_id == "tenant-a"


# ===========================================================================
# 2. Produced context carries the requested identity (Req 11.1)
# ===========================================================================


class TestIssuedContextIdentity:
    """The issued TenantContext mirrors the requested identity (Req 11.1)."""

    def test_carries_requested_tenant_roles_and_pii(self):
        """tenant_id / roles / has_pii_access equal what the caller requested."""
        with _force_environment(Environment.TEST):
            ctx = issue_test_context(
                "tenant-xyz",
                roles=["dispatcher", "ops_manager"],
                has_pii_access=True,
            )
        assert isinstance(ctx, TenantContext)
        assert ctx.tenant_id == "tenant-xyz"
        assert ctx.roles == ["dispatcher", "ops_manager"]
        assert ctx.has_pii_access is True

    def test_has_pii_access_false_is_preserved(self):
        """A requested has_pii_access=False is carried through unchanged."""
        with _force_environment(Environment.TEST):
            ctx = issue_test_context("tenant-a", roles=["driver"], has_pii_access=False)
        assert ctx.has_pii_access is False

    def test_explicit_user_id_is_used_for_audit_attribution(self):
        """An explicit user_id is carried through (audit attribution, Req 5.5)."""
        with _force_environment(Environment.TEST):
            ctx = issue_test_context("tenant-a", user_id="user-42")
        assert ctx.user_id == "user-42"

    def test_default_user_id_is_deterministic_per_tenant(self):
        """Without an explicit user_id, the default is derived from the tenant."""
        with _force_environment(Environment.TEST):
            ctx_a1 = issue_test_context("tenant-a")
            ctx_a2 = issue_test_context("tenant-a")
            ctx_b = issue_test_context("tenant-b")
        # Same tenant -> same identity; different tenant -> different identity.
        assert ctx_a1.user_id == ctx_a2.user_id
        assert ctx_a1.user_id != ctx_b.user_id

    def test_default_roles_when_unspecified(self):
        """Roles default to the admin role when the caller omits them."""
        with _force_environment(Environment.TEST):
            ctx = issue_test_context("tenant-a")
        assert ctx.roles == ["admin"]

    def test_roles_are_normalized(self):
        """Blank/whitespace roles are dropped and a bare string is one role."""
        with _force_environment(Environment.TEST):
            ctx = issue_test_context("tenant-a", roles=["  admin  ", "", "driver"])
            ctx_single = issue_test_context("tenant-a", roles="dispatcher")
        assert ctx.roles == ["admin", "driver"]
        assert ctx_single.roles == ["dispatcher"]

    @pytest.mark.parametrize("bad_tenant", ["", "   "])
    def test_blank_tenant_id_rejected(self, bad_tenant):
        """A blank tenant_id is rejected — tenant scope is meaningless."""
        with _force_environment(Environment.TEST):
            with pytest.raises(ValueError):
                issue_test_context(bad_tenant)


# ===========================================================================
# 3. override_auth installs the override + isolation helper (Req 11.2, 11.4)
# ===========================================================================


class TestOverrideAuthIsolation:
    """override_auth wires the dependency override and scopes to one tenant."""

    def test_installs_and_restores_dependency_override(self):
        """The override is present inside the block and gone after it."""
        app = FastAPI()
        assert get_tenant_context not in app.dependency_overrides

        with _force_environment(Environment.TEST):
            with override_auth(app, tenant_id="tenant-a", roles=["admin"]) as ctx:
                assert get_tenant_context in app.dependency_overrides
                # The override resolves to the very context that was yielded.
                assert app.dependency_overrides[get_tenant_context]() is ctx
                assert is_test_auth_bypass_active(app) is True

        # Cleanly torn down on exit.
        assert get_tenant_context not in app.dependency_overrides
        assert is_test_auth_bypass_active(app) is False

    def test_restores_previous_override_on_exit(self):
        """A pre-existing override is restored (not clobbered) on exit."""
        app = FastAPI()
        sentinel_ctx = TenantContext(
            tenant_id="pre-existing", user_id="u", has_pii_access=False
        )
        previous = lambda: sentinel_ctx  # noqa: E731
        app.dependency_overrides[get_tenant_context] = previous

        with _force_environment(Environment.TEST):
            with override_auth(app, tenant_id="tenant-a"):
                assert app.dependency_overrides[get_tenant_context]() is not sentinel_ctx

        assert app.dependency_overrides[get_tenant_context] is previous

    def test_yielded_context_is_scoped_to_one_tenant(self):
        """The yielded context is scoped to exactly the requested tenant."""
        app = FastAPI()
        with _force_environment(Environment.TEST):
            with override_auth(app, tenant_id="tenant-a", roles=["ops_manager"]) as ctx:
                assert ctx.tenant_id == "tenant-a"
                assert ctx.roles == ["ops_manager"]

    def test_scoped_context_cannot_access_another_tenants_data(self):
        """End-to-end isolation: a context scoped to tenant A sees only A's data.

        This exercises exactly the assertion the Test_Auth_Path exists to
        support (Req 11.4): a tenant-isolation test scopes a context to one
        tenant and confirms another tenant's records are inaccessible.
        """
        # A tiny tenant-scoped data store + endpoint that filters by the
        # verified tenant_id from get_tenant_context (the production seam).
        records = {
            "tenant-a": ["a-record-1", "a-record-2"],
            "tenant-b": ["b-record-1"],
        }

        app = FastAPI()

        @app.get("/records")
        def list_records(tenant: TenantContext = Depends(get_tenant_context)):
            return {"tenant_id": tenant.tenant_id, "records": records.get(tenant.tenant_id, [])}

        client = TestClient(app)

        with _force_environment(Environment.TEST):
            with override_auth(app, tenant_id="tenant-a"):
                resp = client.get("/records")

        assert resp.status_code == 200
        body = resp.json()
        # Only tenant A's data is visible; tenant B's record never leaks.
        assert body["tenant_id"] == "tenant-a"
        assert body["records"] == ["a-record-1", "a-record-2"]
        assert "b-record-1" not in body["records"]

    def test_nested_overlapping_blocks_are_reference_counted(self):
        """Overlapping blocks on one app keep the bypass active until the last exits."""
        app = FastAPI()
        with _force_environment(Environment.TEST):
            with override_auth(app, tenant_id="tenant-a"):
                with override_auth(app, tenant_id="tenant-a"):
                    assert is_test_auth_bypass_active(app) is True
                # Inner block exited; outer block still holds the bypass.
                assert is_test_auth_bypass_active(app) is True
            # Outer block exited; bypass fully released.
            assert is_test_auth_bypass_active(app) is False
