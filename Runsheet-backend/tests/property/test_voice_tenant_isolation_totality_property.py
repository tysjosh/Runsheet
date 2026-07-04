"""
Property-based test for cross-tenant isolation totality across Surface B.

# Feature: dinee-voice-integration, Property 14: Tenant-isolation totality
# across Surface B

**Validates: Requirements 11.1, 11.2, 11.3, 11.4, 14.3, 17.3, 18.3, 20.3, 21.4**

Property 14 asserts that *every* tenant-scoped Surface B read/driver endpoint in
``fuel/voice/voice_read_driver_router.py`` isolates tenants **totally**: a
resource created by tenant A is invisible to a credential bound to tenant B, and
the tenant scope is always derived from the credential binding
(``voice.tenant_id``) — never from any client-supplied header/query/path/body
(Req 11.4).

The test is *parameterized over the mounted routes* so the guarantee is total:

    * A coverage test (``test_every_route_is_classified``) walks
      ``router.routes`` and asserts every mounted method+path is either an
      isolation-checked endpoint or an explicitly exempt one. Adding a new
      Surface B endpoint later without classifying it fails this test, so new
      endpoints are automatically pulled into the isolation contract.

    * The isolation property (``test_cross_tenant_resource_is_invisible``) seeds
      a full set of resources for tenant A, then issues the **byte-identical**
      request under two different credential bindings — once bound to tenant A
      and once bound to tenant B — for every classified endpoint:
        - single-resource endpoints (customer sites/tanks, order status/eta,
          customer deliveries, driver active-assignment, driver report) return
          the resource under binding A but a uniform HTTP 404 under binding B
          (Req 11.3 / 14.3 / 17.3 / 18.3 / 20.3 / 21.4);
        - list endpoints (customers/lookup, orders/lookup) return the matching
          rows under binding A but an empty array under binding B (Req 11.1 /
          11.2);
        - the driver-verify endpoint returns the driver object under binding A
          but never leaks it (``pinVerified: false``, no ``driver``) under
          binding B (Req 11.2).
      Because the request bytes are identical across both bindings and only the
      credential-bound tenant differs, a passing assertion proves the scope is
      derived exclusively from the credential binding (Req 11.4).

Endpoints that are not tenant-resource-scoped are exempt and documented:
``GET /auth/ping`` (credential test, no tenant data) and
``GET /products/validate`` (validates against the global fuel-product catalog,
not tenant data).

The handlers are driven through a FastAPI ``TestClient`` with
``get_voice_tenant`` overridden per tenant (as in
``test_voice_bearer_auth_property``); the repositories/services are recording,
tenant-scoped in-memory fakes wired via ``configure_voice_read_driver_router``
(as in ``test_voice_read_customer_product_property``), so no live Elasticsearch
is required and cross-tenant leakage is directly observable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import from_regex

from errors.exceptions import AppException, resource_not_found
from fuel.driver_report_repository import (
    DriverAssignmentNotFoundError,
    DriverReportCrossTenantAccessError,
)
from fuel.order_models import Driver, FuelOrder
from fuel.voice.voice_auth import VoiceTenantContext, get_voice_tenant
from fuel.voice.voice_read_driver_router import (
    configure_voice_read_driver_router,
    router,
)


# ===========================================================================
# Tenant-scoped recording fakes
#
# Each fake holds data keyed by tenant_id and only ever returns data for the
# tenant_id it is *called* with — mirroring the ``inject_tenant_filter`` +
# post-fetch re-validation discipline the real ES-backed repositories apply.
# ===========================================================================
class FakeCustomerService:
    """Tenant-scoped fake for ``CustomerService`` (lookup + get)."""

    def __init__(self, rows_by_tenant: dict[str, list[dict]]) -> None:
        self.rows_by_tenant = rows_by_tenant

    async def lookup_by_phone_or_account(self, tenant_id, *, phone=None, account_id=None):
        phone_val = str(phone).strip() if phone and str(phone).strip() else None
        acct_val = str(account_id).strip() if account_id and str(account_id).strip() else None
        matched: list[dict] = []
        for row in self.rows_by_tenant.get(tenant_id, []):
            row_phone = row.get("phone")
            row_acct = row.get("account_id")
            if phone_val is not None and row_phone and str(row_phone).strip() == phone_val:
                matched.append(row)
            elif acct_val is not None and row_acct and str(row_acct).strip() == acct_val:
                matched.append(row)
        return matched

    async def get(self, tenant_id, customer_id):
        for row in self.rows_by_tenant.get(tenant_id, []):
            if row.get("customer_id") == customer_id:
                return row
        # Unknown / cross-tenant customer degrades to a uniform 404 (Req 14.3).
        raise resource_not_found("Customer not found")


class _Site:
    """Minimal delivery-destination stand-in with ``.customer_id`` + dump."""

    def __init__(self, customer_id: str, site_id: str) -> None:
        self.customer_id = customer_id
        self.site_id = site_id

    def model_dump(self, mode: str = "python") -> dict:
        return {"customer_id": self.customer_id, "site_id": self.site_id}


class FakeDeliveryDestinationService:
    def __init__(self, sites_by_tenant: dict[str, list[_Site]]) -> None:
        self.sites_by_tenant = sites_by_tenant

    async def list(self, tenant_id):
        return list(self.sites_by_tenant.get(tenant_id, []))


class _Tank:
    def __init__(self, customer_id: str, tank_id: str) -> None:
        self.customer_id = customer_id
        self.tank_id = tank_id

    def model_dump(self, mode: str = "python") -> dict:
        return {"customer_id": self.customer_id, "tank_id": self.tank_id}


class FakeTankRepository:
    def __init__(self, tanks_by_tenant: dict[str, list[_Tank]]) -> None:
        self.tanks_by_tenant = tanks_by_tenant

    async def list_for_tenant(self, tenant_id, *, customer_id=None):
        return [
            t
            for t in self.tanks_by_tenant.get(tenant_id, [])
            if customer_id is None or t.customer_id == customer_id
        ]


class FakeFuelOrderRepository:
    """Tenant-scoped fake for ``FuelOrderRepository`` (search + get)."""

    def __init__(self, orders_by_tenant: dict[str, list[FuelOrder]]) -> None:
        self.orders_by_tenant = orders_by_tenant

    async def search(self, tenant_id, *, customer_phone=None, customer_id=None,
                     driver_id=None, sort=None, size=None):
        orders = list(self.orders_by_tenant.get(tenant_id, []))
        if customer_phone is not None:
            orders = [o for o in orders if o.customer_phone == customer_phone]
        if customer_id is not None:
            orders = [o for o in orders if o.customer_id == customer_id]
        if driver_id is not None:
            orders = [o for o in orders if o.assigned_driver_id == driver_id]
        orders.sort(key=lambda o: o.created_at, reverse=True)
        if size is not None:
            orders = orders[:size]
        return {"orders": orders}

    async def get(self, tenant_id, order_id):
        for order in self.orders_by_tenant.get(tenant_id, []):
            if order.order_id == order_id:
                return order
        return None


class FakeDriverRepository:
    def __init__(self, drivers_by_tenant: dict[str, list[Driver]]) -> None:
        self.drivers_by_tenant = drivers_by_tenant

    async def get(self, tenant_id, driver_id):
        for driver in self.drivers_by_tenant.get(tenant_id, []):
            if driver.driver_id == driver_id:
                return driver
        return None


class FakeDriverPinVault:
    """Verifies a PIN only for a driver owned by the calling tenant."""

    def __init__(self, pins_by_tenant: dict[str, dict[str, str]]) -> None:
        self.pins_by_tenant = pins_by_tenant

    async def verify_pin(self, tenant_id, driver_id, pin):
        return self.pins_by_tenant.get(tenant_id, {}).get(driver_id) == pin


class FakeDriverReportRepository:
    """Persists a report only when (tenant, driver, assignment) is valid."""

    def __init__(self, valid: set[tuple[str, str, str]]) -> None:
        self.valid = valid
        self.created: list[Any] = []

    async def create(self, tenant_id, report):
        key = (tenant_id, report.driver_id, report.assignment_id)
        if key not in self.valid:
            # Cross-tenant / unknown assignment → uniform 404 (Req 21.4/21.5).
            raise DriverAssignmentNotFoundError(
                tenant_id=tenant_id,
                driver_id=report.driver_id,
                assignment_id=report.assignment_id,
            )
        self.created.append(report)
        return report


# ===========================================================================
# Fixtures / builders
# ===========================================================================
_NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_order(tenant_id, order_id, customer_id, *, status, phone=None,
                driver_id=None, created_offset=0):
    """Build a minimal valid non-legacy :class:`FuelOrder`."""
    kwargs: dict[str, Any] = dict(
        order_id=order_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        customer_name="Cust Co",
        customer_phone=phone,
        ship_to_address="1 Depot Rd",
        ship_to_lat=1.0,
        ship_to_lon=2.0,
        product_code="DIESEL_2",
        gallons_requested=500.0,
        call_type="one_off",
        delivery_window_start=_NOW + timedelta(hours=1),
        delivery_window_end=_NOW + timedelta(hours=3),
        intake_channel="voice",
        intake_channel_id="chan-voice-1",
        status=status,
        assigned_driver_id=driver_id,
        assigned_run_id="run-1" if driver_id else None,
        source_schema_version="1.0.0",
        trace_id="trace-1",
        created_at=_NOW + timedelta(minutes=created_offset),
        updated_at=_NOW + timedelta(minutes=created_offset),
        last_event_timestamp=_NOW + timedelta(minutes=created_offset),
    )
    return FuelOrder(**kwargs)


def _make_driver(tenant_id, driver_id, phone):
    return Driver(
        driver_id=driver_id,
        tenant_id=tenant_id,
        driver_name="Dee Driver",
        phone=phone,
        status="active",
        last_event_timestamp=_NOW,
        source_schema_version="1.0.0",
        trace_id="trace-1",
        created_at=_NOW,
        updated_at=_NOW,
    )


class _Ctx:
    """Seeded identifiers for tenant A's resources + the two bindings."""

    def __init__(self, tenant_a, tenant_b, customer_id, order_delivered_id,
                 order_active_id, driver_id, phone, driver_phone):
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.customer_id = customer_id
        self.order_delivered_id = order_delivered_id
        self.order_active_id = order_active_id
        self.driver_id = driver_id
        self.phone = phone
        self.driver_phone = driver_phone
        self.pin = "8391"


def _wire_fakes(ctx: _Ctx) -> None:
    """Wire tenant-scoped fakes holding data for tenant A only."""
    customer_row = {
        "customer_id": ctx.customer_id,
        "display_name": "Cust Co",
        "phone": ctx.phone,
        "account_id": "ACCT-A1",
    }
    delivered = _make_order(
        ctx.tenant_a, ctx.order_delivered_id, ctx.customer_id,
        status="delivered", phone=ctx.phone, created_offset=0,
    )
    active = _make_order(
        ctx.tenant_a, ctx.order_active_id, ctx.customer_id,
        status="dispatched", phone=ctx.phone, driver_id=ctx.driver_id,
        created_offset=10,
    )
    driver = _make_driver(ctx.tenant_a, ctx.driver_id, ctx.driver_phone)

    configure_voice_read_driver_router(
        customer_service=FakeCustomerService({ctx.tenant_a: [customer_row]}),
        delivery_destination_service=FakeDeliveryDestinationService(
            {ctx.tenant_a: [_Site(ctx.customer_id, "site-a1")]}
        ),
        customer_tank_repository=FakeTankRepository(
            {ctx.tenant_a: [_Tank(ctx.customer_id, "tank-a1")]}
        ),
        fuel_order_repository=FakeFuelOrderRepository(
            {ctx.tenant_a: [delivered, active]}
        ),
        driver_repository=FakeDriverRepository({ctx.tenant_a: [driver]}),
        driver_pin_vault=FakeDriverPinVault(
            {ctx.tenant_a: {ctx.driver_id: ctx.pin}}
        ),
        driver_report_repository=FakeDriverReportRepository(
            {(ctx.tenant_a, ctx.driver_id, ctx.order_active_id)}
        ),
    )


def _build_client(bound_tenant: str) -> TestClient:
    """A TestClient whose ``get_voice_tenant`` is bound to ``bound_tenant``."""
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.to_dict()}
        )

    app.dependency_overrides[get_voice_tenant] = lambda: VoiceTenantContext(
        tenant_id=bound_tenant, channel_id=f"chan-{bound_tenant}"
    )
    return TestClient(app, raise_server_exceptions=True)


# ===========================================================================
# Endpoint classification (derived-from-routes coverage)
# ===========================================================================
#: Endpoints that are intentionally NOT tenant-resource isolated.
_EXEMPT_ROUTES: set[tuple[str, str]] = {
    ("GET", "/auth/ping"),          # credential test — carries no tenant data
    ("GET", "/products/validate"),  # validates the global product catalog
}


def _endpoint_specs(ctx: _Ctx) -> list[dict]:
    """Build the isolation spec for every tenant-scoped Surface B endpoint.

    Each spec: how to invoke tenant A's resource, the isolation category, and a
    positive assertion proving the resource is visible under binding A.
    """
    return [
        {
            "method": "GET",
            "path": "/customers/lookup",
            "request": {"url": "/customers/lookup", "params": {"phone": ctx.phone}},
            "category": "empty_list",
            "list_key": "customers",
        },
        {
            "method": "GET",
            "path": "/customers/{customer_id}/sites",
            "request": {"url": f"/customers/{ctx.customer_id}/sites"},
            "category": "not_found",
            "positive_status": 200,
        },
        {
            "method": "GET",
            "path": "/customers/{customer_id}/tanks",
            "request": {"url": f"/customers/{ctx.customer_id}/tanks"},
            "category": "not_found",
            "positive_status": 200,
        },
        {
            "method": "GET",
            "path": "/orders/lookup",
            "request": {"url": "/orders/lookup", "params": {"phone": ctx.phone}},
            "category": "empty_list",
            "list_key": "orders",
        },
        {
            "method": "GET",
            "path": "/orders/{order_id}/status",
            "request": {"url": f"/orders/{ctx.order_delivered_id}/status"},
            "category": "not_found",
            "positive_status": 200,
        },
        {
            "method": "GET",
            "path": "/orders/{order_id}/eta",
            "request": {"url": f"/orders/{ctx.order_delivered_id}/eta"},
            "category": "not_found",
            "positive_status": 200,
        },
        {
            "method": "GET",
            "path": "/customers/{customer_id}/deliveries",
            "request": {"url": f"/customers/{ctx.customer_id}/deliveries"},
            "category": "not_found",
            "positive_status": 200,
        },
        {
            "method": "GET",
            "path": "/drivers/verify",
            "request": {
                "url": "/drivers/verify",
                "params": {
                    "driverIdentifier": ctx.driver_id,
                    "phone": ctx.driver_phone,
                    "pin": ctx.pin,
                },
            },
            "category": "no_driver_leak",
        },
        {
            "method": "GET",
            "path": "/drivers/{driver_id}/active-assignment",
            "request": {"url": f"/drivers/{ctx.driver_id}/active-assignment"},
            "category": "not_found",
            "positive_status": 200,
        },
        {
            "method": "POST",
            "path": "/drivers/{driver_id}/assignments/{assignment_id}/reports",
            "request": {
                "url": (
                    f"/drivers/{ctx.driver_id}/assignments/"
                    f"{ctx.order_active_id}/reports"
                ),
                "json": {"kind": "note", "detail": "on my way"},
            },
            "category": "not_found",
            "positive_status": 200,
        },
    ]


def _invoke(client: TestClient, spec: dict):
    req = spec["request"]
    if spec["method"] == "GET":
        return client.get(req["url"], params=req.get("params"))
    return client.post(req["url"], params=req.get("params"), json=req.get("json"))


# ===========================================================================
# Strategies
# ===========================================================================
_tenant_ids = from_regex(r"tenant-[a-z0-9]{6,12}", fullmatch=True)
_customer_ids = from_regex(r"cust-[a-z0-9]{6,12}", fullmatch=True)
_order_ids = from_regex(r"ord-[a-z0-9]{6,12}", fullmatch=True)
_driver_ids = from_regex(r"drv-[a-z0-9]{6,12}", fullmatch=True)
_phones = from_regex(r"\+1[0-9]{10}", fullmatch=True)


@st.composite
def _contexts(draw) -> _Ctx:
    tenant_a = draw(_tenant_ids)
    tenant_b = draw(_tenant_ids)
    assume(tenant_a != tenant_b)
    order_delivered = draw(_order_ids)
    order_active = draw(_order_ids)
    assume(order_delivered != order_active)
    phone = draw(_phones)
    driver_phone = draw(_phones)
    return _Ctx(
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        customer_id=draw(_customer_ids),
        order_delivered_id=order_delivered,
        order_active_id=order_active,
        driver_id=draw(_driver_ids),
        phone=phone,
        driver_phone=driver_phone,
    )


# ===========================================================================
# Property 14 — coverage: every mounted route is classified
# ===========================================================================
def test_every_route_is_classified():
    """# Feature: dinee-voice-integration, Property 14: Tenant-isolation
    totality across Surface B (route coverage)

    **Validates: Requirements 11.1, 11.2, 11.3**

    Every mounted Surface B route must be either isolation-checked or explicitly
    exempt, so a new endpoint cannot be added without being pulled into the
    isolation contract.
    """
    ctx = _Ctx(
        "tenant-a", "tenant-b", "cust-a", "ord-d", "ord-a", "drv-a",
        "+15550000001", "+15550000002",
    )
    checked = {(s["method"], s["path"]) for s in _endpoint_specs(ctx)}
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            key = (method, route.path)
            assert key in checked or key in _EXEMPT_ROUTES, (
                f"Surface B route {key} is neither isolation-checked nor "
                f"exempt — classify it in _endpoint_specs or _EXEMPT_ROUTES"
            )


# ===========================================================================
# Property 14 — cross-tenant resources are invisible; scope from binding
# ===========================================================================
class TestTenantIsolationTotality:
    """# Feature: dinee-voice-integration, Property 14: Tenant-isolation
    totality across Surface B

    **Validates: Requirements 11.1, 11.2, 11.3, 11.4, 14.3, 17.3, 18.3, 20.3,
    21.4**
    """

    @given(ctx=_contexts())
    @settings(max_examples=100)
    def test_cross_tenant_resource_is_invisible(self, ctx: _Ctx):
        _wire_fakes(ctx)
        client_a = _build_client(ctx.tenant_a)
        client_b = _build_client(ctx.tenant_b)

        for spec in _endpoint_specs(ctx):
            resp_a = _invoke(client_a, spec)
            resp_b = _invoke(client_b, spec)

            # The byte-identical request under binding A sees the resource,
            # proving scope is derived from the credential binding (Req 11.4).
            # Under binding B the resource is totally invisible.
            if spec["category"] == "not_found":
                assert resp_a.status_code == spec["positive_status"], (
                    f"{spec['path']} should resolve under owning tenant"
                )
                assert resp_b.status_code == 404, (
                    f"{spec['path']} must return 404 cross-tenant, got "
                    f"{resp_b.status_code}"
                )
            elif spec["category"] == "empty_list":
                key = spec["list_key"]
                assert resp_a.status_code == 200
                assert resp_a.json()[key], (
                    f"{spec['path']} should return rows under owning tenant"
                )
                assert resp_b.status_code == 200
                assert resp_b.json()[key] == [], (
                    f"{spec['path']} must return an empty {key} list "
                    f"cross-tenant"
                )
            elif spec["category"] == "no_driver_leak":
                assert resp_a.status_code == 200
                body_a = resp_a.json()
                assert body_a.get("pinVerified") is True
                assert "driver" in body_a
                assert resp_b.status_code == 200
                body_b = resp_b.json()
                assert body_b.get("pinVerified") is False
                assert "driver" not in body_b, (
                    "driver-verify must never leak a cross-tenant driver"
                )
            else:  # pragma: no cover - guard against an unclassified category
                raise AssertionError(f"unknown category {spec['category']!r}")
