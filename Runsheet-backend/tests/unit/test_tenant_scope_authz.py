"""Cross-tenant authorization on endpoints acting on a caller-supplied tenant.

Two shapes of the same defect live here. The first half covers ``tenant_id`` as
a path parameter; the second half covers an identifier in the request body that
resolves to an ``auth_users`` row (see that section's header).

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

import contextlib
from dataclasses import dataclass, field
from typing import Any, List

import pytest

from auth.authorization import require_role
from auth.supertokens_init import (
    CANONICAL_ROLES,
    CUSTOMER_ASSIGNABLE_ROLES,
    PLATFORM_ADMIN_ROLE,
    PLATFORM_STAFF_ROLES,
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


def test_ops_manager_is_retired() -> None:
    """``ops_manager`` must not come back, and a revert must be loud.

    It was declared as a canonical role for the whole SuperTokens migration and
    gated nothing: zero ``require_role`` call sites named it, no inline role
    check consulted it, and no frontend surface referenced it. A role that
    grants nothing is worse than no role at all — it looks like a permission
    tier to whoever provisions the next ``auth_users`` row, so it invites
    granting access that is never actually conferred.

    Asserting absence from both tuples matters: dropping it from
    :data:`~auth.supertokens_init.CANONICAL_ROLES` alone would leave it
    assignable by a customer administrator, which is the worse of the two
    halves to get wrong.
    """
    assert "ops_manager" not in CANONICAL_ROLES
    assert "ops_manager" not in CUSTOMER_ASSIGNABLE_ROLES


# ---------------------------------------------------------------------------
# ``platform_admin`` is additive, not a superset: no role implies another
# ---------------------------------------------------------------------------
#
# ``require_role`` and ``require_tenant_scope`` answer different questions, and
# only the latter treats ``platform_admin`` as satisfying ``required_roles``.
# That is deliberate: ``require_role`` gates "may you do this at all" and must
# stay exact-match, because an implication graph there widens roughly twenty
# existing ``require_role(tenant, "admin")`` call sites at once, plus every
# future one. Staff accounts hold both roles instead — ``PLATFORM_STAFF_ROLES``.


@pytest.mark.parametrize("required", ["admin", "dispatcher", "driver"])
def test_platform_admin_alone_does_not_satisfy_require_role(required: str) -> None:
    """The anti-implication guard. ``platform_admin`` is not a super-role.

    If this test ever *passes the call* instead of raising, somebody taught
    :func:`require_role` a role-implication graph, and every
    ``require_role(tenant, "admin")`` site in the codebase silently widened to
    accept staff — including surfaces nobody re-reviewed. The intended shape is
    the opposite: the capability is additive, so a staff account carries
    ``admin`` explicitly.

    Parametrized across three requirements rather than only ``admin`` because a
    partial implication graph is as plausible a mistake as a total one: someone
    could map ``platform_admin -> admin`` and leave the operational roles alone.
    ``platform_admin`` satisfies none of them.
    """
    caller = _Caller(tenant_id="runsheet", roles=[PLATFORM_ADMIN_ROLE])
    with pytest.raises(AppException) as exc:
        require_role(caller, required)
    assert exc.value.error_code == ErrorCode.INSUFFICIENT_ROLE


def test_platform_admin_does_not_satisfy_the_platform_prefix() -> None:
    """``platform_admin`` must not satisfy a requirement for ``"platform"``.

    Exact-match role checking is covered generally by
    ``tests/unit/test_authorization.py`` and by the property test
    ``tests/property/test_exact_match_role_authorization_property.py``, so this
    case is deliberately narrow: it pins the one prefix pair the newest role
    introduces, where a held role has a required name as a proper prefix. A
    substring or ``startswith`` regression would make this pass.
    """
    caller = _Caller(tenant_id="runsheet", roles=[PLATFORM_ADMIN_ROLE])
    with pytest.raises(AppException) as exc:
        require_role(caller, "platform")
    assert exc.value.error_code == ErrorCode.INSUFFICIENT_ROLE


def test_platform_staff_roles_bundles_admin_with_the_staff_capability() -> None:
    """The bundle must carry ``admin`` too, or staff can reach nothing.

    Without this, the test above would be satisfiable by a "staff bundle" of
    ``platform_admin`` alone — which is precisely the 403-on-every-admin-route
    failure mode the decision exists to avoid.
    """
    assert "admin" in PLATFORM_STAFF_ROLES
    assert PLATFORM_ADMIN_ROLE in PLATFORM_STAFF_ROLES
    for role in PLATFORM_STAFF_ROLES:
        assert role in CANONICAL_ROLES, f"{role} is not a canonical role"


def test_staff_bundle_passes_both_gates() -> None:
    """End-to-end counterweight: staff can actually work.

    ``require_role`` for "may you do this at all", ``require_tenant_scope`` for
    "may you do it to that company". Without this case, tightening everything
    into "deny staff" would pass the guard above.
    """
    caller = _Caller(tenant_id="runsheet", roles=list(PLATFORM_STAFF_ROLES))
    require_role(caller, "admin")
    require_tenant_scope(caller, "globex", operation="Staff support action")


def test_platform_staff_roles_is_not_customer_assignable() -> None:
    """A tenant admin must not be able to assemble the staff bundle.

    ``admin`` is assignable on its own — that is the ordinary customer
    administrator. What must not be assignable is the *bundle*, and the reason
    is the ``platform_admin`` member.
    """
    assert not set(PLATFORM_STAFF_ROLES).issubset(set(CUSTOMER_ASSIGNABLE_ROLES))
    assert PLATFORM_ADMIN_ROLE not in CUSTOMER_ASSIGNABLE_ROLES


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


@pytest.mark.parametrize("role", ["driver", "dispatcher", "viewer"])
def test_non_admin_cannot_act_even_on_own_tenant(role: str) -> None:
    """The ops endpoints had no role check; a driver could call them.

    Two canonical non-admin roles plus ``viewer``, which is deliberately *not*
    canonical: the gate must deny an arbitrary role string as firmly as a
    recognised one, since ``auth_users.roles`` is never validated against
    :data:`~auth.supertokens_init.CANONICAL_ROLES` and can therefore hold
    anything an operator typed.
    """
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


# ===========================================================================
# The same hole, arriving as an identifier in the request BODY
# ===========================================================================
#
# The section above covers ``tenant_id`` in the path. Two more endpoints had the
# identical defect without a tenant anywhere in the URL: they take a user's
# email in the request body, resolve it against ``auth_users`` with no tenant
# filter, and act on whatever row comes back. Both were gated on ``admin``,
# which is tenant-scoped, so neither gate constrained *which company's* user was
# being acted on.
#
# 1. ``POST /api/auth/admin/password-reset-link`` ran
#    ``SELECT email FROM auth_users WHERE email = :email`` and then minted a
#    real SuperTokens reset link. A tenant-A admin could therefore mint one for
#    any provisioned account on the platform — including a ``platform_admin``
#    staff account — set its password, and sign in as it. That is a straight
#    escalation past the boundary the tests above establish.
#
# 2. ``POST /api/ops/drivers/{driver_id}/app-access`` read the row for
#    ``body.email`` unscoped and upserted with
#    ``ON CONFLICT (email) DO UPDATE SET tenant_id = EXCLUDED.tenant_id``, so
#    the same caller could rewrite another tenant's (or a staff account's) row
#    into their own tenant, point its ``driver_id`` at one of their drivers, and
#    append ``driver`` to its roles. The victim is hijacked into the attacker's
#    tenant and loses their own. Chained with (1) it is a full takeover of a
#    ``platform_admin`` account.
#
# These are written as attacks that used to succeed.


# ---------------------------------------------------------------------------
# 1. The auth_users lookup behind the password-reset link is tenant-scoped
# ---------------------------------------------------------------------------


class _FakeAuthUsers:
    """A one-table stand-in for ``auth_users`` that honours the WHERE clause.

    It would be easier to return a canned row from ``.first()``, but then every
    "the out-of-tenant email is not found" test would pass with the tenant
    filter deleted — the lookup would keep returning the victim regardless. So
    this evaluates the predicate: it matches on email always, and on tenant only
    when the emitted SQL actually filters on it. Removing the filter therefore
    makes those tests find the victim again, exactly as production would.

    ``rows`` is a list of ``(email, tenant_id)``.
    """

    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.statements: List[str] = []
        self.params: List[dict] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        bound = dict(params or {})
        self.statements.append(sql)
        self.params.append(bound)

        scoped = "tenant_id = :tenant_id" in sql
        found = None
        for email, tenant_id in self.rows:
            if email != bound.get("email"):
                continue
            if scoped and tenant_id != bound.get("tenant_id"):
                continue
            found = (email,)
            break

        class _Result:
            def first(self_inner):
                return found

        return _Result()


def _patched_persistence(session):
    """Patch ``persistence.database`` so the lookup runs against ``session``.

    ``_require_provisioned_email`` imports these lazily inside the function, so
    patching the module attributes is enough — no database is touched.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def _scope():
        yield session

    return patch.multiple(
        "persistence.database",
        is_persistence_enabled=lambda: True,
        session_scope=_scope,
    )


async def test_provisioned_email_lookup_is_filtered_by_caller_tenant() -> None:
    """The attack in (1): the SQL must carry the caller's tenant.

    Asserting on the emitted statement as well as the outcome because the whole
    defect was a missing ``AND tenant_id = ...`` — a test that only checked the
    happy-path return value passed before the fix.
    """
    from auth.password_admin import _require_provisioned_email

    session = _FakeAuthUsers([("driver@acme.test", "acme")])
    with _patched_persistence(session):
        found = await _require_provisioned_email(
            "driver@acme.test", tenant_id="acme"
        )

    assert found == "driver@acme.test"
    assert "tenant_id = :tenant_id" in session.statements[0]
    assert session.params[0]["tenant_id"] == "acme"


async def test_out_of_tenant_email_is_reported_as_not_provisioned() -> None:
    """An out-of-tenant account must be indistinguishable from a missing one.

    A 403 here would confirm to a tenant admin that the address exists somewhere
    on the platform, turning this route into an enumeration oracle over every
    customer's user base. The audit log keeps the distinction the client does
    not get.
    """
    from auth.password_admin import PasswordAdminError, _require_provisioned_email

    # The staff account exists — in another tenant. An acme admin must not see it.
    session = _FakeAuthUsers([("staff@runsheet.test", "runsheet")])
    with _patched_persistence(session):
        with pytest.raises(PasswordAdminError) as exc:
            await _require_provisioned_email("staff@runsheet.test", tenant_id="acme")

    assert exc.value.reason == "not_provisioned"


async def test_unscoped_lookup_stays_unscoped_for_break_glass() -> None:
    """``tenant_id=None`` is the operator-CLI path and must not filter.

    The counterweight to the test above: over-tightening into "always scope"
    would break ``scripts/set_user_password.py``, which has no session and no
    tenant.
    """
    from auth.password_admin import _require_provisioned_email

    session = _FakeAuthUsers([("staff@runsheet.test", "runsheet")])
    with _patched_persistence(session):
        found = await _require_provisioned_email(
            "staff@runsheet.test", tenant_id=None
        )

    assert found == "staff@runsheet.test"
    assert "tenant_id" not in session.statements[0]
    assert "tenant_id" not in session.params[0]


async def test_out_of_tenant_request_never_reaches_supertokens() -> None:
    """The denial must land before a reset link is minted, not after.

    ``_require_provisioned_email`` raising is only half the guarantee — what
    makes the attack work is ``create_reset_password_link`` returning a URL that
    sets the victim's password. So assert on the SDK collaborator: it must not
    be called at all, and neither must the user lookup that precedes it.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from auth.password_admin import PasswordAdminError, create_password_set_link

    session = _FakeAuthUsers([("staff@runsheet.test", "runsheet")])
    mint = AsyncMock(return_value="https://x/?token=t")
    # The victim resolves perfectly well in SuperTokens — the tenant scope is
    # the only thing standing between the caller and a usable link.
    resolve = AsyncMock(return_value=[SimpleNamespace(user_id="st-victim")])

    error = None
    with _patched_persistence(session), patch(
        "supertokens_python.recipe.emailpassword.asyncio.create_reset_password_link",
        new=mint,
    ), patch("supertokens_python.asyncio.list_users_by_account_info", new=resolve):
        try:
            await create_password_set_link("staff@runsheet.test", tenant_id="acme")
        except PasswordAdminError as exc:  # noqa: PERF203 — asserted below
            error = exc

    # Asserted before the exception so a regression reports the escalation
    # itself ("a link was minted") rather than a missing exception.
    mint.assert_not_awaited()
    resolve.assert_not_awaited()
    assert error is not None and error.reason == "not_provisioned"


# ---------------------------------------------------------------------------
# 2. The password-reset-link endpoint passes the right scope
# ---------------------------------------------------------------------------


def _password_admin_client(roles, tenant_id: str = "acme"):
    """A ``TestClient`` for the password-admin router with a stubbed session.

    ``register_exception_handlers`` is deliberately called: a bare ``FastAPI()``
    has no handler for ``AppException``, so a denial would surface as a 500 and
    a "was it refused?" assertion would pass for the wrong reason.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from auth.api.password_admin_endpoints import router
    from errors.handlers import register_exception_handlers
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    app = FastAPI()
    app.include_router(router)
    register_exception_handlers(app)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=tenant_id,
        user_id="user-1",
        has_pii_access=True,
        roles=list(roles),
    )
    return TestClient(app)


def _link_stub():
    from unittest.mock import AsyncMock

    from auth.password_admin import PasswordSetLink

    return AsyncMock(
        return_value=PasswordSetLink(
            email="victim@globex.test", st_user_id="st-9", link="https://x/?token=t"
        )
    )


def test_reset_link_endpoint_scopes_to_the_caller_tenant() -> None:
    """A tenant admin's request must be scoped to their own tenant."""
    from unittest.mock import patch

    client = _password_admin_client(["admin"], tenant_id="acme")
    stub = _link_stub()
    with patch(
        "auth.api.password_admin_endpoints.create_password_set_link", new=stub
    ):
        resp = client.post(
            "/api/auth/admin/password-reset-link",
            json={"email": "victim@globex.test"},
        )

    assert resp.status_code == 200, resp.text
    assert stub.await_args.kwargs["tenant_id"] == "acme"


def test_reset_link_endpoint_leaves_platform_admin_unscoped() -> None:
    """Staff keep the cross-tenant path — support work still has to work.

    The caller holds ``admin`` as well because this route's entry gate is the
    exact-match :func:`auth.authorization.require_role`, which (unlike
    :func:`require_tenant_scope`) does not treat ``platform_admin`` as a
    superset. Staff accounts hold both.
    """
    from unittest.mock import patch

    client = _password_admin_client(
        ["admin", PLATFORM_ADMIN_ROLE], tenant_id="runsheet"
    )
    stub = _link_stub()
    with patch(
        "auth.api.password_admin_endpoints.create_password_set_link", new=stub
    ):
        resp = client.post(
            "/api/auth/admin/password-reset-link",
            json={"email": "victim@globex.test"},
        )

    assert resp.status_code == 200, resp.text
    assert stub.await_args.kwargs["tenant_id"] is None


def test_reset_link_denied_when_caller_tenant_is_blank() -> None:
    """Fail closed rather than searching with a blank scope.

    A blank scope would match a row whose ``tenant_id`` is also blank — the same
    "a falsy value authorizes itself" defect
    ``test_blank_target_tenant_is_denied_not_matched`` pins for path params.
    """
    from unittest.mock import patch

    client = _password_admin_client(["admin"], tenant_id="")
    stub = _link_stub()
    with patch(
        "auth.api.password_admin_endpoints.create_password_set_link", new=stub
    ):
        resp = client.post(
            "/api/auth/admin/password-reset-link",
            json={"email": "victim@globex.test"},
        )

    assert resp.status_code == 403, resp.text
    stub.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. The app-access grant cannot rewrite another tenant's auth_users row
# ---------------------------------------------------------------------------


class _SpyAppAccessUoW:
    """Records writes so a test can prove the victim's row was never touched."""

    def __init__(self, existing_row) -> None:
        self.existing_row = existing_row
        self.upserts: List[dict] = []
        self.marked: List[dict] = []

    async def email_linked_to_driver(self, *, tenant_id: str, driver_id: str):
        return None

    async def read_row(self, email: str):
        return dict(self.existing_row) if self.existing_row else None

    async def upsert_app_access(self, **kwargs) -> None:
        self.upserts.append(kwargs)

    async def clear_app_access(self, **kwargs) -> None:  # pragma: no cover
        raise AssertionError("grant must never clear access")

    async def mark_provisioned(self, **kwargs) -> None:
        self.marked.append(kwargs)

    async def mark_failed(self, **kwargs) -> None:
        self.marked.append(kwargs)


@dataclass
class _GrantHarness:
    """The service under test plus every observable side effect of a grant."""

    service: Any
    tenant: Any
    body: Any
    uow: Any
    provision_calls: List[Any]
    audits: List[dict]

    def outcomes(self) -> List[str]:
        """The ``outcome`` recorded on each audit event, in order."""
        return [a["details"]["outcome"] for a in self.audits]


def _grant_harness(existing_row, caller_roles, caller_tenant="acme"):
    """Build an ``AppAccessService`` whose every write is observable."""
    from contextlib import asynccontextmanager

    from fuel.api.driver_endpoints import AppAccessGrantRequest, AppAccessService
    from ops.middleware.tenant_guard import TenantContext

    uow = _SpyAppAccessUoW(existing_row)
    provision_calls: List[Any] = []
    audits: List[dict] = []

    @asynccontextmanager
    async def _factory():
        yield uow

    class _Result:
        status = "updated"
        st_user_id = "st-9"

    async def _provision_user(row, *, admin, store):
        provision_calls.append(row)
        return _Result()

    class _Repo:
        async def get(self, tenant_id, driver_id):
            return object()  # the driver exists in the caller's tenant

    class _Telemetry:
        def log_audit_event(self, **kwargs):
            audits.append(kwargs)

    service = AppAccessService(
        driver_repository=_Repo(),
        uow_factory=_factory,
        supertokens_admin=object(),
        provision_user=_provision_user,
        telemetry_service=_Telemetry(),
    )
    tenant = TenantContext(
        tenant_id=caller_tenant,
        user_id="attacker-1",
        has_pii_access=False,
        roles=list(caller_roles),
    )
    body = AppAccessGrantRequest(email="victim@globex.test")
    return _GrantHarness(service, tenant, body, uow, provision_calls, audits)


async def test_grant_cannot_hijack_a_user_from_another_tenant() -> None:
    """The attack in (2), and the load-bearing assertion of this section.

    The exception type matters less than the writes: the victim's row must be
    provably unwritten, and SuperTokens must never have been touched. A guard
    that raised *after* the upsert would still leave the account hijacked.
    """
    h = _grant_harness(
        {"tenant_id": "globex", "roles": ["dispatcher"], "st_user_id": "st-victim"},
        ["admin"],
    )

    error = None
    try:
        await h.service.grant(h.tenant, "drv-001", h.body)
    except AppException as exc:  # noqa: PERF203 — asserted below
        error = exc

    # Checked before the exception so a regression reports the hijack itself,
    # not merely a missing error.
    assert h.uow.upserts == [], "the victim's auth_users row was rewritten"
    assert h.provision_calls == [], "SuperTokens was written despite the denial"
    assert error is not None, "the cross-tenant grant was accepted"
    # Same 409 a legitimately unavailable email produces — see the service's
    # comment: a distinguishable 403 would confirm the address holds an account
    # in some other tenant.
    assert error.error_code == ErrorCode.APP_ACCESS_ALREADY_LINKED
    assert "globex" not in str(error.details)
    assert "globex" not in error.message


async def test_grant_cross_tenant_denial_is_audited_distinctly() -> None:
    """The client cannot tell a probe from a conflict; the audit log must.

    Suppressing the 403 removes the enumeration oracle, but it would also erase
    the evidence if the audit trail collapsed into the generic
    ``rejected:APP_ACCESS_ALREADY_LINKED``. This pins the distinct outcome.
    """
    h = _grant_harness(
        {"tenant_id": "globex", "roles": ["dispatcher"], "st_user_id": "st-victim"},
        ["admin"],
    )

    with contextlib.suppress(AppException):
        await h.service.grant(h.tenant, "drv-001", h.body)

    assert h.outcomes() == ["rejected:cross_tenant_email"]
    assert h.audits[0]["details"]["acting_user_id"] == "attacker-1"
    assert h.audits[0]["details"]["email"] == "victim@globex.test"


async def test_grant_by_platform_admin_may_cross_tenants() -> None:
    """Staff retain the cross-tenant grant, so this is not "deny everything".

    ``admin`` is held too: ``grant`` still enters through the exact-match
    :func:`auth.authorization.require_role`, which does not treat
    ``platform_admin`` as a superset.
    """
    h = _grant_harness(
        {"tenant_id": "globex", "roles": [], "st_user_id": None},
        ["admin", PLATFORM_ADMIN_ROLE],
        caller_tenant="runsheet",
    )

    result = await h.service.grant(h.tenant, "drv-001", h.body)

    assert result.provision_status == "updated"
    assert len(h.uow.upserts) == 1
    assert len(h.provision_calls) == 1


async def test_grant_within_the_callers_own_tenant_still_works() -> None:
    """The ordinary case: an admin granting one of their own users."""
    h = _grant_harness(
        {"tenant_id": "acme", "roles": ["dispatcher"], "st_user_id": "st-1"},
        ["admin"],
    )

    result = await h.service.grant(h.tenant, "drv-001", h.body)

    assert result.tenant_id == "acme"
    assert h.uow.upserts[0]["roles"] == ["dispatcher", "driver"]
    assert len(h.provision_calls) == 1


async def test_grant_for_a_brand_new_email_is_unaffected() -> None:
    """No existing row means no tenant to protect — the guard must not fire.

    Pins that the check is conditional on an existing ``tenant_id``; making it
    unconditional would break first-time grants, which are the common case.
    """
    h = _grant_harness(None, ["admin"])

    result = await h.service.grant(h.tenant, "drv-001", h.body)

    assert result.email == "victim@globex.test"
    assert h.uow.upserts[0]["tenant_id"] == "acme"
    assert len(h.provision_calls) == 1


# ---------------------------------------------------------------------------
# 4. Revoke was already safe — pin it so a refactor cannot widen it
# ---------------------------------------------------------------------------


async def test_revoke_resolves_the_email_within_the_callers_tenant() -> None:
    """``revoke`` is safe *by construction*, and must stay that way.

    It never takes an email from the caller: it resolves one via
    ``email_linked_to_driver``, whose SQL filters on
    ``tenant_id = :tenant_id AND driver_id = :driver_id``, so the row it goes on
    to clear is in the caller's tenant by definition. A later refactor that
    accepted an email in the body — or dropped the tenant argument — would
    reintroduce exactly the defect above, so this test pins both the tenant
    passed to the lookup and the fact that a cross-tenant link is simply not
    found.
    """
    from contextlib import asynccontextmanager

    from fuel.api.driver_endpoints import AppAccessService
    from ops.middleware.tenant_guard import TenantContext

    lookups: List[dict] = []
    cleared: List[dict] = []

    class _Uow:
        async def email_linked_to_driver(self, *, tenant_id: str, driver_id: str):
            lookups.append({"tenant_id": tenant_id, "driver_id": driver_id})
            # The link exists, but in globex — so an acme caller must miss it.
            return "victim@globex.test" if tenant_id == "globex" else None

        async def read_row(self, email: str):  # pragma: no cover
            raise AssertionError("must not read a row it never resolved")

        async def clear_app_access(self, **kwargs) -> None:  # pragma: no cover
            cleared.append(kwargs)

    @asynccontextmanager
    async def _factory():
        yield _Uow()

    service = AppAccessService(
        driver_repository=object(), uow_factory=_factory, supertokens_admin=object()
    )
    tenant = TenantContext(
        tenant_id="acme", user_id="attacker-1", has_pii_access=False, roles=["admin"]
    )

    with pytest.raises(AppException) as exc:
        await service.revoke(tenant, "drv-001")

    assert exc.value.error_code == ErrorCode.RESOURCE_NOT_FOUND
    assert lookups == [{"tenant_id": "acme", "driver_id": "drv-001"}]
    assert cleared == []


# ---------------------------------------------------------------------------
# 5. Drift guards — the unscoped query must not come back
# ---------------------------------------------------------------------------
#
# The behavioural tests above all drive the code through a test double, so they
# only catch a regression that survives to runtime. These guards read the source
# and fail on the *shape* that caused the defect, which is what a careless
# refactor actually reintroduces.


def test_auth_users_lookup_scope_cannot_be_omitted_by_a_caller() -> None:
    """``tenant_id`` must stay keyword-only with no default.

    The original signatures took an email and nothing else, so every call site
    was unscoped by construction. A default value — even ``None`` — would put
    that back: a new caller could omit the argument and silently get the
    platform-wide lookup. Requiring it forces each caller to state its scope.
    """
    import inspect

    from auth import password_admin

    for name in (
        "_require_provisioned_email",
        "create_password_set_link",
        "set_password_for_email",
    ):
        param = inspect.signature(getattr(password_admin, name)).parameters[
            "tenant_id"
        ]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert param.default is inspect.Parameter.empty, (
            f"{name} gives tenant_id a default, so a caller can be unscoped "
            "by accident"
        )


def test_unscoped_auth_users_select_is_reachable_only_when_asked_for() -> None:
    """The unscoped SQL may exist, but only inside the ``tenant_id is None`` arm.

    ``scripts/set_user_password.py`` legitimately needs the platform-wide
    lookup, so the query itself cannot simply be banned. What must hold is that
    it is guarded: the tenant-filtered statement has to exist, and the bare
    ``WHERE email = :email`` form must sit after the explicit
    ``if tenant_id is None`` branch rather than being the only statement.
    """
    import inspect

    from auth.password_admin import _require_provisioned_email

    source = inspect.getsource(_require_provisioned_email)
    unscoped = 'SELECT email FROM auth_users WHERE email = :email'

    assert "AND tenant_id = :tenant_id" in source, (
        "the tenant-filtered lookup is gone — a tenant admin can reach any "
        "account on the platform again"
    )
    assert source.index("if tenant_id is None") < source.index(unscoped), (
        "the unscoped lookup is no longer behind an explicit "
        "'tenant_id is None' branch"
    )


def test_grant_checks_the_tenant_before_it_writes_auth_users() -> None:
    """Ordering is the whole guarantee, so pin it in the source too.

    A guard that runs after ``upsert_app_access`` leaves the victim's row
    already rewritten — the rollback would undo it, but ``provision_user`` may
    have pushed the role change to SuperTokens first. Both the comparison
    against the caller's tenant and the ``platform_admin`` exception must appear
    before the write.
    """
    import inspect

    from fuel.api.driver_endpoints import AppAccessService

    source = inspect.getsource(AppAccessService.grant)
    write_at = source.index("upsert_app_access")

    for marker in ('existing.get("tenant_id")', "is_platform_admin", "!= tenant.tenant_id"):
        assert marker in source, f"grant no longer performs: {marker}"
        assert source.index(marker) < write_at, (
            f"'{marker}' moved after the auth_users write"
        )
