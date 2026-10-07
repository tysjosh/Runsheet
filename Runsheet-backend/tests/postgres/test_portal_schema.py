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
