"""
Unit tests for :mod:`fuel.api.order_endpoints`.

Task 7.5 of the order-intake-pipeline spec. Exercises the order REST
surface with mocked repositories so the suite stays decoupled from
Elasticsearch.

Covers:
* Two-tenant isolation: cross-tenant reads -> 404, cross-tenant writes -> 403
* Role-gating: dispatcher-only writes -> 403 for a driver JWT
* State-machine enforcement: placed -> in_transit rejected with 409
* Driver-availability guard: off_duty driver -> 409 driver_unavailable
* Error envelope consistency: every failure returns the ErrorResponse shape

Validates: Requirements 2.4, 2.5, 10.1, 10.2.1
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from fuel.api.order_endpoints import configure_order_endpoints, router
from fuel.order_models import FuelOrder, FuelOrderEvent
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fake pipeline / repository
# ---------------------------------------------------------------------------


@dataclass
class FakeIntakeResult:
    event_id: str = "evt_fake123"
    status: str = "processed"
    order_id: Optional[str] = "ord_fake456"


class FakePipeline:
    """Minimal fake of OrderIntakePipeline for endpoint tests."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    async def ingest_dispatcher(self, *, tenant, payload, request_id, client_event_id):
        self.calls.append({
            "method": "ingest_dispatcher",
            "tenant_id": tenant.tenant_id,
            "payload": payload,
            "request_id": request_id,
            "client_event_id": client_event_id,
        })
        return FakeIntakeResult()


class _FakeES:
    """Fake ES client that supports update_document for the order repo."""

    def __init__(self, repo: "FakeOrderRepository"):
        self._repo = repo

    async def update_document(self, index: str, doc_id: str, fields: Dict[str, Any]) -> None:
        """Apply partial update fields to the stored order."""
        # Find the order by doc_id (order_id) across all tenants
        for key, order in list(self._repo._orders.items()):
            if order.order_id == doc_id:
                order_dict = order.model_dump(mode="python")
                order_dict.update(fields)
                # Handle None hold_reason for non-on_hold statuses
                if order_dict.get("status") != "on_hold" and "hold_reason" in fields:
                    order_dict["hold_reason"] = fields["hold_reason"]
                # Re-parse datetimes from ISO strings if needed
                from datetime import datetime as dt
                for date_field in ("updated_at", "last_event_timestamp", "created_at"):
                    val = order_dict.get(date_field)
                    if isinstance(val, str):
                        order_dict[date_field] = dt.fromisoformat(val)
                self._repo._orders[key] = FuelOrder.model_validate(order_dict)
                return


class FakeOrderRepository:
    """In-memory fake of FuelOrderRepository for endpoint tests."""

    def __init__(self):
        self._orders: Dict[str, FuelOrder] = {}
        self._events: Dict[str, List[Any]] = {}
        self._es = _FakeES(self)
        self._orders_index = "fuel_orders_current"

    def _key(self, tenant_id: str, order_id: str) -> str:
        return f"{tenant_id}::{order_id}"

    def seed_order(self, order: FuelOrder) -> None:
        self._orders[self._key(order.tenant_id, order.order_id)] = order

    async def get(self, tenant_id: str, order_id: str) -> Optional[FuelOrder]:
        key = self._key(tenant_id, order_id)
        return self._orders.get(key)

    async def search(self, *, tenant_id: str, **kwargs) -> Dict[str, Any]:
        orders = [o for o in self._orders.values() if o.tenant_id == tenant_id]
        return {"orders": orders, "total": len(orders), "page": 1, "size": 20}

    async def upsert(self, tenant_id: str, order: FuelOrder) -> None:
        self._orders[self._key(tenant_id, order.order_id)] = order

    async def upsert_with_last_event_timestamp(
        self, tenant_id: str, order: FuelOrder | Dict[str, Any]
    ) -> None:
        model = order if isinstance(order, FuelOrder) else FuelOrder(**order)
        self._orders[self._key(tenant_id, model.order_id)] = model

    async def append_event(self, tenant_id: str, event: Any) -> None:
        if isinstance(event, dict):
            order_id = event.get("order_id", "unknown")
        else:
            order_id = event.order_id
        key = self._key(tenant_id, order_id)
        self._events.setdefault(key, []).append(event)

    async def get_events_for_order(
        self, tenant_id: str, order_id: str
    ) -> List[FuelOrderEvent]:
        key = self._key(tenant_id, order_id)
        return self._events.get(key, [])


class FakeDriverRepository:
    """In-memory fake of DriverRepository for endpoint tests."""

    def __init__(self):
        self._drivers: Dict[str, Any] = {}

    def seed_driver(self, tenant_id: str, driver_id: str, status: str = "on_duty"):
        self._drivers[f"{tenant_id}::{driver_id}"] = _FakeDriver(
            driver_id=driver_id, tenant_id=tenant_id, status=status
        )

    async def get(self, tenant_id: str, driver_id: str):
        return self._drivers.get(f"{tenant_id}::{driver_id}")


@dataclass
class _FakeDriver:
    driver_id: str
    tenant_id: str
    status: str = "on_duty"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _tenant_ctx(
    tenant_id: str = "tenant-A",
    roles: Optional[List[str]] = None,
    user_id: str = "user-1",
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        has_pii_access=False,
        roles=roles if roles is not None else ["dispatcher"],
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


def _make_order(
    order_id: str = "ord_abc123",
    tenant_id: str = "tenant-A",
    status: str = "placed",
    call_type: str = "one_off",
    delivery_window: bool = True,
) -> FuelOrder:
    """Create a valid FuelOrder for testing."""
    kwargs = dict(
        order_id=order_id,
        tenant_id=tenant_id,
        customer_id="cust-1",
        customer_name="Test Customer",
        ship_to_address="123 Main St",
        ship_to_lat=30.0,
        ship_to_lon=-90.0,
        product_code="DIESEL_2",
        gallons_requested=500.0,
        fill_to_full=False,
        call_type=call_type,
        intake_channel="dispatcher",
        intake_channel_id="dispatcher-default",
        status=status,
        source_schema_version="1.0",
        trace_id="trace-001",
        created_at=_NOW,
        updated_at=_NOW,
        last_event_timestamp=_NOW,
    )
    if delivery_window:
        kwargs["delivery_window_start"] = datetime(2026, 1, 16, 8, 0, 0, tzinfo=timezone.utc)
        kwargs["delivery_window_end"] = datetime(2026, 1, 16, 12, 0, 0, tzinfo=timezone.utc)
    if status == "on_hold":
        kwargs["hold_reason"] = "credit check pending"
    return FuelOrder(**kwargs)


def _build_app(
    *,
    tenant_id: str = "tenant-A",
    roles: Optional[List[str]] = None,
    repo: Optional[FakeOrderRepository] = None,
    pipeline: Optional[FakePipeline] = None,
    driver_repo: Optional[FakeDriverRepository] = None,
) -> Tuple[FastAPI, TestClient, FakeOrderRepository, FakePipeline, FakeDriverRepository]:
    """Build a FastAPI app with the order router wired in."""
    repo = repo or FakeOrderRepository()
    pipeline = pipeline or FakePipeline()
    driver_repo = driver_repo or FakeDriverRepository()

    configure_order_endpoints(
        order_intake_pipeline=pipeline,
        order_repository=repo,
        driver_repository=driver_repo,
    )

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.to_dict()},
        )

    ctx = _tenant_ctx(tenant_id=tenant_id, roles=roles)
    app.dependency_overrides[get_tenant_context] = lambda: ctx

    client = TestClient(app)
    return app, client, repo, pipeline, driver_repo


def _assert_error_envelope(resp_json: dict) -> None:
    """Assert the response matches the ErrorResponse shape."""
    assert "detail" in resp_json
    detail = resp_json["detail"]
    assert "error_code" in detail
    assert "message" in detail
    assert isinstance(detail["error_code"], str)
    assert isinstance(detail["message"], str)


# ---------------------------------------------------------------------------
# Tests — Two-tenant isolation (Req 9.1, 10.2.1)
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Cross-tenant reads -> 404, cross-tenant writes -> 403."""

    def test_cross_tenant_get_order_returns_404(self):
        """Tenant A cannot read Tenant B's order — gets 404."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_b1", tenant_id="tenant-B"))

        _, client, *_ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.get("/api/orders/ord_b1")
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "RESOURCE_NOT_FOUND"

    def test_cross_tenant_get_events_returns_404(self):
        """Tenant A cannot read Tenant B's order events — gets 404."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_b2", tenant_id="tenant-B"))

        _, client, *_ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.get("/api/orders/ord_b2/events")
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())

    def test_cross_tenant_status_transition_returns_404(self):
        """Tenant A cannot transition Tenant B's order status — gets 404."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_b3", tenant_id="tenant-B"))

        _, client, *_ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.patch(
            "/api/orders/ord_b3/status",
            json={"new_status": "confirmed"},
        )
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())

    def test_cross_tenant_assign_returns_404(self):
        """Tenant A cannot assign a driver to Tenant B's order — gets 404."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_b4", tenant_id="tenant-B"))

        _, client, *_ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.patch(
            "/api/orders/ord_b4/assign",
            json={"driver_id": "drv-1"},
        )
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())

    def test_cross_tenant_cancel_returns_404(self):
        """Tenant A cannot cancel Tenant B's order — gets 404."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_b5", tenant_id="tenant-B"))

        _, client, *_ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.post(
            "/api/orders/ord_b5/cancel",
            json={"reason": "test"},
        )
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())

    def test_cross_tenant_hold_returns_404(self):
        """Tenant A cannot hold Tenant B's order — gets 404."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_b6", tenant_id="tenant-B"))

        _, client, *_ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.post(
            "/api/orders/ord_b6/hold",
            json={"hold_reason": "test"},
        )
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())

    def test_list_only_shows_own_tenant_orders(self):
        """List only returns orders belonging to the caller's tenant."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_a1", tenant_id="tenant-A"))
        repo.seed_order(_make_order(order_id="ord_b1", tenant_id="tenant-B"))

        _, client, *_ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.get("/api/orders")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["order_id"] == "ord_a1"


# ---------------------------------------------------------------------------
# Tests — Role-gating (Req 10.1)
# ---------------------------------------------------------------------------


class TestRoleGating:
    """Dispatcher-only writes -> 403 for a driver JWT."""

    def test_driver_cannot_create_order(self):
        """A driver role cannot POST /api/orders — gets 403."""
        _, client, *_ = _build_app(roles=["driver"])

        resp = client.post(
            "/api/orders",
            json={
                "client_event_id": "evt-1",
                "customer_id": "cust-1",
                "customer_name": "Test",
                "ship_to_address": "123 Main",
                "ship_to_lat": 30.0,
                "ship_to_lon": -90.0,
                "product_code": "DIESEL_2",
                "gallons_requested": 500,
                "call_type": "one_off",
            },
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_driver_cannot_bulk_create(self):
        """A driver role cannot POST /api/orders/bulk — gets 403."""
        _, client, *_ = _build_app(roles=["driver"])

        resp = client.post(
            "/api/orders/bulk",
            json={"orders": [{"customer_id": "c1", "customer_name": "T",
                              "ship_to_address": "A", "ship_to_lat": 30.0,
                              "ship_to_lon": -90.0, "product_code": "D",
                              "call_type": "one_off"}]},
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_driver_cannot_transition_status(self):
        """A driver role cannot PATCH status — gets 403."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())

        _, client, *_ = _build_app(roles=["driver"], repo=repo)

        resp = client.patch(
            "/api/orders/ord_abc123/status",
            json={"new_status": "confirmed"},
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_driver_cannot_assign(self):
        """A driver role cannot PATCH assign — gets 403."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())

        _, client, *_ = _build_app(roles=["driver"], repo=repo)

        resp = client.patch(
            "/api/orders/ord_abc123/assign",
            json={"driver_id": "drv-1"},
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_driver_cannot_cancel(self):
        """A driver role cannot POST cancel — gets 403."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())

        _, client, *_ = _build_app(roles=["driver"], repo=repo)

        resp = client.post(
            "/api/orders/ord_abc123/cancel",
            json={"reason": "test"},
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_driver_cannot_hold(self):
        """A driver role cannot POST hold — gets 403."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())

        _, client, *_ = _build_app(roles=["driver"], repo=repo)

        resp = client.post(
            "/api/orders/ord_abc123/hold",
            json={"hold_reason": "test"},
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_driver_cannot_release_hold(self):
        """A driver role cannot POST release-hold — gets 403."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="on_hold"))

        _, client, *_ = _build_app(roles=["driver"], repo=repo)

        resp = client.post(
            "/api/orders/ord_abc123/release-hold",
            json={},
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_driver_can_read_a_single_order_and_its_events(self):
        """A driver role CAN still read a single order and its event timeline."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())

        _, client, *_ = _build_app(roles=["driver"], repo=repo)

        resp = client.get("/api/orders/ord_abc123")
        assert resp.status_code == 200

        resp = client.get("/api/orders/ord_abc123/events")
        assert resp.status_code == 200

    def test_driver_cannot_list_orders(self):
        """A driver role can no longer list every order in the tenant.

        ``GET /api/orders`` is the dispatcher list surface: it takes a
        ``driver_id`` filter but scopes nothing to the caller, so it now
        requires dispatcher or admin (driver-mobile-app Req 3.13). Drivers read
        their own work through ``GET /api/driver/work``.
        """
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())

        _, client, *_ = _build_app(roles=["driver"], repo=repo)

        resp = client.get("/api/orders")
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"


# ---------------------------------------------------------------------------
# Tests — State-machine enforcement (Req 1.2.3)
# ---------------------------------------------------------------------------


class TestStateMachineEnforcement:
    """placed -> in_transit rejected with 409 invalid_status_transition."""

    def test_placed_to_in_transit_rejected(self):
        """Direct placed -> in_transit is not allowed."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="placed"))

        _, client, *_ = _build_app(repo=repo)

        resp = client.patch(
            "/api/orders/ord_abc123/status",
            json={"new_status": "in_transit"},
        )
        assert resp.status_code == 409
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INVALID_STATUS_TRANSITION"

    def test_placed_to_confirmed_allowed(self):
        """placed -> confirmed is a valid transition."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="placed"))

        _, client, *_ = _build_app(repo=repo)

        resp = client.patch(
            "/api/orders/ord_abc123/status",
            json={"new_status": "confirmed"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

    def test_delivered_to_any_rejected(self):
        """Terminal status 'delivered' cannot transition anywhere."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="delivered"))

        _, client, *_ = _build_app(repo=repo)

        resp = client.patch(
            "/api/orders/ord_abc123/status",
            json={"new_status": "placed"},
        )
        assert resp.status_code == 409
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INVALID_STATUS_TRANSITION"

    def test_cancelled_to_any_rejected(self):
        """Terminal status 'cancelled' cannot transition anywhere."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="cancelled"))

        _, client, *_ = _build_app(repo=repo)

        resp = client.patch(
            "/api/orders/ord_abc123/status",
            json={"new_status": "placed"},
        )
        assert resp.status_code == 409
        _assert_error_envelope(resp.json())

    def test_cancel_from_delivered_rejected(self):
        """Cannot cancel a delivered order."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="delivered"))

        _, client, *_ = _build_app(repo=repo)

        resp = client.post(
            "/api/orders/ord_abc123/cancel",
            json={"reason": "changed mind"},
        )
        assert resp.status_code == 409
        _assert_error_envelope(resp.json())

    def test_missing_delivery_window_blocks_scheduled(self):
        """Transition to scheduled without a delivery window -> 409."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(
            status="confirmed",
            call_type="will_call",
            delivery_window=False,
        ))

        _, client, *_ = _build_app(repo=repo)

        resp = client.patch(
            "/api/orders/ord_abc123/status",
            json={"new_status": "scheduled"},
        )
        assert resp.status_code == 409
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "MISSING_DELIVERY_WINDOW"


# ---------------------------------------------------------------------------
# Tests — Driver-availability guard (Req 3.1.5)
# ---------------------------------------------------------------------------


class TestDriverAvailabilityGuard:
    """off_duty driver -> 409 driver_unavailable."""

    def test_off_duty_driver_rejected(self):
        """Assigning an off_duty driver returns 409."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())
        driver_repo = FakeDriverRepository()
        driver_repo.seed_driver("tenant-A", "drv-off", status="off_duty")

        _, client, *_ = _build_app(repo=repo, driver_repo=driver_repo)

        resp = client.patch(
            "/api/orders/ord_abc123/assign",
            json={"driver_id": "drv-off"},
        )
        assert resp.status_code == 409
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "DRIVER_UNAVAILABLE"

    def test_inactive_driver_rejected(self):
        """Assigning an inactive driver returns 409."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())
        driver_repo = FakeDriverRepository()
        driver_repo.seed_driver("tenant-A", "drv-inactive", status="inactive")

        _, client, *_ = _build_app(repo=repo, driver_repo=driver_repo)

        resp = client.patch(
            "/api/orders/ord_abc123/assign",
            json={"driver_id": "drv-inactive"},
        )
        assert resp.status_code == 409
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "DRIVER_UNAVAILABLE"

    def test_on_duty_driver_accepted(self):
        """Assigning an on_duty driver succeeds."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())
        driver_repo = FakeDriverRepository()
        driver_repo.seed_driver("tenant-A", "drv-ok", status="on_duty")

        _, client, *_ = _build_app(repo=repo, driver_repo=driver_repo)

        resp = client.patch(
            "/api/orders/ord_abc123/assign",
            json={"driver_id": "drv-ok"},
        )
        assert resp.status_code == 200
        assert resp.json()["assigned_driver_id"] == "drv-ok"

    def test_nonexistent_driver_returns_404(self):
        """Assigning a driver that doesn't exist returns 404."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())
        driver_repo = FakeDriverRepository()

        _, client, *_ = _build_app(repo=repo, driver_repo=driver_repo)

        resp = client.patch(
            "/api/orders/ord_abc123/assign",
            json={"driver_id": "drv-ghost"},
        )
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())


# ---------------------------------------------------------------------------
# Tests — Error envelope consistency (Req 10.1)
# ---------------------------------------------------------------------------


class TestErrorEnvelopeConsistency:
    """Every failure returns the ErrorResponse shape with error_code + message."""

    def test_404_has_error_envelope(self):
        _, client, *_ = _build_app()
        resp = client.get("/api/orders/nonexistent")
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())

    def test_403_has_error_envelope(self):
        _, client, *_ = _build_app(roles=["driver"])
        resp = client.post(
            "/api/orders",
            json={
                "client_event_id": "e1",
                "customer_id": "c1",
                "customer_name": "T",
                "ship_to_address": "A",
                "ship_to_lat": 30.0,
                "ship_to_lon": -90.0,
                "product_code": "D",
                "call_type": "one_off",
            },
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())

    def test_409_has_error_envelope(self):
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="delivered"))
        _, client, *_ = _build_app(repo=repo)
        resp = client.patch(
            "/api/orders/ord_abc123/status",
            json={"new_status": "placed"},
        )
        assert resp.status_code == 409
        _assert_error_envelope(resp.json())

    def test_400_missing_client_event_id_has_error_envelope(self):
        _, client, *_ = _build_app()
        resp = client.post(
            "/api/orders",
            json={
                "customer_id": "c1",
                "customer_name": "T",
                "ship_to_address": "A",
                "ship_to_lat": 30.0,
                "ship_to_lon": -90.0,
                "product_code": "D",
                "call_type": "one_off",
            },
        )
        assert resp.status_code == 400
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "MISSING_CLIENT_EVENT_ID"

    def test_400_bulk_exceeds_1000_rows(self):
        """POST /api/orders/bulk with > 1000 rows returns 400."""
        _, client, *_ = _build_app()
        rows = [
            {
                "customer_id": f"c{i}",
                "customer_name": f"Customer {i}",
                "ship_to_address": f"{i} Main St",
                "ship_to_lat": 30.0,
                "ship_to_lon": -90.0,
                "product_code": "DIESEL_2",
                "call_type": "one_off",
            }
            for i in range(1001)
        ]
        resp = client.post("/api/orders/bulk", json={"orders": rows})
        assert resp.status_code == 400
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "VALIDATION_ERROR"

    def test_400_hold_without_hold_reason(self):
        """POST /api/orders/{id}/hold with empty hold_reason returns 400."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="placed"))
        _, client, *_ = _build_app(repo=repo)

        # The HoldOrderRequest requires hold_reason with min_length=1,
        # but we test with a whitespace-only value that passes Pydantic
        # but fails the endpoint's strip check.
        resp = client.post(
            "/api/orders/ord_abc123/hold",
            json={"hold_reason": "   "},
        )
        assert resp.status_code == 400
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "MISSING_HOLD_REASON"


# ---------------------------------------------------------------------------
# Tests — Happy-path CRUD (Req 2.4, 2.5)
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Basic CRUD operations succeed for authorized callers."""

    def test_create_order_returns_201(self):
        _, client, *_ = _build_app()
        resp = client.post(
            "/api/orders",
            json={
                "client_event_id": "evt-1",
                "customer_id": "cust-1",
                "customer_name": "Test Customer",
                "ship_to_address": "123 Main St",
                "ship_to_lat": 30.0,
                "ship_to_lon": -90.0,
                "product_code": "DIESEL_2",
                "gallons_requested": 500,
                "call_type": "one_off",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "processed"
        assert body["order_id"] is not None

    def test_get_order_returns_200(self):
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())
        _, client, *_ = _build_app(repo=repo)

        resp = client.get("/api/orders/ord_abc123")
        assert resp.status_code == 200
        assert resp.json()["order_id"] == "ord_abc123"

    def test_list_orders_returns_200(self):
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())
        _, client, *_ = _build_app(repo=repo)

        resp = client.get("/api/orders")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_hold_and_release_hold(self):
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(status="placed"))
        _, client, *_ = _build_app(repo=repo)

        # Hold
        resp = client.post(
            "/api/orders/ord_abc123/hold",
            json={"hold_reason": "credit check"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "on_hold"
        assert resp.json()["hold_reason"] == "credit check"

        # Release hold
        resp = client.post(
            "/api/orders/ord_abc123/release-hold",
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "placed"


# ---------------------------------------------------------------------------
# Tests — Order resolver read (cross-module-entity-linkage task 2.1)
# ---------------------------------------------------------------------------


def _build_app_with_resolver(
    *,
    tenant_id: str = "tenant-A",
    repo: Optional[FakeOrderRepository] = None,
    resolver: Optional[Any] = None,
) -> Tuple[FastAPI, TestClient, FakeOrderRepository]:
    """Build an app wiring a custom RefResolver into the order endpoints."""
    repo = repo or FakeOrderRepository()
    pipeline = FakePipeline()
    driver_repo = FakeDriverRepository()

    configure_order_endpoints(
        order_intake_pipeline=pipeline,
        order_repository=repo,
        driver_repository=driver_repo,
        ref_resolver=resolver,
    )

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.to_dict()},
        )

    ctx = _tenant_ctx(tenant_id=tenant_id, roles=["dispatcher"])
    app.dependency_overrides[get_tenant_context] = lambda: ctx

    client = TestClient(app)
    return app, client, repo


class TestOrderResolverRead:
    """``GET /api/orders/{id}?expand=customer,asset,driver`` (Req 1.1, 5.1, 5.4)."""

    def test_non_expanded_read_unchanged_no_links(self):
        """Without ``expand`` the response carries no ``links`` key (Req 6.3)."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_order())
        _, client, *_ = _build_app(repo=repo)

        resp = client.get("/api/orders/ord_abc123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["order_id"] == "ord_abc123"
        assert "links" not in body

    def test_expand_resolves_customer_asset_driver(self):
        """Resolvable references come back as ``resolved`` summaries (Req 1.1)."""
        from services.ref_resolver import RefResolver

        order = _make_order()
        order = order.model_copy(
            update={"assigned_asset_id": "TRUCK_1", "assigned_driver_id": "DRV_1"}
        )
        repo = FakeOrderRepository()
        repo.seed_order(order)

        resolver = RefResolver()

        async def _customer(tenant_id, entity_id):
            return {"customer_id": entity_id, "display_name": "Acme Fuel"}

        async def _asset(tenant_id, entity_id):
            return {"asset_id": entity_id, "name": "Truck 1", "asset_type": "vehicle"}

        async def _driver(tenant_id, entity_id):
            return {"driver_id": entity_id, "driver_name": "Pat"}

        resolver.register("customer", _customer)
        resolver.register("asset", _asset)
        resolver.register("driver", _driver)

        _, client, *_ = _build_app_with_resolver(repo=repo, resolver=resolver)

        resp = client.get("/api/orders/ord_abc123?expand=customer,asset,driver")
        assert resp.status_code == 200
        body = resp.json()
        links = body["links"]
        assert links["customer"]["status"] == "resolved"
        assert links["customer"]["summary"]["display_name"] == "Acme Fuel"
        assert links["asset"]["status"] == "resolved"
        assert links["asset"]["summary"]["name"] == "Truck 1"
        assert links["driver"]["status"] == "resolved"
        assert links["driver"]["summary"]["driver_name"] == "Pat"
        # Base order contract is still present and unchanged.
        assert body["order_id"] == "ord_abc123"
        assert body["customer_id"] == "cust-1"

    def test_expand_unresolved_reference_marked_not_dropped(self):
        """A dangling reference returns an explicit ``unresolved`` marker (Req 5.4)."""
        from services.ref_resolver import RefResolver

        order = _make_order().model_copy(update={"assigned_asset_id": "GHOST"})
        repo = FakeOrderRepository()
        repo.seed_order(order)

        resolver = RefResolver()

        async def _none(tenant_id, entity_id):
            return None

        resolver.register("asset", _none)

        _, client, *_ = _build_app_with_resolver(repo=repo, resolver=resolver)

        resp = client.get("/api/orders/ord_abc123?expand=asset")
        assert resp.status_code == 200
        link = resp.json()["links"]["asset"]
        assert link["status"] == "unresolved"
        assert link["id"] == "GHOST"
        assert "summary" not in link

    def test_expand_absent_reference_marked_empty(self):
        """A null reference id resolves to ``empty`` (absent, not dangling)."""
        from services.ref_resolver import RefResolver

        repo = FakeOrderRepository()
        repo.seed_order(_make_order())  # assigned_asset_id is None
        resolver = RefResolver()

        _, client, *_ = _build_app_with_resolver(repo=repo, resolver=resolver)

        resp = client.get("/api/orders/ord_abc123?expand=asset")
        assert resp.status_code == 200
        link = resp.json()["links"]["asset"]
        assert link["status"] == "empty"
        assert link["id"] is None

    def test_unknown_expand_token_ignored(self):
        """Unknown ``expand`` tokens are ignored (forward-compatible)."""
        from services.ref_resolver import RefResolver

        repo = FakeOrderRepository()
        repo.seed_order(_make_order())
        resolver = RefResolver()

        _, client, *_ = _build_app_with_resolver(repo=repo, resolver=resolver)

        resp = client.get("/api/orders/ord_abc123?expand=bogus")
        assert resp.status_code == 200
        # No valid token -> treated as a non-expanded read (no links).
        assert "links" not in resp.json()

    def test_cross_tenant_order_still_404_with_expand(self):
        """Expand does not bypass tenant scoping (Req 5.3)."""
        from services.ref_resolver import RefResolver

        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_b1", tenant_id="tenant-B"))
        resolver = RefResolver()

        _, client, *_ = _build_app_with_resolver(
            tenant_id="tenant-A", repo=repo, resolver=resolver
        )

        resp = client.get("/api/orders/ord_b1?expand=customer")
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())


# ---------------------------------------------------------------------------
# Tests — Nullable ship-to coordinates on the response model
# ---------------------------------------------------------------------------


def _make_voice_order_without_coordinates(
    order_id: str = "ord_voice1",
    tenant_id: str = "tenant-A",
) -> FuelOrder:
    """A voice order held for review with no geocoded ship-to coordinates.

    This is a legitimate entity, not a corrupt one:
    ``FuelOrder._validate_coordinates`` exempts ``voice`` and ``legacy`` intake
    because a phone order captures only a free-text address, and a human
    reconciles the coordinates during review-hold.
    """
    return FuelOrder(
        order_id=order_id,
        tenant_id=tenant_id,
        customer_id="cust-1",
        customer_name="Phone Customer",
        ship_to_address="1200 Industrial Pkwy, Houston, TX",
        ship_to_lat=None,
        ship_to_lon=None,
        product_code="DIESEL_2",
        gallons_requested=500.0,
        call_type="will_call",
        intake_channel="voice",
        intake_channel_id="voice-default",
        status="on_hold",
        hold_reason="awaiting address confirmation",
        source_schema_version="1.0",
        trace_id="trace-voice",
        created_at=_NOW,
        updated_at=_NOW,
        last_event_timestamp=_NOW,
    )


class TestNullableShipToCoordinates:
    """``OrderResponse`` must not be stricter than the entity it serializes.

    ``OrderResponse.ship_to_lat``/``ship_to_lon`` were declared as required
    ``float`` while :class:`fuel.order_models.FuelOrder` permits ``None`` for
    ``voice`` and ``legacy`` intake. The consequence was worse than a wrong
    field: ``list_orders`` builds its page inside a list comprehension, so one
    coordinate-less voice order raised ``ValidationError`` and returned 500 for
    **the entire page** — the observed failure was 2 on-hold voice orders taking
    down a list of 125.
    """

    def test_list_serializes_an_order_with_null_coordinates(self):
        """A voice order with no coordinates does not 500 the list page."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_voice_order_without_coordinates())
        _, client, *_ = _build_app(repo=repo)

        resp = client.get("/api/orders")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["order_id"] == "ord_voice1"
        assert item["ship_to_lat"] is None
        assert item["ship_to_lon"] is None
        # The address is what a dispatcher actually reconciles against.
        assert item["ship_to_address"] == "1200 Industrial Pkwy, Houston, TX"

    def test_one_coordinateless_order_does_not_hide_its_neighbours(self):
        """The whole page still renders, not just the well-formed rows.

        This is the property that failed in production: the page is built by a
        list comprehension, so a single invalid row is not skipped — it aborts
        the response.
        """
        repo = FakeOrderRepository()
        repo.seed_order(_make_order(order_id="ord_ok1"))
        repo.seed_order(_make_voice_order_without_coordinates())
        repo.seed_order(_make_order(order_id="ord_ok2"))
        _, client, *_ = _build_app(repo=repo)

        resp = client.get("/api/orders")

        assert resp.status_code == 200
        returned = {item["order_id"] for item in resp.json()["items"]}
        assert returned == {"ord_ok1", "ord_voice1", "ord_ok2"}

    def test_single_order_read_serializes_null_coordinates(self):
        """``GET /api/orders/{id}`` shares ``OrderResponse`` and so shared the bug."""
        repo = FakeOrderRepository()
        repo.seed_order(_make_voice_order_without_coordinates())
        _, client, *_ = _build_app(repo=repo)

        resp = client.get("/api/orders/ord_voice1")

        assert resp.status_code == 200
        assert resp.json()["ship_to_lat"] is None

    def test_create_still_requires_coordinates(self):
        """Relaxing the *response* must not relax dispatcher *intake*.

        ``CreateOrderRequest`` keeps both coordinates required: a dispatcher
        creating an order has an address on screen and must geocode it, so
        downstream routing has a stop at intake.
        """
        _, client, *_ = _build_app()

        resp = client.post(
            "/api/orders",
            json={
                "client_event_id": "evt-nocoord",
                "customer_id": "cust-1",
                "customer_name": "Test Customer",
                "ship_to_address": "123 Main St",
                "product_code": "DIESEL_2",
                "gallons_requested": 500,
                "call_type": "one_off",
            },
        )

        assert resp.status_code == 422
        missing = {
            tuple(err["loc"][-1:]) for err in resp.json()["detail"]
        }
        assert ("ship_to_lat",) in missing
        assert ("ship_to_lon",) in missing
