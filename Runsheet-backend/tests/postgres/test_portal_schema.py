"""Customer-portal schema invariants on real Postgres (design §1.3; PRV-2).

Needs the migrated schema (``alembic upgrade head``), which the CI
migration-check job applies before running this directory. Every row uses a
unique tenant id and email and is deleted afterwards.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_INSERT = text(
    "INSERT INTO auth_users (email, tenant_id, roles, has_pii_access, driver_id, customer_id) "
    "VALUES (:email, :tenant_id, CAST(:roles AS text[]), :pii, :driver_id, :customer_id)"
)


def _params(tenant_id, *, roles, customer_id=None, driver_id=None, pii=False):
    return {
        "email": f"pytest-portal-{uuid.uuid4().hex[:12]}@example.test",
        "tenant_id": tenant_id,
        "roles": "{" + ",".join(roles) + "}",
        "pii": pii,
        "driver_id": driver_id,
        "customer_id": customer_id,
    }


@pytest.mark.parametrize(
    "case",
    [
        {"roles": ["customer", "admin"], "customer_id": "QA-C1"},
        {"roles": ["customer"], "customer_id": "QA-C1", "driver_id": "QA-D1"},
        {"roles": ["dispatcher"], "customer_id": "QA-C1"},
        {"roles": ["customer"]},
        {"roles": ["customer"], "customer_id": "QA-C1", "pii": True},
    ],
    ids=["customer_plus_admin", "customer_with_driver", "binding_on_staff", "customer_unbound", "customer_with_pii"],
)
async def test_customer_binding_check(pg_engine, case):
    tenant_id = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    try:
        with pytest.raises(IntegrityError) as exc:
            async with pg_engine.begin() as conn:
                await conn.execute(_INSERT, _params(tenant_id, **case))
        assert "ck_auth_users_customer_binding" in str(exc.value)

        # The two valid shapes still insert.
        async with pg_engine.begin() as conn:
            await conn.execute(_INSERT, _params(tenant_id, roles=["customer"], customer_id="QA-C1"))
            await conn.execute(_INSERT, _params(tenant_id, roles=["dispatcher"]))
    finally:
        async with pg_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM auth_users WHERE tenant_id = :t"), {"t": tenant_id}
            )


# ---------------------------------------------------------------------------
# PortalAccessService on real Postgres (FEAT-002: F1 cap, ISO-T-4 grants list)
# ---------------------------------------------------------------------------


class _FakeST:
    """Minimal SuperTokens fake for the provisioner and the SDK seams."""

    def __init__(self):
        self.users = {}

    async def get_user_id_by_email(self, email):
        return self.users.get(email.casefold())

    async def create_user(self, email):
        import asyncio

        await asyncio.sleep(0.01)  # widen any race window
        uid = f"st-{uuid.uuid4().hex[:12]}"
        self.users[email.casefold()] = uid
        return uid

    async def set_user_roles(self, uid, roles):
        return None

    async def set_user_metadata(self, uid, metadata):
        return None

    async def noop(self, *args, **kwargs):
        return None

    async def mint(self, email, *, tenant_id):
        return None


class _Customers:
    def __init__(self, tenants):
        self._tenants = set(tenants)

    async def get(self, tenant_id, customer_id):
        from errors.exceptions import resource_not_found

        if tenant_id not in self._tenants:
            raise resource_not_found("Customer not found")
        return {"customer_id": customer_id, "tenant_id": tenant_id, "status": "active"}


def _access_service(pg_sessionmaker, tenants):
    from contextlib import asynccontextmanager

    from portal.services.portal_access_service import (
        PortalAccessService,
        PostgresPortalAccessUnitOfWork,
    )

    @asynccontextmanager
    async def uow():
        async with pg_sessionmaker() as session:
            async with session.begin():
                yield PostgresPortalAccessUnitOfWork(session)

    st = _FakeST()
    return PortalAccessService(
        customer_service=_Customers(tenants),
        uow_factory=uow,
        supertokens_admin=st,
        session_revoker=st.noop,
        user_deleter=st.noop,
        role_creator=st.noop,
        link_minter=st.mint,
        reset_emailer=st.noop,
    )


async def _cleanup(pg_engine, *tenants):
    async with pg_engine.begin() as conn:
        for t in tenants:
            await conn.execute(text("DELETE FROM portal_user_grants WHERE tenant_id = :t"), {"t": t})
            await conn.execute(text("DELETE FROM auth_users WHERE tenant_id = :t"), {"t": t})


async def test_concurrent_invites_cap(pg_engine, pg_sessionmaker):
    """FREEZE F1: 11 concurrent invites for one customer leave exactly 10 active."""
    import asyncio

    from auth.test_auth import issue_test_context
    from errors.exceptions import AppException

    tenant_id = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    service = _access_service(pg_sessionmaker, [tenant_id])
    admin = issue_test_context(tenant_id, roles=("admin",), user_id="pytest-admin")
    emails = [f"pytest-portal-{uuid.uuid4().hex[:8]}@example.test" for _ in range(11)]
    try:
        results = await asyncio.gather(
            *(service.invite(admin, "QA-C1", e) for e in emails), return_exceptions=True
        )
        ok = [r for r in results if not isinstance(r, BaseException)]
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(ok) == 10, errors
        assert len(errors) == 1 and isinstance(errors[0], AppException)
        assert errors[0].to_dict()["error_code"] == "PORTAL_USER_LIMIT_REACHED"
        async with pg_engine.connect() as conn:
            active = (await conn.execute(
                text("SELECT count(*) FROM portal_user_grants WHERE tenant_id = :t "
                     "AND customer_id = 'QA-C1' AND status = 'active'"), {"t": tenant_id},
            )).scalar_one()
            bound = (await conn.execute(
                text("SELECT count(*) FROM auth_users WHERE tenant_id = :t "
                     "AND customer_id = 'QA-C1' AND st_user_id IS NOT NULL"), {"t": tenant_id},
            )).scalar_one()
        assert active == 10 and bound == 10
    finally:
        await _cleanup(pg_engine, tenant_id)


async def test_grants_list_scoped_and_revoke_on_pg(pg_engine, pg_sessionmaker):
    """ISO-T-4 grants part: T1/A's list has no T2 rows (same customer id), and
    revoke deletes only the portal-only row."""
    from auth.test_auth import issue_test_context

    t1 = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    t2 = f"pytest-portal-{uuid.uuid4().hex[:12]}"
    service = _access_service(pg_sessionmaker, [t1, t2])
    a1 = issue_test_context(t1, roles=("admin",), user_id="pytest-admin-1")
    a2 = issue_test_context(t2, roles=("admin",), user_id="pytest-admin-2")
    e1 = f"pytest-portal-{uuid.uuid4().hex[:8]}@example.test"
    e2 = f"pytest-portal-{uuid.uuid4().hex[:8]}@example.test"
    staff = f"pytest-portal-{uuid.uuid4().hex[:8]}@example.test"
    try:
        async with pg_engine.begin() as conn:
            await conn.execute(_INSERT, {**_params(t2, roles=["admin"]), "email": staff})
        g1 = await service.invite(a1, "QA-C1", e1)
        await service.invite(a2, "QA-C1", e2)

        listed = (await service.list(a1, "QA-C1")).data
        assert [g.grant_id for g in listed] == [g1.grant_id]
        assert listed[0].email == e1 and listed[0].status == "invited"

        # Case-insensitive re-invite is the same active grant (CITEXT).
        again = await service.invite(a1, "QA-C1", e1.upper())
        assert again.already_invited and again.grant_id == g1.grant_id

        await service.revoke(a1, "QA-C1", g1.grant_id)
        async with pg_engine.connect() as conn:
            rows = (await conn.execute(
                text("SELECT email FROM auth_users WHERE tenant_id IN (:a, :b) ORDER BY email"),
                {"a": t1, "b": t2},
            )).all()
            status = (await conn.execute(
                text("SELECT status, revoked_by FROM portal_user_grants WHERE grant_id = :g"),
                {"g": g1.grant_id},
            )).one()
        assert sorted(r[0] for r in rows) == sorted([e2, staff])
        assert tuple(status) == ("revoked", "pytest-admin-1")
        assert (await service.list(a1, "QA-C1")).data[0].status == "revoked"
    finally:
        await _cleanup(pg_engine, t1, t2)
