"""Customer archive revokes portal sessions and keeps grants (design §1.7, PD22, R2.12).

[real-auth] Drives ``PATCH /api/commerce/customers/{id}`` with ``status=archived``.
"""
from __future__ import annotations

import logging

import pytest

from tests.portal.conftest import CUSTOMER_A, CUSTOMER_B, T1, call


class _ArchivingCustomers:
    """Wraps the portal fake customer service with ``archive``."""

    def __init__(self, inner, fail=False):
        self._inner = inner
        self.fail = fail

    async def get(self, tenant_id, customer_id):
        return await self._inner.get(tenant_id, customer_id)

    async def archive(self, tenant_id, customer_id):
        from errors.exceptions import conflict

        if self.fail:
            raise conflict("Cannot archive customer with open invoices")
        self._inner.customers[(tenant_id, customer_id)]["status"] = "archived"
        return dict(self._inner.customers[(tenant_id, customer_id)])


@pytest.fixture
def customers_api(monkeypatch, portal_on, portal_fakes):
    import commerce.api.customer_endpoints as ce
    from config.settings import clear_settings_cache

    monkeypatch.setenv("COMMERCE_CUSTOMERS_ENABLED", "true")
    clear_settings_cache()
    service = _ArchivingCustomers(portal_fakes.customers)
    monkeypatch.setattr(ce, "_customer_service", service)
    return service


def _invite(access, email, customer_id):
    import asyncio

    from auth.test_auth import issue_test_context

    admin = issue_test_context(T1, roles=("admin",), user_id="admin-t1")
    asyncio.run(access.service.invite(admin, customer_id, email))
    return access.db.user(email)["st_user_id"]


def test_archive_revokes_sessions_keeps_grants(client, sessions, access, customers_api):
    a1 = _invite(access, "a1@example.test", CUSTOMER_A)
    a2 = _invite(access, "a2@example.test", CUSTOMER_A)
    b1 = _invite(access, "b1@example.test", CUSTOMER_B)
    access.st.calls.clear()

    resp = call(
        client, "PATCH", f"/api/commerce/customers/{CUSTOMER_A}",
        sessions.staff("admin"), json={"status": "archived"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "archived"
    revoked = {c[1] for c in access.st.calls if c[0] == "revoke_sessions"}
    assert revoked == {a1, a2} and b1 not in revoked
    assert len(access.db.active(T1, CUSTOMER_A)) == 2
    assert access.db.user("a1@example.test")["customer_id"] == CUSTOMER_A
    assert [c[0] for c in access.st.writes()] == ["revoke_sessions", "revoke_sessions"]


def test_archive_succeeds_when_revoke_fails(client, sessions, access, customers_api, caplog):
    _invite(access, "a1@example.test", CUSTOMER_A)
    access.st.fail["revoke_sessions"] = RuntimeError("core unavailable")
    caplog.set_level(logging.ERROR)
    resp = call(
        client, "PATCH", f"/api/commerce/customers/{CUSTOMER_A}",
        sessions.staff("admin"), json={"status": "archived"},
    )
    assert resp.status_code == 200
    assert any("revoke on archive failed" in r.getMessage() for r in caplog.records)


def test_failed_archive_revokes_nothing(client, sessions, access, customers_api):
    _invite(access, "a1@example.test", CUSTOMER_A)
    customers_api.fail = True
    access.st.calls.clear()
    resp = call(
        client, "PATCH", f"/api/commerce/customers/{CUSTOMER_A}",
        sessions.staff("admin"), json={"status": "archived"},
    )
    assert resp.status_code == 409
    assert access.st.calls == []


def test_archive_without_portal_service_is_unchanged(client, sessions, portal_fakes, customers_api):
    from portal.services import portal_access_service as pas

    saved = pas.get_portal_access_service()
    pas.configure_portal_access(None)
    try:
        resp = call(
            client, "PATCH", f"/api/commerce/customers/{CUSTOMER_A}",
            sessions.staff("admin"), json={"status": "archived"},
        )
    finally:
        pas.configure_portal_access(saved)
    assert resp.status_code == 200
