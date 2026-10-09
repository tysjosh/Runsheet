"""Live-binding check (design §2.3 step 4, E6; PRV-5)."""
from __future__ import annotations

import pytest

from errors.exceptions import AppException
from portal.services.principal import PortalPrincipalChecker
from tests.portal.conftest import (
    CUSTOMER_A,
    CUSTOMER_B,
    T1,
    FakeCustomerService,
    FakeGrantStore,
    call,
)


def _checker(grants, customers, clock):
    return PortalPrincipalChecker(
        customer_service=customers,
        grant_store=grants,
        cache_seconds=60,
        clock=lambda: clock[0],
    )


def test_archived_customer_suspended(client, portal_on, cA, portal_fakes):
    portal_fakes.customers.add(T1, CUSTOMER_A, status="archived")
    resp = call(client, "GET", "/api/portal/me", cA)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "PORTAL_ACCESS_SUSPENDED"


def test_revoked_grant_suspended(client, portal_on, cA, portal_fakes):
    portal_fakes.grants.revoke(cA)
    resp = call(client, "GET", "/api/portal/me", cA)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "PORTAL_ACCESS_SUSPENDED"


def test_store_error_503_not_cached(client, portal_on, cA, portal_fakes):
    portal_fakes.grants.fail = RuntimeError("db down")
    resp = call(client, "GET", "/api/portal/me", cA)
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "PORTAL_UNAVAILABLE"
    portal_fakes.grants.fail = None
    assert call(client, "GET", "/api/portal/me", cA).status_code == 200


async def test_cache_ttl_respected():
    grants, customers, clock = FakeGrantStore(), FakeCustomerService(), [0.0]
    customers.add(T1, CUSTOMER_A)
    grants.grants[(T1, "u1", CUSTOMER_A)] = {"grant_id": "g1", "first_seen_at": None}
    checker = _checker(grants, customers, clock)

    assert await checker.check(T1, "u1", CUSTOMER_A) == "ok"
    grants.grants.clear()  # revoked in the store
    clock[0] = 59.0
    assert await checker.check(T1, "u1", CUSTOMER_A) == "ok"  # still cached
    clock[0] = 60.5
    assert await checker.check(T1, "u1", CUSTOMER_A) == "revoked"


async def test_cached_ok_not_reused_for_other_customer():
    grants, customers, clock = FakeGrantStore(), FakeCustomerService(), [0.0]
    customers.add(T1, CUSTOMER_A)
    customers.add(T1, CUSTOMER_B)
    grants.grants[(T1, "u1", CUSTOMER_A)] = {"grant_id": "g1", "first_seen_at": None}
    checker = _checker(grants, customers, clock)

    assert await checker.check(T1, "u1", CUSTOMER_A) == "ok"
    assert await checker.check(T1, "u1", CUSTOMER_B) == "revoked"


async def test_store_error_raises_503_and_is_not_cached():
    grants, customers, clock = FakeGrantStore(), FakeCustomerService(), [0.0]
    customers.add(T1, CUSTOMER_A)
    grants.grants[(T1, "u1", CUSTOMER_A)] = {"grant_id": "g1", "first_seen_at": None}
    grants.fail = RuntimeError("db down")
    checker = _checker(grants, customers, clock)

    with pytest.raises(AppException) as exc:
        await checker.check(T1, "u1", CUSTOMER_A)
    assert exc.value.status_code == 503
    assert exc.value.error_code == "PORTAL_UNAVAILABLE"
    grants.fail = None
    assert await checker.check(T1, "u1", CUSTOMER_A) == "ok"


async def test_first_seen_set_once():
    grants, customers, clock = FakeGrantStore(), FakeCustomerService(), [0.0]
    customers.add(T1, CUSTOMER_A)
    grants.grants[(T1, "u1", CUSTOMER_A)] = {"grant_id": "g1", "first_seen_at": None}
    checker = _checker(grants, customers, clock)

    await checker.check(T1, "u1", CUSTOMER_A)
    clock[0] = 120.0  # past the TTL: re-evaluated, grant now has first_seen_at
    await checker.check(T1, "u1", CUSTOMER_A)
    marks = [c for c in grants.calls if c[0] == "mark_first_seen"]
    assert marks == [("mark_first_seen", {"grant_id": "g1"})]


async def test_invalidate_user_drops_cached_verdicts():
    grants, customers, clock = FakeGrantStore(), FakeCustomerService(), [0.0]
    customers.add(T1, CUSTOMER_A)
    grants.grants[(T1, "u1", CUSTOMER_A)] = {"grant_id": "g1", "first_seen_at": None}
    checker = _checker(grants, customers, clock)

    assert await checker.check(T1, "u1", CUSTOMER_A) == "ok"
    grants.grants.clear()
    checker.invalidate_user("u1")
    assert await checker.check(T1, "u1", CUSTOMER_A) == "revoked"


def test_unwired_checker_fails_closed(client, portal_on, cA, monkeypatch):
    from portal.services import principal

    monkeypatch.setattr(principal, "_checker", None)
    resp = call(client, "GET", "/api/portal/me", cA)
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "PORTAL_UNAVAILABLE"
