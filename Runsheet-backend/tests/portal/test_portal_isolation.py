"""Portal isolation (design §13.1). FEAT-002 adds XR-9; later FEATs add the
per-customer and per-tenant read cases to this module.

[real-auth] ``main.app`` with fake session verifiers.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from tests.portal.conftest import CUSTOMER_A, T1, call

EMAIL = "buyer@example.test"
BASE = f"/api/commerce/customers/{CUSTOMER_A}/portal-users"


@pytest.fixture
def db_checker(portal_fakes, access):
    """A principal checker over the access fake DB (grants joined by st_user_id)."""
    from portal.services import principal

    checker = principal.PortalPrincipalChecker(
        customer_service=portal_fakes.customers,
        grant_store=access.db,
        cache_seconds=60,
        clock=lambda: portal_fakes.clock[0],
    )
    principal.configure_portal_principal(checker)  # portal_fakes restores it
    return checker


@pytest.fixture
def real_claims_lookup(monkeypatch, access):
    """Run the real ``_lookup_auth_user_claims`` over the fake ``auth_users``."""
    import persistence.database as database

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class _DB:
        async def execute(self, _query, params):
            rows = [
                (r["tenant_id"], r["roles"], r["has_pii_access"], r["driver_id"], r["customer_id"])
                for r in access.db.auth_users.values()
                if r.get("st_user_id") == params["user_id"]
            ]
            return _Result(rows)

    @asynccontextmanager
    async def _scope():
        yield _DB()

    monkeypatch.setattr(database, "is_persistence_enabled", lambda: True)
    monkeypatch.setattr(database, "session_scope", _scope)

    from auth.supertokens_init import _lookup_auth_user_claims

    return lambda uid: asyncio.run(_lookup_auth_user_claims(uid))


def test_revoked_customer_has_no_session(
    client, portal_on, sessions, access, db_checker, real_claims_lookup
):
    """XR-9: revoke leaves no identity; an old session is suspended."""
    admin = sessions.staff("admin")
    invited = call(client, "POST", BASE, admin, json={"email": EMAIL})
    assert invited.status_code == 201
    uid = access.db.user(EMAIL)["st_user_id"]

    claims = real_claims_lookup(uid)
    assert claims == {
        "tenant_id": T1, "roles": ["customer"], "has_pii_access": False,
        "customer_id": CUSTOMER_A,
    }
    old = sessions.make(claims, user_id=uid)
    assert call(client, "GET", "/api/portal/me", old).status_code == 200  # cached ok

    revoked = call(client, "DELETE", f"{BASE}/{invited.json()['grant_id']}", admin)
    assert revoked.status_code == 200
    assert ("delete_user", uid) in access.st.calls
    assert access.db.user(EMAIL) is None

    # A fresh sign-in has no claims at all: 401 on staff and portal routes.
    fresh_claims = real_claims_lookup(uid)
    assert fresh_claims == {}
    fresh = sessions.make(fresh_claims, user_id=uid)
    for path in ("/api/fuel/mvp/forecasts", "/api/portal/me"):
        resp = call(client, "GET", path, fresh)
        assert resp.status_code == 401, (path, resp.text)

    # The pre-revoke session: cache invalidated, no clock advance needed.
    resp = call(client, "GET", "/api/portal/me", old)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "PORTAL_ACCESS_SUSPENDED"

    # Re-invite provisions a fresh SuperTokens user (no ProvisioningConflictError).
    again = call(client, "POST", BASE, admin, json={"email": EMAIL})
    assert again.status_code == 201
    assert access.db.user(EMAIL)["st_user_id"] not in (None, uid)
