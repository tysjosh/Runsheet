"""Portal-user admin routes (design §1.7, D3; XR-7, AUD-1 admin part, AC7).

[real-auth] ``main.app`` with fake session verifiers; the service runs over
the in-memory fakes from the ``access`` fixture.
"""
from __future__ import annotations

import logging

import pytest

from tests.portal.conftest import (
    CUSTOMER_A,
    CUSTOMER_C,
    audit_records,
    call,
    fill_path,
    portal_admin_routes,
)

BASE = f"/api/commerce/customers/{CUSTOMER_A}/portal-users"
EMAIL = "buyer@example.test"


def _admin_paths(app, grant_id="pug_x", customer_id=CUSTOMER_A):
    out = []
    for method, route in portal_admin_routes(app):
        path = route.path.replace("{customer_id}", customer_id).replace("{grant_id}", grant_id)
        out.append((method, fill_path(path), route))
    return out


def test_four_admin_routes_mounted(portal_app):
    assert sorted((m, r.path) for m, r in portal_admin_routes(portal_app)) == [
        ("DELETE", "/api/commerce/customers/{customer_id}/portal-users/{grant_id}"),
        ("GET", "/api/commerce/customers/{customer_id}/portal-users"),
        ("POST", "/api/commerce/customers/{customer_id}/portal-users"),
        ("POST", "/api/commerce/customers/{customer_id}/portal-users/{grant_id}/resend"),
    ]


@pytest.mark.parametrize("role", ["dispatcher", "driver", "platform_admin"])
def test_non_admin_staff_forbidden(portal_app, client, portal_on, sessions, access, role):
    staff = sessions.staff(role)
    for method, path, _ in _admin_paths(portal_app):
        resp = call(client, method, path, staff, json={"email": EMAIL})
        assert resp.status_code == 403, (method, path, resp.text)
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"
    assert access.st.writes() == [] and access.db.grants == []


def test_customer_session_route_forbidden(portal_app, client, portal_on, cA, access, handler_spy):
    for method, path, _ in _admin_paths(portal_app):
        resp = call(client, method, path, cA, json={"email": EMAIL})
        assert resp.status_code == 403
        assert resp.json()["error_code"] == "PORTAL_ROUTE_FORBIDDEN"
    assert handler_spy == []


def test_admin_full_flow(client, portal_on, sessions, access):
    admin = sessions.staff("admin")

    resp = call(client, "POST", BASE, admin, json={"email": EMAIL})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {
        "grant_id", "email", "status", "password_set_link", "link_error",
        "email_sent", "already_invited",
    }
    assert body["status"] == "invited" and body["already_invited"] is False
    assert body["password_set_link"] and body["link_error"] is False
    grant_id = body["grant_id"]

    again = call(client, "POST", BASE, admin, json={"email": EMAIL})
    assert again.status_code == 200
    assert again.json()["already_invited"] is True and again.json()["grant_id"] == grant_id

    listed = call(client, "GET", BASE, admin)
    assert listed.status_code == 200
    (row,) = listed.json()["data"]
    assert set(row) == {"grant_id", "email", "status", "created_at", "revoked_at"}
    assert row["grant_id"] == grant_id and row["status"] == "invited"

    resent = call(client, "POST", f"{BASE}/{grant_id}/resend", admin)
    assert resent.status_code == 200
    assert set(resent.json()) == {"password_set_link", "link_error", "email_sent"}

    revoked = call(client, "DELETE", f"{BASE}/{grant_id}", admin)
    assert revoked.status_code == 200
    assert revoked.json() == {"grant_id": grant_id, "status": "revoked"}
    assert access.db.user(EMAIL) is None

    gone = call(client, "DELETE", f"{BASE}/{grant_id}", admin)
    assert gone.status_code == 404 and gone.json()["error_code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize("customer_id", ["QA-PORTAL-NOPE", CUSTOMER_C])
def test_unknown_or_other_tenant_customer_404_nothing_written(
    portal_app, client, portal_on, sessions, access, customer_id
):
    admin = sessions.staff("admin")
    for method, path, _ in _admin_paths(portal_app, customer_id=customer_id):
        resp = call(client, method, path, admin, json={"email": EMAIL})
        assert resp.status_code == 404, (method, path)
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"
    assert access.st.calls == [] and access.db.grants == [] and access.db.auth_users == {}


@pytest.mark.parametrize(
    "body",
    [{"email": "no-at-sign"}, {"email": "a@b"}, {"email": "x y@example.test"},
     {"email": EMAIL, "tenant_id": "qa-tenant-b"}, {"email": EMAIL, "roles": ["admin"]}, {}],
)
def test_invite_body_validation(client, portal_on, sessions, access, body):
    resp = call(client, "POST", BASE, sessions.staff("admin"), json=body)
    assert resp.status_code == 422, resp.text
    assert access.st.calls == [] and access.db.grants == []


def test_email_in_use_body_identical_over_http(client, portal_on, sessions, access):
    access.db.add_user("staff@example.test", "demo-tenant", roles=["admin"])
    access.db.add_user("other@example.test", "qa-tenant-b", roles=["customer"], customer_id=CUSTOMER_C)
    admin = sessions.staff("admin")
    bodies = []
    for email in ("staff@example.test", "other@example.test"):
        resp = call(client, "POST", BASE, admin, json={"email": email})
        assert resp.status_code == 409
        body = resp.json()
        body.pop("request_id", None)
        bodies.append(body)
    assert bodies[0] == bodies[1] and bodies[0]["error_code"] == "PORTAL_EMAIL_IN_USE"
    assert access.st.writes() == []


def test_service_unwired_503(client, portal_on, sessions, portal_fakes):
    from portal.services import portal_access_service as pas

    saved = pas.get_portal_access_service()
    pas.configure_portal_access(None)
    try:
        resp = call(client, "GET", BASE, sessions.staff("admin"))
    finally:
        pas.configure_portal_access(saved)
    assert resp.status_code == 503 and resp.json()["error_code"] == "PORTAL_UNAVAILABLE"


def test_admin_routes_one_audit_line(portal_app, client, portal_on, sessions, access, caplog):
    """AUD-1 for the admin routes: one clean line each, with the grant id."""
    caplog.set_level(logging.INFO, logger="portal_audit")
    admin = sessions.staff("admin")
    created = call(client, "POST", BASE, admin, json={"email": EMAIL})
    (line,) = audit_records(caplog)
    grant_id = created.json()["grant_id"]
    assert line["action"] == "portal_user_invite" and line["outcome"] == "ok"
    assert line["target_ids"] == {"customer_id": CUSTOMER_A, "grant_id": grant_id}
    assert line["actor_user_id"] == admin.user_id and line["tenant_id"] == "demo-tenant"
    assert line["customer_id"] is None
    assert created.headers.get("cache-control") == "no-store"

    for method, path, route in _admin_paths(portal_app, grant_id=grant_id):
        if method == "POST" and route.path.endswith("portal-users"):
            continue
        caplog.clear()
        resp = call(client, method, path, admin)
        lines = audit_records(caplog)
        assert len(lines) == 1, (method, path, lines)
        assert lines[0]["action"] == route.name
        assert lines[0]["status_code"] == resp.status_code
        for value in [*lines[0]["target_ids"].values(), lines[0]["actor_user_id"]]:
            assert "@" not in value and " " not in value
