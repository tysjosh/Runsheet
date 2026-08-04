"""
Unit tests for the Driver_Work_API router (``driver/api/work_endpoints.py``).

Covers the three reads task 8.3 adds: the paged assigned-order list, the
single-order detail, and ``GET /api/driver/me``. The assertions that matter here
are the ones about *scope*: the handlers take the ``(tenant_id, driver_id)`` pair
from the verified context and there is no request-supplied ``driver_id`` on the
surface at all (R3.12), another driver's order is a 404 (R3.6), and every page
carries the ``message_endpoints.py`` pagination envelope (R3.14).

Validates: Requirements 1.11, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.12, 3.14, 13.10,
15.6
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.api.work_endpoints import (
    configure_work_endpoints,
    router as work_router,
)
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "t1"
DRIVER = "drv-1"
OTHER_DRIVER = "drv-2"
ORDER = "ord-1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _order_doc(**overrides) -> dict:
    doc = {
        "order_id": ORDER,
        "tenant_id": TENANT,
        "status": "dispatched",
        "assigned_driver_id": DRIVER,
        "assigned_asset_id": "truck-1",
        "assigned_run_id": "",
        "delivery_window_start": "2026-01-05T08:00:00+00:00",
        "delivery_window_end": "2026-01-05T12:00:00+00:00",
        "ship_to_address": "1 Depot Rd",
        "ship_to_lat": 29.76,
        "ship_to_lon": -95.37,
        "customer_name": "Acme Fuel",
        "customer_phone": "+15550001111",
        "product_code": "DIESEL_2",
        "gallons_requested": 3200.0,
        "pod_otp": "845213",
    }
    doc.update(overrides)
    return doc


class _FakeOrderRepository:
    """Stands in for ``FuelOrderRepository`` on both read paths."""

    def __init__(self, *, orders=None, order=None, total=None):
        self._orders = orders if orders is not None else []
        self._order = order
        self._total = len(self._orders) if total is None else total
        self.search_calls: list[dict] = []

    async def search_for_driver(
        self,
        tenant_id,
        driver_id,
        *,
        statuses=(),
        window_start=None,
        window_end=None,
        page=1,
        size=50,
    ):
        self.search_calls.append(
            {
                "tenant_id": tenant_id,
                "driver_id": driver_id,
                "statuses": statuses,
                "window_start": window_start,
                "window_end": window_end,
                "page": page,
                "size": size,
            }
        )
        return {
            "orders": list(self._orders),
            "total": self._total,
            "page": page,
            "size": size,
        }

    async def get(self, tenant_id, order_id):
        return self._order


class _FakeES:
    """Answers the ``drivers_current`` read behind ``GET /api/driver/me``."""

    def __init__(self, *, sources=()):
        self._sources = list(sources)

    async def search_documents(self, index, query, size=100):
        return {"hits": {"hits": [{"_source": s} for s in self._sources]}}


def _driver_record(**overrides) -> dict:
    record = {
        "driver_id": DRIVER,
        "tenant_id": TENANT,
        "driver_name": "Ada Driver",
        "assigned_truck_id": "truck-1",
        "status": "on_duty",
        "duty_status_updated_at": "2026-01-05T07:00:00+00:00",
    }
    record.update(overrides)
    return record


def _make_app(*, order_repository=None, es_service=None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(work_router)
    configure_work_endpoints(
        es_service=es_service if es_service is not None else _FakeES(),
        order_repository=order_repository,
        job_service=None,
        redis_client=None,
    )
    install_test_auth(app)
    return app


def _driver_headers(**kwargs) -> dict:
    kwargs.setdefault("roles", ["driver"])
    kwargs.setdefault("driver_id", DRIVER)
    return auth_headers(TENANT, sub="user-1", **kwargs)


# ---------------------------------------------------------------------------
# GET /api/driver/work
# ---------------------------------------------------------------------------


class TestListWork:
    """The paged assigned-order list."""

    def test_returns_the_drivers_own_orders_with_pagination(self):
        """Validates: Requirements 3.1, 3.2, 3.14"""
        repo = _FakeOrderRepository(orders=[_order_doc()], total=1)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get("/api/driver/work", headers=_driver_headers())

        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"] == {
            "page": 1,
            "size": 50,
            "total": 1,
            "total_pages": 1,
        }
        assert "request_id" in body
        (order,) = body["data"]
        assert order["order_id"] == ORDER
        assert order["destination"]["address"] == "1 Depot Rd"
        # The scope came from the verified context, never from the request.
        assert repo.search_calls[0]["tenant_id"] == TENANT
        assert repo.search_calls[0]["driver_id"] == DRIVER

    def test_query_driver_id_cannot_widen_the_scope(self):
        """A ``driver_id`` in the query string is not a scoping parameter (R3.12).

        Validates: Requirements 3.12
        """
        repo = _FakeOrderRepository(orders=[_order_doc()], total=1)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get(
            f"/api/driver/work?driver_id={OTHER_DRIVER}",
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert repo.search_calls[0]["driver_id"] == DRIVER

    def test_repeatable_status_and_window_filters_reach_the_repository(self):
        """Validates: Requirements 3.4, 3.5"""
        repo = _FakeOrderRepository(orders=[], total=0)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get(
            "/api/driver/work"
            "?status=dispatched&status=in_transit"
            "&window_start=2026-01-05T00:00:00%2B00:00"
            "&window_end=2026-01-06T00:00:00%2B00:00"
            "&page=2&size=10",
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        call = repo.search_calls[0]
        assert call["statuses"] == ("dispatched", "in_transit")
        assert call["window_start"] == "2026-01-05T00:00:00+00:00"
        assert call["window_end"] == "2026-01-06T00:00:00+00:00"
        assert call["page"] == 2
        assert call["size"] == 10

    def test_no_status_filter_leaves_the_repository_default(self):
        """Validates: Requirements 3.3"""
        repo = _FakeOrderRepository(orders=[], total=0)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get("/api/driver/work", headers=_driver_headers())

        assert resp.status_code == 200
        assert repo.search_calls[0]["statuses"] == ()

    def test_customer_phone_omitted_without_pii_access(self):
        """Validates: Requirements 15.6"""
        repo = _FakeOrderRepository(orders=[_order_doc()], total=1)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get("/api/driver/work", headers=_driver_headers())

        (order,) = resp.json()["data"]
        assert "customer_phone" not in order

    def test_customer_phone_returned_with_pii_access(self):
        """Validates: Requirements 15.6"""
        repo = _FakeOrderRepository(orders=[_order_doc()], total=1)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get(
            "/api/driver/work", headers=_driver_headers(has_pii_access=True)
        )

        (order,) = resp.json()["data"]
        assert order["customer_phone"] == "+15550001111"

    def test_non_driver_role_is_rejected(self):
        """Validates: Requirements 3.12"""
        repo = _FakeOrderRepository(orders=[], total=0)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get(
            "/api/driver/work",
            headers=auth_headers(TENANT, roles=["dispatcher"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"

    def test_missing_driver_identity_is_rejected(self):
        """A driver-role session with no ``driver_id`` claim cannot read work."""
        repo = _FakeOrderRepository(orders=[], total=0)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get(
            "/api/driver/work", headers=auth_headers(TENANT, roles=["driver"])
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "DRIVER_IDENTITY_MISSING"


# ---------------------------------------------------------------------------
# GET /api/driver/work/{order_id}
# ---------------------------------------------------------------------------


class TestGetWork:
    """The single-order detail read."""

    def test_returns_the_order_with_degradation_flags(self):
        """Validates: Requirements 3.6, 3.11"""
        repo = _FakeOrderRepository(order=_order_doc())
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get(
            f"/api/driver/work/{ORDER}", headers=_driver_headers()
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["order_id"] == ORDER
        assert data["manifest_available"] is False
        assert data["compartment_manifest"] == []
        assert data["route_available"] is False
        # Never off the server on a /api/driver/* response (R5.26).
        assert "pod_otp" not in data

    def test_another_drivers_order_is_404(self):
        """Validates: Requirements 3.6"""
        repo = _FakeOrderRepository(
            order=_order_doc(assigned_driver_id=OTHER_DRIVER)
        )
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get(
            f"/api/driver/work/{ORDER}", headers=_driver_headers()
        )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"

    def test_absent_order_is_the_same_404(self):
        """Absent and not-yours are indistinguishable.

        Validates: Requirements 3.6
        """
        repo = _FakeOrderRepository(order=None)
        client = TestClient(_make_app(order_repository=repo))

        resp = client.get(
            f"/api/driver/work/{ORDER}", headers=_driver_headers()
        )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /api/driver/me
# ---------------------------------------------------------------------------


class TestGetIdentity:
    """Identity and server-authoritative duty status."""

    def test_returns_identity_and_duty_status(self):
        """Validates: Requirements 1.11, 13.10"""
        client = TestClient(
            _make_app(es_service=_FakeES(sources=[_driver_record()]))
        )

        resp = client.get("/api/driver/me", headers=_driver_headers())

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["driver_id"] == DRIVER
        assert data["tenant_id"] == TENANT
        assert data["driver_name"] == "Ada Driver"
        assert data["assigned_truck_id"] == "truck-1"
        assert data["duty_status"] == "on_duty"

    def test_absent_driver_record_is_404(self):
        """Validates: Requirements 1.11"""
        client = TestClient(_make_app(es_service=_FakeES(sources=[])))

        resp = client.get("/api/driver/me", headers=_driver_headers())

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    """The ``configure_work_endpoints`` accessor fails closed when unwired."""

    def test_unconfigured_router_returns_a_structured_error(self):
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(work_router)
        configure_work_endpoints(
            es_service=None, order_repository=None, job_service=None,
            redis_client=None,
        )
        install_test_auth(app)

        resp = TestClient(app, raise_server_exceptions=False).get(
            "/api/driver/work", headers=_driver_headers()
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL_ERROR"

    def test_paths_carry_no_driver_id_parameter(self):
        """R3.12 as a surface property, not a per-handler one.

        Validates: Requirements 3.12
        """
        for route in work_router.routes:
            assert "driver_id" not in route.path
            params = {p.name for p in getattr(route, "dependant").query_params}
            assert "driver_id" not in params
