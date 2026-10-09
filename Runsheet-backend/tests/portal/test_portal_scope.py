"""Scope construction and scope-parameter rejection (ISO-C-4, ISO-T-2 query part)."""
from __future__ import annotations

import pytest

from auth.test_auth import issue_test_context
from portal.api._authz import PortalScope
from tests.portal.conftest import T1, T2, CUSTOMER_C, call, fill_path, portal_routes


@pytest.mark.parametrize("field", ["tenant_id", "customer_id", "user_id"])
@pytest.mark.parametrize("value", ["", "   "])
def test_portal_scope_rejects_empty_ids(field, value):
    ids = {"tenant_id": T1, "customer_id": "c", "user_id": "u"}
    ids[field] = value
    with pytest.raises(ValueError):
        PortalScope(**ids, tenant=issue_test_context(T1, roles=["customer"], customer_id="c"))


@pytest.mark.parametrize("path", ["/api/portal/me", "/api/fuel/mvp/forecasts"])
def test_claimless_customer_identity_invalid(client, portal_on, sessions, portal_fakes, path):
    claimless = sessions.customer(T1, None)
    resp = call(client, "GET", path, claimless)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "PORTAL_IDENTITY_INVALID"


@pytest.mark.parametrize("query", [f"tenant_id={T2}", f"customer_id={CUSTOMER_C}"])
def test_scope_params_rejected(portal_app, client, portal_on, cA, portal_fakes, query):
    """ISO-T-2: every portal GET refuses tenant_id/customer_id with 422, no store call."""
    failures = []
    for method, route in portal_routes(portal_app):
        if method != "GET":
            continue
        resp = call(client, method, f"{fill_path(route.path)}?{query}", cA)
        body = resp.json()
        if resp.status_code != 422 or body.get("error_code") != "VALIDATION_ERROR":
            failures.append((route.path, resp.status_code, body.get("error_code")))
        else:
            assert body["details"]["fields"] == [query.split("=")[0]]
    assert failures == []
    assert portal_fakes.grants.calls == []
    assert portal_fakes.customers.calls == []


def test_me_shape_and_capabilities(client, portal_on, cA, portal_fakes, monkeypatch):
    """/me returns PortalMe; ordering follows the tenant's portal ordering
    setting (not the order_intake_pipeline flag) and needs the request path
    wired; payments off with no portal factory configured."""
    from ops.middleware import tenant_guard
    from portal.services import portal_order_service as pos
    from services.tenant_settings import TenantSettingsService

    class _Redis:
        def __init__(self):
            self.data = {}

        async def get(self, key):
            return self.data.get(key)

        async def set(self, key, value, ex=None, nx=False):
            self.data[key] = value

        async def delete(self, key):
            self.data.pop(key, None)

    redis = _Redis()
    tenant_guard.configure_tenant_guard(TenantSettingsService(redis_client=redis))
    saved = pos.get_configured_order_service()
    pos.configure_portal_orders(object())
    try:
        _me_shape(client, cA, portal_fakes, redis)
    finally:
        pos.configure_portal_orders(saved)
        tenant_guard.configure_tenant_guard(None)


def _me_shape(client, cA, portal_fakes, redis):
    # The shared rollout flag is off: portal ordering doesn't read it.
    portal_fakes.pipeline.state = "disabled"
    resp = call(client, "GET", "/api/portal/me", cA)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data", "request_id"}
    data = body["data"]
    assert set(data) == {
        "email",
        "customer_display_name",
        "supplier_name",
        "time_zone",
        "ordering_available",
        "invoices_available",
        "payments_available",
        "measurement_units",
        # PE4 (null while the balance can't be read).
        "open_balance_cents",
        "open_invoice_count",
        "overdue_count",
    }
    assert data["customer_display_name"] == "Customer A"
    assert data["supplier_name"] == "Your fuel supplier"
    assert data["time_zone"] == "America/Chicago"
    assert data["ordering_available"] is True  # default on, flag disabled
    assert data["invoices_available"] is True
    assert data["payments_available"] is False
    assert data["measurement_units"] == {"volume": "gal", "distance": "mi"}

    # The admin turns portal ordering off for the tenant.
    redis.data[f"tenant:{T1}:portal_ordering"] = "disabled"
    portal_fakes.pipeline.state = "active_auto"
    assert call(client, "GET", "/api/portal/me", cA).json()["data"]["ordering_available"] is False


def test_me_payments_available_with_enabled_connector(client, portal_on, cA, portal_fakes):
    from portal.services import portal_payment_service as pay

    async def _factory(_tenant_id):
        return object()

    pay.configure_portal_payments(connector_factory=_factory)
    assert call(client, "GET", "/api/portal/me", cA).json()["data"]["payments_available"] is True

    async def _broken(_tenant_id):
        raise RuntimeError("vault down")

    pay.configure_portal_payments(connector_factory=_broken)
    assert call(client, "GET", "/api/portal/me", cA).json()["data"]["payments_available"] is False


def test_me_invoices_unavailable_when_invoicing_off(client, set_flags, cA, portal_fakes):
    set_flags(invoicing=False)
    data = call(client, "GET", "/api/portal/me", cA).json()["data"]
    assert data["invoices_available"] is False
    assert data["payments_available"] is False
