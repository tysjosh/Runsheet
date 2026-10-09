"""Central default-deny for customer sessions (design §2.2; XR-1, XR-2, XR-3, XR-5).

[real-auth] ``main.app`` with fake verifiers, never ``override_auth``.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from middleware.auth_enforcement import is_public_route
from portal.scope import CUSTOMER_ALLOWED_EXACT, PORTAL_PATH_PREFIX
from tests.portal.conftest import (
    T1,
    api_routes,
    call,
    fill_path,
    health_container,
)

#: A representative staff route for the single-route cases.
STAFF_ROUTE = "/api/fuel/mvp/forecasts"


def _staff_inventory(app):
    """Every (method, filled path) a customer must be refused on."""
    out = []
    for route in api_routes(app):
        path = fill_path(route.path)
        if (
            path.startswith(PORTAL_PATH_PREFIX)
            or path in CUSTOMER_ALLOWED_EXACT
            or is_public_route(path)
        ):
            continue
        for method in sorted(route.methods):
            out.append((method, path))
    return out


def test_route_inventory_customer_forbidden(portal_app, client, portal_on, cA, handler_spy):
    """XR-1: every mounted non-public, non-allowlisted route refuses ``cA``."""
    inventory = _staff_inventory(portal_app)
    assert len(inventory) > 100, "route inventory unexpectedly small"
    failures = []
    for method, path in inventory:
        resp = call(client, method, path, cA)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 403 or body.get("error_code") != "PORTAL_ROUTE_FORBIDDEN":
            failures.append((method, path, resp.status_code, body.get("error_code")))
    assert failures == []
    assert handler_spy == [], "a handler ran for a refused customer request"


def test_new_route_is_denied_by_default(portal_app, client, portal_on, cA, handler_spy):
    """XR-2: a route added later is refused without any per-route check."""

    async def _new_route():
        return {"ok": True}

    route = APIRoute("/api/zz-new", _new_route, methods=["GET"])
    portal_app.router.routes.append(route)
    try:
        resp = call(client, "GET", "/api/zz-new", cA)
    finally:
        portal_app.router.routes.remove(route)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "PORTAL_ROUTE_FORBIDDEN"
    assert handler_spy == []


def test_allowlist_still_works(portal_app, client, portal_on, cA, handler_spy, monkeypatch):
    """XR-3: the account routes, the portal, health and SDK routes stay reachable."""
    monkeypatch.setattr(portal_app.state, "container", health_container(), raising=False)

    account = call(client, "GET", "/api/auth/account/me", cA)
    assert account.status_code == 200, account.text
    assert account.json()["roles"] == ["customer"]

    me = call(client, "GET", "/api/portal/me", cA)
    assert me.status_code == 200, me.text

    health = call(client, "GET", "/health", cA)
    assert health.status_code == 200, health.text

    refresh = call(client, "POST", "/auth/session/refresh", cA)
    assert refresh.status_code != 403
    assert "PORTAL_ROUTE_FORBIDDEN" not in refresh.text

    # The spy saw the handlers run, so it would have caught one in XR-1.
    assert "/api/auth/account/me" in handler_spy
    assert "/api/portal/me" in handler_spy


@pytest.mark.parametrize("path", [STAFF_ROUTE, "/api/portal/me"])
def test_mixed_role_session_refused(client, portal_on, sessions, portal_fakes, path, handler_spy):
    """XR-5: ``customer`` plus another role is refused everywhere (R2.2)."""
    mixed = sessions.customer(T1, "QA-PORTAL-CUST-A", roles=["customer", "admin"])
    portal_fakes.grants.grant(mixed)
    resp = call(client, "GET", path, mixed)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "PORTAL_IDENTITY_INVALID"
    assert handler_spy == []


def test_staff_session_unaffected(client, portal_on, sessions, portal_fakes, handler_spy):
    """A staff session is not touched by the central deny."""
    staff = sessions.staff("dispatcher")
    resp = call(client, "GET", "/api/auth/account/me", staff)
    assert resp.status_code == 200
    assert resp.json()["roles"] == ["dispatcher"]


async def test_get_tenant_context_repeats_the_verdict(sessions):
    """E1b: ``get_tenant_context`` refuses a customer on a staff path by itself."""
    from starlette.requests import Request

    from errors.exceptions import AppException
    from ops.middleware.tenant_guard import get_tenant_context

    session = sessions.customer(T1, "QA-PORTAL-CUST-A")

    def _request(path: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [(b"x-portal-test-session", session.key.encode())],
                "query_string": b"",
            }
        )

    with pytest.raises(AppException) as exc:
        await get_tenant_context(_request(STAFF_ROUTE))
    assert exc.value.error_code == "PORTAL_ROUTE_FORBIDDEN"
    assert exc.value.status_code == 403

    ctx = await get_tenant_context(_request("/api/portal/me"))
    assert ctx.customer_id == "QA-PORTAL-CUST-A"
    assert ctx.roles == ["customer"]
