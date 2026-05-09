"""
Unit tests for :mod:`fuel.api.driver_endpoints` (fuel ops driver surface).

Task 8.5 of the order-intake-pipeline spec. Exercises the driver REST
surface with mocked repositories so the suite stays decoupled from
Elasticsearch.

Covers:
* Two-tenant isolation: cross-tenant reads → 404
* Admin-gated writes: non-admin POST/PATCH → 403
* Qualification warnings: medical_card_expiry within 30 days →
  "medical_card_expiring_soon", expired → "medical_card_expired"
* Counter-increment atomicity: asyncio.gather on 10 simultaneous
  transitions and assert final counter matches

Validates: Requirements 3.1.4, 3.1.5, 3.2.1, 3.2.3, 10.2.1
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from fuel.api.driver_endpoints import configure_driver_endpoints, router
from fuel.order_models import Driver
from fuel.services.driver_counter_service import DriverCounterService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fake repository
# ---------------------------------------------------------------------------


class FakeDriverRepository:
    """In-memory fake of DriverRepository for endpoint tests."""

    def __init__(self):
        self._drivers: Dict[str, Driver] = {}
        self._counter_calls: List[Dict[str, Any]] = []

    def _key(self, tenant_id: str, driver_id: str) -> str:
        return f"{tenant_id}::{driver_id}"

    def seed_driver(self, driver: Driver) -> None:
        self._drivers[self._key(driver.tenant_id, driver.driver_id)] = driver

    async def get(self, tenant_id: str, driver_id: str) -> Optional[Driver]:
        key = self._key(tenant_id, driver_id)
        return self._drivers.get(key)

    async def create(self, tenant_id: str, driver_data: Dict[str, Any]) -> Driver:
        driver_data.setdefault("tenant_id", tenant_id)
        model = Driver(**driver_data)
        self._drivers[self._key(tenant_id, model.driver_id)] = model
        return model

    async def update(
        self, tenant_id: str, driver_id: str, updates: Dict[str, Any]
    ) -> Optional[Driver]:
        key = self._key(tenant_id, driver_id)
        existing = self._drivers.get(key)
        if existing is None:
            return None
        existing_dict = existing.model_dump(mode="python")
        existing_dict.update(updates)
        from services.time_utils import utcnow
        existing_dict["updated_at"] = utcnow()
        model = Driver(**existing_dict)
        self._drivers[key] = model
        return model

    async def list_for_tenant(self, tenant_id: str, *, size: int = 500) -> List[Driver]:
        return [d for d in self._drivers.values() if d.tenant_id == tenant_id]

    async def search(
        self,
        tenant_id: str,
        *,
        status: Optional[str] = None,
        availability: Optional[str] = None,
        page: int = 1,
        size: int = 50,
        **kwargs,
    ) -> Dict[str, Any]:
        drivers = [d for d in self._drivers.values() if d.tenant_id == tenant_id]
        if status:
            drivers = [d for d in drivers if d.status == status]
        return {"drivers": drivers, "total": len(drivers), "page": page, "size": size}

    async def increment_counters(
        self,
        tenant_id: str,
        driver_id: str,
        delta_active: int = 0,
        delta_completed: int = 0,
    ) -> bool:
        """Atomically adjust counters (simulated for tests)."""
        key = self._key(tenant_id, driver_id)
        driver = self._drivers.get(key)
        if driver is None:
            return False
        self._counter_calls.append({
            "driver_id": driver_id,
            "tenant_id": tenant_id,
            "delta_active": delta_active,
            "delta_completed": delta_completed,
        })
        # Simulate atomic update
        d = driver.model_dump(mode="python")
        d["active_order_count"] = max(
            0, d.get("active_order_count", 0) + delta_active
        )
        d["completed_today"] = d.get("completed_today", 0) + delta_completed
        from services.time_utils import utcnow
        d["updated_at"] = utcnow()
        d["last_event_timestamp"] = utcnow()
        self._drivers[key] = Driver(**d)
        return True


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


def _make_driver(
    driver_id: str = "drv-001",
    tenant_id: str = "tenant-A",
    status: str = "active",
    medical_card_expiry: Optional[datetime] = None,
    active_order_count: int = 0,
    completed_today: int = 0,
) -> Driver:
    """Create a valid Driver for testing."""
    return Driver(
        driver_id=driver_id,
        tenant_id=tenant_id,
        driver_name="Test Driver",
        phone="555-0100",
        status=status,
        availability="available",
        assigned_truck_id="truck-1",
        cdl_class="A",
        hazmat_endorsement=True,
        medical_card_expiry=medical_card_expiry,
        current_location={"lat": 30.0, "lon": -90.0},
        last_seen=_NOW,
        active_order_count=active_order_count,
        completed_today=completed_today,
        last_event_timestamp=_NOW,
        source_schema_version="1.0",
        trace_id="trace-drv-001",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _build_app(
    *,
    tenant_id: str = "tenant-A",
    roles: Optional[List[str]] = None,
    repo: Optional[FakeDriverRepository] = None,
) -> Tuple[FastAPI, TestClient, FakeDriverRepository]:
    """Build a FastAPI app with the driver router wired in."""
    repo = repo or FakeDriverRepository()

    configure_driver_endpoints(driver_repository=repo)

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
    return app, client, repo


def _assert_error_envelope(resp_json: dict) -> None:
    """Assert the response matches the ErrorResponse shape."""
    assert "detail" in resp_json
    detail = resp_json["detail"]
    assert "error_code" in detail
    assert "message" in detail
    assert isinstance(detail["error_code"], str)
    assert isinstance(detail["message"], str)


# ---------------------------------------------------------------------------
# Tests — Two-tenant isolation (Req 3.1.4, 10.2.1)
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Cross-tenant reads → 404."""

    def test_cross_tenant_get_driver_returns_404(self):
        """Tenant A cannot read Tenant B's driver — gets 404."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(driver_id="drv-b1", tenant_id="tenant-B"))

        _, client, _ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.get("/api/ops/drivers/drv-b1")
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "RESOURCE_NOT_FOUND"

    def test_cross_tenant_list_only_shows_own_drivers(self):
        """List only returns drivers belonging to the caller's tenant."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(driver_id="drv-a1", tenant_id="tenant-A"))
        repo.seed_driver(_make_driver(driver_id="drv-b1", tenant_id="tenant-B"))

        _, client, _ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.get("/api/ops/drivers")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["driver_id"] == "drv-a1"

    def test_cross_tenant_utilization_only_shows_own_drivers(self):
        """Utilization only returns drivers belonging to the caller's tenant."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(driver_id="drv-a1", tenant_id="tenant-A"))
        repo.seed_driver(_make_driver(driver_id="drv-b1", tenant_id="tenant-B"))

        _, client, _ = _build_app(tenant_id="tenant-A", repo=repo)

        resp = client.get("/api/ops/drivers/utilization")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["driver_id"] == "drv-a1"

    def test_cross_tenant_patch_returns_404(self):
        """Tenant A cannot update Tenant B's driver — gets 404."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(driver_id="drv-b1", tenant_id="tenant-B"))

        _, client, _ = _build_app(tenant_id="tenant-A", roles=["admin"], repo=repo)

        resp = client.patch(
            "/api/ops/drivers/drv-b1",
            json={"driver_name": "Hacked Name"},
        )
        assert resp.status_code == 404
        _assert_error_envelope(resp.json())


# ---------------------------------------------------------------------------
# Tests — Admin-gated writes (Req 3.1.5, 10.2.1)
# ---------------------------------------------------------------------------


class TestAdminGatedWrites:
    """Non-admin POST/PATCH → 403."""

    def test_dispatcher_cannot_create_driver(self):
        """A dispatcher role cannot POST /api/ops/drivers — gets 403."""
        _, client, _ = _build_app(roles=["dispatcher"])

        resp = client.post(
            "/api/ops/drivers",
            json={
                "driver_id": "drv-new",
                "driver_name": "New Driver",
                "status": "active",
            },
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_driver_role_cannot_create_driver(self):
        """A driver role cannot POST /api/ops/drivers — gets 403."""
        _, client, _ = _build_app(roles=["driver"])

        resp = client.post(
            "/api/ops/drivers",
            json={
                "driver_id": "drv-new",
                "driver_name": "New Driver",
                "status": "active",
            },
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_dispatcher_cannot_update_driver(self):
        """A dispatcher role cannot PATCH /api/ops/drivers/{id} — gets 403."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver())

        _, client, _ = _build_app(roles=["dispatcher"], repo=repo)

        resp = client.patch(
            "/api/ops/drivers/drv-001",
            json={"driver_name": "Updated Name"},
        )
        assert resp.status_code == 403
        _assert_error_envelope(resp.json())
        assert resp.json()["detail"]["error_code"] == "INSUFFICIENT_ROLE"

    def test_admin_can_create_driver(self):
        """An admin role CAN POST /api/ops/drivers — gets 201."""
        _, client, _ = _build_app(roles=["admin"])

        resp = client.post(
            "/api/ops/drivers",
            json={
                "driver_id": "drv-new",
                "driver_name": "New Driver",
                "status": "active",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["driver_id"] == "drv-new"

    def test_admin_can_update_driver(self):
        """An admin role CAN PATCH /api/ops/drivers/{id} — gets 200."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver())

        _, client, _ = _build_app(roles=["admin"], repo=repo)

        resp = client.patch(
            "/api/ops/drivers/drv-001",
            json={"driver_name": "Updated Name"},
        )
        assert resp.status_code == 200
        assert resp.json()["driver_name"] == "Updated Name"

    def test_any_role_can_read_drivers(self):
        """Any authenticated role CAN read drivers (GET endpoints)."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver())

        _, client, _ = _build_app(roles=["driver"], repo=repo)

        resp = client.get("/api/ops/drivers")
        assert resp.status_code == 200

        resp = client.get("/api/ops/drivers/drv-001")
        assert resp.status_code == 200

        resp = client.get("/api/ops/drivers/utilization")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests — Qualification warnings (Req 3.1.4, 3.2.3)
# ---------------------------------------------------------------------------


class TestQualificationWarnings:
    """medical_card_expiry within 30 days → "medical_card_expiring_soon",
    expired → "medical_card_expired"."""

    @patch("fuel.api.driver_endpoints.utcnow")
    def test_expired_medical_card_warning(self, mock_utcnow):
        """Expired medical card → 'medical_card_expired' in warnings."""
        mock_utcnow.return_value = _NOW

        repo = FakeDriverRepository()
        # Card expired 5 days ago
        expired_date = _NOW - timedelta(days=5)
        repo.seed_driver(_make_driver(medical_card_expiry=expired_date))

        _, client, _ = _build_app(repo=repo)

        resp = client.get("/api/ops/drivers/drv-001")
        assert resp.status_code == 200
        body = resp.json()
        assert "medical_card_expired" in body["qualification_warnings"]

    @patch("fuel.api.driver_endpoints.utcnow")
    def test_near_expiry_medical_card_warning(self, mock_utcnow):
        """Medical card expiring within 30 days → 'medical_card_expiring_soon'."""
        mock_utcnow.return_value = _NOW

        repo = FakeDriverRepository()
        # Card expires in 15 days
        near_expiry_date = _NOW + timedelta(days=15)
        repo.seed_driver(_make_driver(medical_card_expiry=near_expiry_date))

        _, client, _ = _build_app(repo=repo)

        resp = client.get("/api/ops/drivers/drv-001")
        assert resp.status_code == 200
        body = resp.json()
        assert "medical_card_expiring_soon" in body["qualification_warnings"]

    @patch("fuel.api.driver_endpoints.utcnow")
    def test_valid_medical_card_no_warning(self, mock_utcnow):
        """Medical card valid for > 30 days → no warnings."""
        mock_utcnow.return_value = _NOW

        repo = FakeDriverRepository()
        # Card expires in 60 days — well beyond the 30-day threshold
        valid_date = _NOW + timedelta(days=60)
        repo.seed_driver(_make_driver(medical_card_expiry=valid_date))

        _, client, _ = _build_app(repo=repo)

        resp = client.get("/api/ops/drivers/drv-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["qualification_warnings"] == []

    @patch("fuel.api.driver_endpoints.utcnow")
    def test_no_medical_card_no_warning(self, mock_utcnow):
        """No medical_card_expiry → no warnings."""
        mock_utcnow.return_value = _NOW

        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(medical_card_expiry=None))

        _, client, _ = _build_app(repo=repo)

        resp = client.get("/api/ops/drivers/drv-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["qualification_warnings"] == []

    @patch("fuel.api.driver_endpoints.utcnow")
    def test_utilization_includes_qualification_warnings(self, mock_utcnow):
        """Utilization endpoint also includes qualification_warnings."""
        mock_utcnow.return_value = _NOW

        repo = FakeDriverRepository()
        # Card expired
        expired_date = _NOW - timedelta(days=10)
        repo.seed_driver(_make_driver(medical_card_expiry=expired_date))

        _, client, _ = _build_app(repo=repo)

        resp = client.get("/api/ops/drivers/utilization")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert "medical_card_expired" in body["items"][0]["qualification_warnings"]

    @patch("fuel.api.driver_endpoints.utcnow")
    def test_expiry_exactly_at_30_days_triggers_warning(self, mock_utcnow):
        """Medical card expiring in exactly 30 days → 'medical_card_expiring_soon'."""
        mock_utcnow.return_value = _NOW

        repo = FakeDriverRepository()
        # Card expires in exactly 30 days
        expiry_date = _NOW + timedelta(days=30)
        repo.seed_driver(_make_driver(medical_card_expiry=expiry_date))

        _, client, _ = _build_app(repo=repo)

        resp = client.get("/api/ops/drivers/drv-001")
        assert resp.status_code == 200
        body = resp.json()
        assert "medical_card_expiring_soon" in body["qualification_warnings"]


# ---------------------------------------------------------------------------
# Tests — Counter-increment atomicity (Req 3.2.1, 10.2.1)
# ---------------------------------------------------------------------------


class TestCounterIncrementAtomicity:
    """Use asyncio.gather on 10 simultaneous transitions and assert
    final counter matches."""

    @pytest.mark.asyncio
    async def test_concurrent_counter_increments_are_atomic(self):
        """10 simultaneous increment_counters calls produce correct final count."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(
            driver_id="drv-atomic",
            active_order_count=0,
            completed_today=0,
        ))

        counter_service = DriverCounterService(driver_repo=repo)

        # Simulate 10 concurrent order assignments (each increments active by 1)
        tasks = [
            counter_service.increment_counters(
                driver_id="drv-atomic",
                tenant_id="tenant-A",
                delta_active=1,
                delta_completed=0,
            )
            for _ in range(10)
        ]

        results = await asyncio.gather(*tasks)

        # All 10 should succeed
        assert all(r is True for r in results)

        # Final active_order_count should be exactly 10
        driver = await repo.get("tenant-A", "drv-atomic")
        assert driver is not None
        assert driver.active_order_count == 10

    @pytest.mark.asyncio
    async def test_concurrent_mixed_increments_and_decrements(self):
        """Mixed concurrent increments and decrements produce correct final count."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(
            driver_id="drv-mixed",
            active_order_count=5,
            completed_today=0,
        ))

        counter_service = DriverCounterService(driver_repo=repo)

        # 5 increments (+1 each) and 5 decrements (-1 each) = net 0
        increment_tasks = [
            counter_service.increment_counters(
                driver_id="drv-mixed",
                tenant_id="tenant-A",
                delta_active=1,
                delta_completed=0,
            )
            for _ in range(5)
        ]
        decrement_tasks = [
            counter_service.increment_counters(
                driver_id="drv-mixed",
                tenant_id="tenant-A",
                delta_active=-1,
                delta_completed=0,
            )
            for _ in range(5)
        ]

        results = await asyncio.gather(*(increment_tasks + decrement_tasks))
        assert all(r is True for r in results)

        # Final active_order_count should be 5 (started at 5, net change 0)
        driver = await repo.get("tenant-A", "drv-mixed")
        assert driver is not None
        assert driver.active_order_count == 5

    @pytest.mark.asyncio
    async def test_concurrent_completed_today_increments(self):
        """10 simultaneous completed_today increments produce correct final count."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(
            driver_id="drv-complete",
            active_order_count=10,
            completed_today=0,
        ))

        counter_service = DriverCounterService(driver_repo=repo)

        # 10 concurrent deliveries: each decrements active by 1, increments completed by 1
        tasks = [
            counter_service.increment_counters(
                driver_id="drv-complete",
                tenant_id="tenant-A",
                delta_active=-1,
                delta_completed=1,
            )
            for _ in range(10)
        ]

        results = await asyncio.gather(*tasks)
        assert all(r is True for r in results)

        driver = await repo.get("tenant-A", "drv-complete")
        assert driver is not None
        assert driver.active_order_count == 0
        assert driver.completed_today == 10

    @pytest.mark.asyncio
    async def test_counter_clamps_at_zero(self):
        """active_order_count never goes below 0 even with excess decrements."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(
            driver_id="drv-clamp",
            active_order_count=3,
            completed_today=0,
        ))

        counter_service = DriverCounterService(driver_repo=repo)

        # 10 decrements on a driver with only 3 active orders
        tasks = [
            counter_service.increment_counters(
                driver_id="drv-clamp",
                tenant_id="tenant-A",
                delta_active=-1,
                delta_completed=0,
            )
            for _ in range(10)
        ]

        results = await asyncio.gather(*tasks)
        assert all(r is True for r in results)

        driver = await repo.get("tenant-A", "drv-clamp")
        assert driver is not None
        # Should be clamped at 0, not negative
        assert driver.active_order_count == 0

    @pytest.mark.asyncio
    async def test_counter_service_skips_empty_driver_id(self):
        """Empty driver_id returns False without error."""
        repo = FakeDriverRepository()
        counter_service = DriverCounterService(driver_repo=repo)

        result = await counter_service.increment_counters(
            driver_id="",
            tenant_id="tenant-A",
            delta_active=1,
            delta_completed=0,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_counter_service_skips_noop(self):
        """Zero deltas return False (no-op)."""
        repo = FakeDriverRepository()
        repo.seed_driver(_make_driver(driver_id="drv-noop"))

        counter_service = DriverCounterService(driver_repo=repo)

        result = await counter_service.increment_counters(
            driver_id="drv-noop",
            tenant_id="tenant-A",
            delta_active=0,
            delta_completed=0,
        )
        assert result is False
