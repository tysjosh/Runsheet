"""Session claims for customer-portal users (OI-06, design §1.1/§1.2; PRV-4)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, List

import pytest

import auth.supertokens_init as st_init


class _Result:
    def __init__(self, rows: List[tuple]) -> None:
        self._rows = rows

    def all(self) -> List[tuple]:
        return list(self._rows)


@pytest.fixture
def auth_users(monkeypatch):
    """A fake ``auth_users`` table answering the st_user_id-keyed lookup."""
    import persistence.database as database

    rows: List[Dict[str, Any]] = []

    class _DB:
        async def execute(self, query, params):
            assert "customer_id" in str(query)
            return _Result(
                [
                    (r["tenant_id"], r["roles"], r["has_pii_access"], r["driver_id"], r["customer_id"])
                    for r in rows
                    if r["st_user_id"] == params["user_id"]
                ]
            )

    @asynccontextmanager
    async def _scope():
        yield _DB()

    monkeypatch.setattr(database, "is_persistence_enabled", lambda: True)
    monkeypatch.setattr(database, "session_scope", _scope)
    return rows


def _row(st_user_id, *, roles, customer_id=None, driver_id=None):
    return {
        "st_user_id": st_user_id,
        "tenant_id": "demo-tenant",
        "roles": roles,
        "has_pii_access": False,
        "driver_id": driver_id,
        "customer_id": customer_id,
    }


async def test_customer_id_claim(auth_users):
    auth_users.append(_row("st-c", roles=["customer"], customer_id="QA-CUST-1"))
    auth_users.append(_row("st-s", roles=["dispatcher"]))
    auth_users.append(_row("st-e", roles=["customer"], customer_id=""))

    assert await st_init._lookup_auth_user_claims("st-c") == {
        "tenant_id": "demo-tenant",
        "roles": ["customer"],
        "has_pii_access": False,
        "customer_id": "QA-CUST-1",
    }
    assert "customer_id" not in await st_init._lookup_auth_user_claims("st-s")
    assert "customer_id" not in await st_init._lookup_auth_user_claims("st-e")


def test_customer_role_is_canonical_but_not_assignable():
    assert "customer" in st_init.CANONICAL_ROLES
    assert st_init.CUSTOMER_PORTAL_ROLE == "customer"
    assert "customer" not in st_init.CUSTOMER_ASSIGNABLE_ROLES
    assert "customer" not in st_init.STAFF_ROLES
    assert set(st_init.STAFF_ROLES) == set(st_init.CANONICAL_ROLES) - {"customer"}
