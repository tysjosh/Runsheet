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


# ---------------------------------------------------------------------------
# Orders and tanks (FEAT-003): ISO-C-1, ISO-C-2, ISO-T-1
# ---------------------------------------------------------------------------

import json  # noqa: E402

from tests.portal.conftest import seed_isolation  # noqa: E402


def _id_routes(ids_order: str, ids_tank: str):
    """Every order/tank id route, as (method, path, json)."""
    return [
        ("GET", f"/api/portal/orders/{ids_order}", None),
        ("POST", f"/api/portal/orders/{ids_order}/cancel", {}),
        ("GET", f"/api/portal/tanks/{ids_tank}", None),
        ("GET", f"/api/portal/tanks/{ids_tank}/deliveries", None),
    ]


def _normalized(resp, *idents):
    body = dict(resp.json())
    body.pop("request_id", None)
    text = json.dumps(body, sort_keys=True)
    for ident in idents:
        text = text.replace(ident, "<id>")
    return text


def _assert_like_unknown(client, session, foreign_order, foreign_tank):
    unknown_order, unknown_tank = "QA-ORD-UNKNOWN-1", "QA-TANK-UNKNOWN-1"
    unknown = _id_routes(unknown_order, unknown_tank)
    foreign = _id_routes(foreign_order, foreign_tank)
    for (m, path_u, js), (_m, path_f, _js) in zip(unknown, foreign):
        miss = call(client, m, path_u, session, json=js)
        hit = call(client, m, path_f, session, json=js)
        assert miss.status_code == 404, (path_u, miss.text)
        assert hit.status_code == 404, (path_f, hit.text)
        assert miss.json()["error_code"] == "RESOURCE_NOT_FOUND"
        assert _normalized(hit, foreign_order, foreign_tank) == _normalized(
            miss, unknown_order, unknown_tank
        ), path_f


def test_customer_a_cannot_read_b(client, portal_on, portal_orders, cA):
    """ISO-C-1 (order/tank part): B's ids answer exactly like unknown ids."""
    ids = seed_isolation(portal_orders)
    for order_id in (ids.order_b, ids.order_b_portal):
        _assert_like_unknown(client, cA, order_id, ids.tank_b)
    # B's portal order is untouched by A's cancel attempt.
    assert portal_orders.order(ids.order_b_portal)["status"] == "on_hold"


def _list_ids(client, session):
    orders = call(client, "GET", "/api/portal/orders", session)
    tanks = call(client, "GET", "/api/portal/tanks", session)
    assert orders.status_code == 200 and tanks.status_code == 200
    order_ids = {o["order_id"] for o in orders.json()["data"]}
    tank_data = tanks.json()["data"]
    tank_ids = {t["customer_tank_id"] for t in tank_data}
    next_ids = {t["next_delivery"]["order_id"] for t in tank_data if t["next_delivery"]}
    order_tanks = {o["tank"]["customer_tank_id"] for o in orders.json()["data"] if o["tank"]}
    return order_ids, tank_ids, next_ids, order_tanks


def test_lists_contain_only_own_rows(client, portal_on, portal_orders, cA):
    """ISO-C-2 (order/tank part)."""
    ids = seed_isolation(portal_orders)
    order_ids, tank_ids, next_ids, order_tanks = _list_ids(client, cA)
    assert order_ids == {ids.order_a, ids.order_a_portal}
    assert tank_ids == {ids.tank_a}
    assert next_ids == {ids.order_a}
    assert order_tanks == {ids.tank_a}
    history = call(client, "GET", f"/api/portal/tanks/{ids.tank_a}/deliveries", cA)
    assert history.status_code == 200 and history.json()["data"] == []


def test_tenant_boundary(client, portal_on, portal_orders, cA, cC):
    """ISO-T-1 (order/tank part): T2 ids are unknown to cA; cC sees only T2."""
    ids = seed_isolation(portal_orders)
    for order_id in (ids.order_c, ids.order_c_portal):
        _assert_like_unknown(client, cA, order_id, ids.tank_c)
    order_ids, tank_ids, next_ids, _ = _list_ids(client, cC)
    assert order_ids == {ids.order_c, ids.order_c_portal}
    assert tank_ids == {ids.tank_c}
    assert next_ids == {ids.order_c}
    # And the reverse: cC can't reach A's rows.
    _assert_like_unknown(client, cC, ids.order_a_portal, ids.tank_a)


# ---------------------------------------------------------------------------
# Invoices, PDF and CSV (FEAT-004): ISO-C-1, ISO-C-2, ISO-T-1
# ---------------------------------------------------------------------------

import csv  # noqa: E402
import io  # noqa: E402

from tests.portal.conftest import seed_invoice_isolation  # noqa: E402

_VISIBLE = ("open", "partial", "paid", "overdue", "void")


def _assert_invoice_like_unknown(client, session, foreign_id, *, pdf=False):
    """Detail (and, with ``pdf``, the PDF route: 5/min per user, so callers
    check it for two ids only)."""
    unknown = "QA-INV-UNKNOWN-1"
    for suffix in ("", "/pdf") if pdf else ("",):
        miss = call(client, "GET", f"/api/portal/invoices/{unknown}{suffix}", session)
        hit = call(client, "GET", f"/api/portal/invoices/{foreign_id}{suffix}", session)
        assert miss.status_code == 404 and hit.status_code == 404, (foreign_id, suffix)
        assert miss.json()["error_code"] == "RESOURCE_NOT_FOUND"
        assert _normalized(hit, foreign_id) == _normalized(miss, unknown), (foreign_id, suffix)


def _invoice_list_and_csv(client, session):
    listed = call(client, "GET", "/api/portal/invoices", session)
    exported = call(client, "GET", "/api/portal/invoices/export", session)
    assert listed.status_code == 200 and exported.status_code == 200
    rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8").lstrip("\ufeff"))))
    return {i["invoice_id"] for i in listed.json()["data"]}, {r[0] for r in rows[1:]}


def test_customer_a_cannot_read_b_invoices(client, portal_on, portal_fakes, cA):
    """ISO-C-1 (invoice part): B's invoices and PDFs answer like unknown ids."""
    inv = seed_invoice_isolation(portal_fakes.invoices)
    for status in ("draft",) + _VISIBLE:
        _assert_invoice_like_unknown(client, cA, inv["B"][status],
                                     pdf=status in ("draft", "open"))


def test_invoice_lists_and_csv_contain_only_own_rows(client, portal_on, portal_fakes, cA):
    """ISO-C-2 (invoice part): list and parsed CSV contain only A's invoices."""
    inv = seed_invoice_isolation(portal_fakes.invoices)
    listed, csv_numbers = _invoice_list_and_csv(client, cA)
    assert listed == {inv["A"][s] for s in _VISIBLE}
    assert csv_numbers == {f"INV-{inv['A'][s]}" for s in _VISIBLE}


def test_invoice_tenant_boundary(client, portal_on, portal_fakes, cA, cC):
    """ISO-T-1 (invoice part): T2 invoices are unknown to cA; cC sees only T2."""
    inv = seed_invoice_isolation(portal_fakes.invoices)
    for status in ("draft",) + _VISIBLE:
        _assert_invoice_like_unknown(client, cA, inv["C"][status],
                                     pdf=status in ("draft", "open"))
    listed, csv_numbers = _invoice_list_and_csv(client, cC)
    assert listed == {inv["C"][s] for s in _VISIBLE}
    assert csv_numbers == {f"INV-{inv['C'][s]}" for s in _VISIBLE}
    _assert_invoice_like_unknown(client, cC, inv["A"]["open"], pdf=True)
