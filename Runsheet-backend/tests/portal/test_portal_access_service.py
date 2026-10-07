"""``PortalAccessService`` (design §1.7, F1, D10; PRV-1, ISO-T-5, AC7).

Runs the service over the in-memory ``FakePortalDB`` (transactional unit of
work) and ``FakeSuperTokens`` (provisioner admin + SDK seams) from the
portal conftest, so every SuperTokens write is observable.
"""
from __future__ import annotations

import logging

import pytest

from auth.test_auth import issue_test_context
from errors.exceptions import AppException
from tests.portal.conftest import (
    CUSTOMER_A,
    CUSTOMER_B,
    CUSTOMER_C,
    T1,
    T2,
    FakeCustomerService,
    FakePortalDB,
    FakeSuperTokens,
    FakeTelemetry,
    make_access_service,
)

EMAIL = "buyer@example.test"


class Env:
    def __init__(self) -> None:
        self.customers = FakeCustomerService()
        for tenant_id, customer_id in ((T1, CUSTOMER_A), (T1, CUSTOMER_B), (T2, CUSTOMER_C)):
            self.customers.add(tenant_id, customer_id)
        self.db = FakePortalDB()
        self.st = FakeSuperTokens(self.db)
        self.telemetry = FakeTelemetry()
        self.service = make_access_service(self.customers, self.db, self.st, self.telemetry)
        self.admin = issue_test_context(T1, roles=("admin",), user_id="admin-t1")

    def outcomes(self, action: str):
        return [
            e["details"]["outcome"]
            for e in self.telemetry.events
            if e["event_type"] == f"portal_user_{action}"
        ]


@pytest.fixture
def env() -> Env:
    return Env()


def _err(exc: AppException):
    return (exc.status_code, exc.to_dict())


# ---------------------------------------------------------------------------
# Invite
# ---------------------------------------------------------------------------


async def test_invite_creates_binding_grant_and_link(env):
    result = await env.service.invite(env.admin, CUSTOMER_A, f"  {EMAIL} ")

    assert result.grant_id.startswith("pug_")
    assert result.email == EMAIL
    assert result.status == "invited"
    assert result.already_invited is False
    assert result.link_error is False and result.email_sent is True
    row = env.db.user(EMAIL)
    uid = row["st_user_id"]
    assert result.password_set_link == f"https://reset.test/{uid}"
    assert row["roles"] == ["customer"] and row["customer_id"] == CUSTOMER_A
    assert row["tenant_id"] == T1 and row["driver_id"] is None and row["has_pii_access"] is False
    assert env.st.roles[uid] == ["customer"]
    assert env.st.metadata[uid]["customer_id"] == CUSTOMER_A
    (grant,) = env.db.active(T1, CUSTOMER_A)
    assert grant["grant_id"] == result.grant_id and grant["created_by"] == "admin-t1"
    assert env.db.locks == [(T1, CUSTOMER_A)]
    assert env.outcomes("invite") == ["invited:created"]


async def test_role_created_once_per_service(env):
    await env.service.invite(env.admin, CUSTOMER_A, "one@example.test")
    await env.service.invite(env.admin, CUSTOMER_A, "two@example.test")
    assert [c for c in env.st.calls if c[0] == "create_role"] == [("create_role", "customer")]


@pytest.mark.parametrize("role", ["dispatcher", "driver", "platform_admin"])
async def test_non_admin_refused(env, role):
    ctx = issue_test_context(T1, roles=(role,))
    for call in (
        env.service.invite(ctx, CUSTOMER_A, EMAIL),
        env.service.list(ctx, CUSTOMER_A),
        env.service.resend(ctx, CUSTOMER_A, "pug_x"),
        env.service.revoke(ctx, CUSTOMER_A, "pug_x"),
    ):
        with pytest.raises(AppException) as exc:
            await call
        assert exc.value.to_dict()["error_code"] == "INSUFFICIENT_ROLE"
    assert env.st.writes() == [] and env.db.grants == []


async def test_already_invited_returns_same_grant(env):
    first = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    second = await env.service.invite(env.admin, CUSTOMER_A, EMAIL.upper())
    assert second.already_invited is True
    assert second.grant_id == first.grant_id
    assert len(env.db.active(T1, CUSTOMER_A)) == 1
    assert len([c for c in env.st.calls if c[0] == "create_user"]) == 1
    assert env.outcomes("invite") == ["invited:created", "already_invited"]


async def test_user_cap(env):
    for i in range(10):
        await env.service.invite(env.admin, CUSTOMER_A, f"u{i}@example.test")
    creates = len([c for c in env.st.calls if c[0] == "create_user"])

    with pytest.raises(AppException) as exc:
        await env.service.invite(env.admin, CUSTOMER_A, "u10@example.test")
    assert exc.value.status_code == 409
    assert exc.value.to_dict()["error_code"] == "PORTAL_USER_LIMIT_REACHED"
    assert len(env.db.active(T1, CUSTOMER_A)) == 10
    assert env.db.user("u10@example.test") is None
    assert len([c for c in env.st.calls if c[0] == "create_user"]) == creates

    # Another customer has its own cap; a revoke frees a slot.
    await env.service.invite(env.admin, CUSTOMER_B, "b0@example.test")
    revoked = env.db.active(T1, CUSTOMER_A)[0]["grant_id"]
    await env.service.revoke(env.admin, CUSTOMER_A, revoked)
    await env.service.invite(env.admin, CUSTOMER_A, "u10@example.test")
    assert len(env.db.active(T1, CUSTOMER_A)) == 10


async def test_archived_customer_refused(env):
    env.customers.add(T1, "QA-PORTAL-CUST-ARCH", status="archived")
    with pytest.raises(AppException) as exc:
        await env.service.invite(env.admin, "QA-PORTAL-CUST-ARCH", EMAIL)
    assert exc.value.status_code == 409
    assert exc.value.to_dict()["error_code"] == "PORTAL_CUSTOMER_ARCHIVED"
    assert env.st.writes() == [] and env.db.grants == [] and env.db.auth_users == {}


_IN_USE_ROWS = {
    "staff_role": dict(tenant_id=T1, roles=["admin"]),
    "driver": dict(tenant_id=T1, roles=["driver"], driver_id="QA-DRV-1"),
    "other_customer": dict(tenant_id=T1, roles=["customer"], customer_id=CUSTOMER_B),
    "cross_tenant_staff": dict(tenant_id=T2, roles=["dispatcher"]),
    "cross_tenant_customer": dict(tenant_id=T2, roles=["customer"], customer_id=CUSTOMER_C),
    "roleless_driver_link": dict(tenant_id=T1, roles=[], driver_id="QA-DRV-2"),
}
_REASONS = {
    "staff_role": "staff_role",
    "driver": "staff_role",
    "other_customer": "other_customer",
    "cross_tenant_staff": "cross_tenant",
    "cross_tenant_customer": "cross_tenant",
    "roleless_driver_link": "driver_link",
}


async def test_email_in_use_is_indistinguishable(env, caplog):
    caplog.set_level(logging.WARNING)
    bodies = set()
    for case, fields in _IN_USE_ROWS.items():
        env.db.auth_users.clear()
        fields = dict(fields)
        tenant_id = fields.pop("tenant_id")
        env.db.add_user(EMAIL, tenant_id, st_user_id="st-existing", **fields)
        before = dict(env.db.user(EMAIL))

        with pytest.raises(AppException) as exc:
            await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
        status, body = _err(exc.value)
        assert status == 409 and body["error_code"] == "PORTAL_EMAIL_IN_USE", case
        bodies.add(repr(body))
        assert env.db.user(EMAIL) == before, case
        assert env.outcomes("invite")[-1] == f"rejected:{_REASONS[case]}", case

    # One identical body for every reason; no SuperTokens write, no grant.
    assert len(bodies) == 1
    assert env.st.writes() == [] and env.db.grants == []
    assert all("@" not in r.getMessage() for r in caplog.records if "Portal invite refused" in r.getMessage())


async def test_unbound_supertokens_user_refused(env):
    env.st.users[EMAIL] = "st-squatter"
    with pytest.raises(AppException) as exc:
        await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    assert exc.value.to_dict()["error_code"] == "PORTAL_EMAIL_IN_USE"
    assert env.st.writes() == []
    assert env.db.auth_users == {} and env.db.grants == []
    assert env.outcomes("invite") == ["rejected:unbound_supertokens_user"]


async def test_invite_cross_tenant_customer_404(env):
    """ISO-T-5: a T1 admin can't target T2's customer."""
    with pytest.raises(AppException) as exc:
        await env.service.invite(env.admin, CUSTOMER_C, EMAIL)
    assert exc.value.status_code == 404
    assert exc.value.to_dict()["error_code"] == "RESOURCE_NOT_FOUND"
    assert env.st.calls == [] and env.db.auth_users == {} and env.db.grants == []


async def test_invite_email_of_other_tenant_409(env):
    """ISO-T-5: a T2 portal user's email can't be pulled into T1."""
    t2_admin = issue_test_context(T2, roles=("admin",), user_id="admin-t2")
    await env.service.invite(t2_admin, CUSTOMER_C, EMAIL)
    t2_row = dict(env.db.user(EMAIL))
    writes = list(env.st.writes())

    with pytest.raises(AppException) as exc:
        await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    assert exc.value.to_dict()["error_code"] == "PORTAL_EMAIL_IN_USE"
    assert env.st.writes() == writes
    assert env.db.user(EMAIL) == t2_row
    assert env.db.active(T1, CUSTOMER_A) == []


async def test_rollback_and_compensation_on_supertokens_error(env):
    env.st.fail["set_user_roles"] = RuntimeError("core unavailable")
    with pytest.raises(RuntimeError):
        await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    # Rolled back, and the user this attempt created is gone again.
    assert env.db.auth_users == {} and env.db.grants == []
    assert env.st.users == {}
    assert env.outcomes("invite") == ["failed"]
    # A retry converges (no unbound SuperTokens user left behind).
    result = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    assert result.status == "invited"


async def test_compensation_after_provisioner_commit_failure(env):
    env.db.fail_commits = 1
    with pytest.raises(RuntimeError):
        await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    assert env.db.auth_users == {} and env.db.grants == []
    assert env.st.users == {}
    assert [c[0] for c in env.st.writes()][-1] == "delete_user"


async def test_compensation_restores_roles_of_bound_user(env):
    """A role-less same-tenant row bound to a SuperTokens user keeps that user."""
    env.db.add_user(EMAIL, T1, roles=[], st_user_id="st-bound")
    env.st.users[EMAIL] = "st-bound"
    env.db.fail_commits = 1
    with pytest.raises(RuntimeError):
        await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    assert env.st.users[EMAIL] == "st-bound"
    assert env.st.roles["st-bound"] == []
    assert env.db.user(EMAIL)["roles"] == [] and env.db.grants == []


async def test_link_mint_failure_keeps_grant(env, caplog):
    from auth.password_admin import PasswordAdminError

    async def _fail(email, *, tenant_id):
        raise PasswordAdminError("link_unavailable", "no link")

    env.service._link_minter = _fail
    caplog.set_level(logging.WARNING)
    result = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    assert result.password_set_link is None and result.link_error is True
    assert result.email_sent is True
    assert len(env.db.active(T1, CUSTOMER_A)) == 1
    assert any("password-set link could not be minted" in r.getMessage() for r in caplog.records)


async def test_email_failure_reports_not_sent(env):
    env.st.fail["send_reset_email"] = RuntimeError("smtp down")
    result = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    assert result.email_sent is False and result.link_error is False
    assert len(env.db.active(T1, CUSTOMER_A)) == 1


async def test_service_never_writes_portal_audit(env, caplog):
    caplog.set_level(logging.DEBUG)
    result = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    await env.service.revoke(env.admin, CUSTOMER_A, result.grant_id)
    assert [r for r in caplog.records if r.name == "portal_audit"] == []
    # The telemetry audit carries the actor and target, like the driver flow.
    event = env.telemetry.events[0]
    assert event["user_id"] == "admin-t1" and event["resource_type"] == "portal_user_grant"
    assert event["details"]["customer_id"] == CUSTOMER_A


# ---------------------------------------------------------------------------
# List / resend
# ---------------------------------------------------------------------------


async def test_list_newest_first_with_display_status(env):
    g1 = await env.service.invite(env.admin, CUSTOMER_A, "one@example.test")
    g2 = await env.service.invite(env.admin, CUSTOMER_A, "two@example.test")
    g3 = await env.service.invite(env.admin, CUSTOMER_A, "three@example.test")
    await env.service.invite(env.admin, CUSTOMER_B, "other@example.test")
    await env.service.revoke(env.admin, CUSTOMER_A, g1.grant_id)
    await env.db.mark_first_seen(grant_id=g2.grant_id)

    listed = (await env.service.list(env.admin, CUSTOMER_A)).data
    assert [(g.grant_id, g.status) for g in listed] == [
        (g3.grant_id, "invited"),
        (g2.grant_id, "active"),
        (g1.grant_id, "revoked"),
    ]
    assert listed[2].revoked_at is not None and listed[0].created_at is not None


async def test_resend(env):
    grant = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    uid = env.db.user(EMAIL)["st_user_id"]
    env.st.calls.clear()

    sent = await env.service.resend(env.admin, CUSTOMER_A, grant.grant_id)
    assert sent.password_set_link == f"https://reset.test/{uid}"
    assert sent.link_error is False and sent.email_sent is True
    assert ("send_reset_email", uid) in env.st.calls

    for grant_id, customer_id in (("pug_missing", CUSTOMER_A), (grant.grant_id, CUSTOMER_B)):
        with pytest.raises(AppException) as exc:
            await env.service.resend(env.admin, customer_id, grant_id)
        assert exc.value.status_code == 404

    await env.service.revoke(env.admin, CUSTOMER_A, grant.grant_id)
    with pytest.raises(AppException) as exc:
        await env.service.resend(env.admin, CUSTOMER_A, grant.grant_id)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


async def test_revoke_removes_identity_and_keeps_history(env):
    env.db.add_user("staff@example.test", T1, roles=["admin"], st_user_id="st-staff")
    grant = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    uid = env.db.user(EMAIL)["st_user_id"]
    env.st.calls.clear()

    resp = await env.service.revoke(env.admin, CUSTOMER_A, grant.grant_id)
    assert resp.model_dump() == {"grant_id": grant.grant_id, "status": "revoked"}
    assert [c for c in env.st.writes()] == [
        ("set_user_roles", (uid, [])),
        ("revoke_sessions", uid),
        ("delete_user", uid),
    ]
    assert env.db.user(EMAIL) is None
    assert env.db.user("staff@example.test")["roles"] == ["admin"]
    (row,) = env.db.grants
    assert row["status"] == "revoked" and row["revoked_by"] == "admin-t1"
    assert env.db.locks[-1] == (T1, CUSTOMER_A)

    with pytest.raises(AppException) as exc:
        await env.service.revoke(env.admin, CUSTOMER_A, grant.grant_id)
    assert exc.value.status_code == 404

    # Re-invite provisions a fresh SuperTokens user without a conflict.
    again = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    assert again.already_invited is False
    assert env.db.user(EMAIL)["st_user_id"] not in (None, uid)


async def test_revoke_other_customers_grant_404(env):
    grant = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    with pytest.raises(AppException) as exc:
        await env.service.revoke(env.admin, CUSTOMER_B, grant.grant_id)
    assert exc.value.status_code == 404
    assert env.db.active(T1, CUSTOMER_A)


async def test_revoke_retry_after_failed_commit_treats_unknown_user_as_success(env):
    grant = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    uid = env.db.user(EMAIL)["st_user_id"]

    env.db.fail_commits = 1
    with pytest.raises(RuntimeError):
        await env.service.revoke(env.admin, CUSTOMER_A, grant.grant_id)
    # SuperTokens already forgot the user; the DB rolled back.
    assert not env.st.known(uid)
    assert env.db.user(EMAIL) is not None and env.db.active(T1, CUSTOMER_A)

    env.st.calls.clear()
    await env.service.revoke(env.admin, CUSTOMER_A, grant.grant_id)
    assert [c[0] for c in env.st.writes()] == ["set_user_roles", "revoke_sessions", "delete_user"]
    assert env.db.user(EMAIL) is None
    assert env.db.grants[0]["status"] == "revoked"
    assert env.outcomes("revoke") == ["failed", "revoked"]


async def test_revoke_rolls_back_on_supertokens_error(env):
    grant = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    uid = env.db.user(EMAIL)["st_user_id"]
    env.st.fail["revoke_sessions"] = RuntimeError("core unavailable")
    with pytest.raises(RuntimeError):
        await env.service.revoke(env.admin, CUSTOMER_A, grant.grant_id)
    assert env.st.known(uid)
    assert env.db.user(EMAIL) is not None and env.db.active(T1, CUSTOMER_A)
    assert env.outcomes("revoke") == ["failed"]


async def test_revoke_invalidates_principal_cache(env, monkeypatch):
    from portal.services import principal

    class _Checker:
        def __init__(self):
            self.invalidated = []

        def invalidate_user(self, user_id):
            self.invalidated.append(user_id)

    checker = _Checker()
    monkeypatch.setattr(principal, "_checker", checker)
    grant = await env.service.invite(env.admin, CUSTOMER_A, EMAIL)
    uid = env.db.user(EMAIL)["st_user_id"]
    await env.service.revoke(env.admin, CUSTOMER_A, grant.grant_id)
    assert checker.invalidated == [uid]


# ---------------------------------------------------------------------------
# Customer archive hook
# ---------------------------------------------------------------------------


async def test_revoke_sessions_for_customer_keeps_grants(env, caplog):
    await env.service.invite(env.admin, CUSTOMER_A, "a1@example.test")
    await env.service.invite(env.admin, CUSTOMER_A, "a2@example.test")
    await env.service.invite(env.admin, CUSTOMER_B, "b1@example.test")
    a_uids = {env.db.user(e)["st_user_id"] for e in ("a1@example.test", "a2@example.test")}
    env.st.calls.clear()

    assert await env.service.revoke_sessions_for_customer(T1, CUSTOMER_A) == 2
    assert {c[1] for c in env.st.calls if c[0] == "revoke_sessions"} == a_uids
    assert [c[0] for c in env.st.writes()] == ["revoke_sessions", "revoke_sessions"]
    assert len(env.db.active(T1, CUSTOMER_A)) == 2

    # Best effort: one failing user is logged at ERROR, the rest proceed.
    caplog.set_level(logging.ERROR)
    env.st.fail["revoke_sessions"] = RuntimeError("core unavailable")
    assert await env.service.revoke_sessions_for_customer(T1, CUSTOMER_A) == 1
    assert any("revoke on archive failed" in r.getMessage() for r in caplog.records)

    env.db.fail_commits = 1  # the read itself fails: nothing raised
    assert await env.service.revoke_sessions_for_customer(T1, CUSTOMER_A) == 0
