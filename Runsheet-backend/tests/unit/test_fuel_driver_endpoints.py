"""
Unit tests for fuel driver REST endpoints (GET/POST/PATCH /api/ops/drivers).

Covers:
- Two-tenant isolation (cross-tenant reads → 404)
- Admin-gated writes (non-admin → 403)
- Qualification warnings for near-expiry medical cards
- Counter-increment atomicity under concurrent order transitions

Validates: Requirements 3.1.4, 3.1.5, 3.2.1, 3.2.3, 10.2.1.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support.auth_seam import auth_headers, install_test_auth

# ---------------------------------------------------------------------------
# Patch ElasticsearchService singleton BEFORE any fuel imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from fuel.api.driver_endpoints import (
    router as driver_router,
    configure_driver_endpoints,
    _compute_qualification_warnings,
    _compute_on_duty_minutes_today,
)
from fuel.order_models import Driver

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = "tenant-alpha"
TENANT_B = "tenant-beta"

# Endpoints authenticate via the Test_Auth_Path dependency-override seam
# (installed per-app in ``_make_app``); no legacy-JWT settings patch needed.
_SETTINGS_PATCH = nullcontext()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_headers(
    tenant_id: str = TENANT_A,
    roles: Optional[List[str]] = None,
) -> dict:
    return auth_headers(tenant_id, sub="user-1", roles=roles or ["admin"])


def _make_driver(
    driver_id: str = "drv_001",
    tenant_id: str = TENANT_A,
    status: str = "active",
    medical_card_expiry: Optional[str] = None,
    active_order_count: int = 0,
    completed_today: int = 0,
    last_seen: Optional[str] = None,
) -> Driver:
    """Create a Driver model for testing."""
    now = datetime.now(timezone.utc)
    return Driver(
        driver_id=driver_id,
        tenant_id=tenant_id,
        driver_name="Test Driver",
        phone="+15551234567",
        status=status,
        availability="available",
        assigned_truck_id="truck_001",
        cdl_class="A",
        hazmat_endorsement=True,
        medical_card_expiry=medical_card_expiry,
        current_location={"lat": 32.7767, "lon": -96.7970},
        last_seen=last_seen or now.isoformat(),
        active_order_count=active_order_count,
        completed_today=completed_today,
        last_event_timestamp=now,
        source_schema_version="1.0",
        trace_id=f"drv_{driver_id}",
        created_at=now,
        updated_at=now,
    )


class FakeDriverRepository:
    """In-memory fake DriverRepository for testing."""

    def __init__(self) -> None:
        self._drivers: Dict[str, Driver] = {}
        self._increment_calls: List[Dict[str, Any]] = []

    def add_driver(self, driver: Driver) -> None:
        """Seed a driver into the fake store."""
        self._drivers[f"{driver.tenant_id}:{driver.driver_id}"] = driver

    async def get(self, tenant_id: str, driver_id: str) -> Optional[Driver]:
        key = f"{tenant_id}:{driver_id}"
        return self._drivers.get(key)

    async def create(self, tenant_id: str, driver_data: Any) -> Driver:
        if isinstance(driver_data, dict):
            driver_data.setdefault("tenant_id", tenant_id)
            driver = Driver(**driver_data)
        else:
            driver = driver_data
        self._drivers[f"{tenant_id}:{driver.driver_id}"] = driver
        return driver

    async def update(
        self, tenant_id: str, driver_id: str, updates: Dict[str, Any]
    ) -> Optional[Driver]:
        key = f"{tenant_id}:{driver_id}"
        existing = self._drivers.get(key)
        if existing is None:
            return None
        existing_dict = existing.model_dump(mode="python")
        existing_dict.update(updates)
        existing_dict["updated_at"] = datetime.now(timezone.utc)
        driver = Driver(**existing_dict)
        self._drivers[key] = driver
        return driver

    async def list_for_tenant(self, tenant_id: str, **kwargs) -> List[Driver]:
        return [
            d for d in self._drivers.values()
            if d.tenant_id == tenant_id
        ]

    async def search(
        self, tenant_id: str, **kwargs
    ) -> Dict[str, Any]:
        drivers = [
            d for d in self._drivers.values()
            if d.tenant_id == tenant_id
        ]
        status = kwargs.get("status")
        if status:
            drivers = [d for d in drivers if d.status == status]
        return {
            "drivers": drivers,
            "total": len(drivers),
            "page": kwargs.get("page", 1),
            "size": kwargs.get("size", 50),
        }

    async def increment_counters(
        self,
        tenant_id: str,
        driver_id: str,
        delta_active: int = 0,
        delta_completed: int = 0,
    ) -> bool:
        self._increment_calls.append({
            "tenant_id": tenant_id,
            "driver_id": driver_id,
            "delta_active": delta_active,
            "delta_completed": delta_completed,
        })
        key = f"{tenant_id}:{driver_id}"
        driver = self._drivers.get(key)
        if driver is None:
            return False
        d = driver.model_dump(mode="python")
        d["active_order_count"] = max(
            0, d["active_order_count"] + delta_active
        )
        d["completed_today"] = d["completed_today"] + delta_completed
        d["updated_at"] = datetime.now(timezone.utc)
        self._drivers[key] = Driver(**d)
        return True


def _make_app(driver_repo: FakeDriverRepository) -> FastAPI:
    """Create a test FastAPI app with the fuel driver router."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(driver_router)
    configure_driver_endpoints(driver_repository=driver_repo)
    install_test_auth(app)
    return app


# ---------------------------------------------------------------------------
# Test: Two-tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Cross-tenant reads return 404, never leaking existence."""

    def test_get_driver_cross_tenant_returns_404(self):
        """Tenant B cannot read Tenant A's driver. Validates: Req 3.1.2"""
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(driver_id="drv_001", tenant_id=TENANT_A))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            # Tenant B tries to read Tenant A's driver
            resp = client.get(
                "/api/ops/drivers/drv_001",
                headers=_auth_headers(tenant_id=TENANT_B),
            )
        assert resp.status_code == 404

    def test_list_drivers_only_returns_own_tenant(self):
        """List only returns drivers for the caller's tenant. Validates: Req 3.1.2"""
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(driver_id="drv_A1", tenant_id=TENANT_A))
        repo.add_driver(_make_driver(driver_id="drv_B1", tenant_id=TENANT_B))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/ops/drivers",
                headers=_auth_headers(tenant_id=TENANT_A),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["driver_id"] == "drv_A1"

    def test_utilization_only_returns_own_tenant(self):
        """Utilization only returns drivers for the caller's tenant. Validates: Req 3.2.3"""
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(driver_id="drv_A1", tenant_id=TENANT_A))
        repo.add_driver(_make_driver(driver_id="drv_B1", tenant_id=TENANT_B))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/ops/drivers/utilization",
                headers=_auth_headers(tenant_id=TENANT_A),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["driver_id"] == "drv_A1"


# ---------------------------------------------------------------------------
# Test: Admin-gated writes
# ---------------------------------------------------------------------------


class TestAdminGatedWrites:
    """POST and PATCH require admin role."""

    def test_create_driver_requires_admin(self):
        """Non-admin cannot create a driver. Validates: Req 3.1.3"""
        repo = FakeDriverRepository()
        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/ops/drivers",
                json={
                    "driver_id": "drv_new",
                    "driver_name": "New Driver",
                    "status": "active",
                },
                headers=_auth_headers(roles=["dispatcher"]),
            )
        assert resp.status_code == 403

    def test_update_driver_requires_admin(self):
        """Non-admin cannot update a driver. Validates: Req 3.1.3"""
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(driver_id="drv_001", tenant_id=TENANT_A))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.patch(
                "/api/ops/drivers/drv_001",
                json={"driver_name": "Updated Name"},
                headers=_auth_headers(roles=["driver"]),
            )
        assert resp.status_code == 403

    def test_create_driver_admin_succeeds(self):
        """Admin can create a driver. Validates: Req 3.1.3"""
        repo = FakeDriverRepository()
        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/ops/drivers",
                json={
                    "driver_id": "drv_new",
                    "driver_name": "New Driver",
                    "status": "active",
                },
                headers=_auth_headers(roles=["admin"]),
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["driver_id"] == "drv_new"
        assert data["tenant_id"] == TENANT_A

    def test_update_driver_admin_succeeds(self):
        """Admin can update a driver. Validates: Req 3.1.3"""
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(driver_id="drv_001", tenant_id=TENANT_A))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.patch(
                "/api/ops/drivers/drv_001",
                json={"driver_name": "Updated Name"},
                headers=_auth_headers(roles=["admin"]),
            )
        assert resp.status_code == 200
        assert resp.json()["driver_name"] == "Updated Name"

    def test_read_endpoints_allow_any_role(self):
        """Any authenticated role can read drivers. Validates: Req 3.1.2"""
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(driver_id="drv_001", tenant_id=TENANT_A))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            # Driver role can read
            resp = client.get(
                "/api/ops/drivers/drv_001",
                headers=_auth_headers(roles=["driver"]),
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: Qualification warnings
# ---------------------------------------------------------------------------


class TestQualificationWarnings:
    """Medical card expiry warnings. Validates: Req 3.1.4"""

    def test_expired_medical_card_warning(self):
        """Expired medical card produces 'medical_card_expired'. Validates: Req 3.1.4"""
        past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        driver = _make_driver(medical_card_expiry=past)
        warnings = _compute_qualification_warnings(driver)
        assert "medical_card_expired" in warnings

    def test_near_expiry_medical_card_warning(self):
        """Medical card expiring within 30 days produces warning. Validates: Req 3.1.4"""
        near_future = (datetime.now(timezone.utc) + timedelta(days=15)).isoformat()
        driver = _make_driver(medical_card_expiry=near_future)
        warnings = _compute_qualification_warnings(driver)
        assert "medical_card_expiring_soon" in warnings

    def test_valid_medical_card_no_warning(self):
        """Medical card expiring in 60 days produces no warning. Validates: Req 3.1.4"""
        far_future = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
        driver = _make_driver(medical_card_expiry=far_future)
        warnings = _compute_qualification_warnings(driver)
        assert warnings == []

    def test_no_medical_card_no_warning(self):
        """No medical card expiry produces no warning. Validates: Req 3.1.4"""
        driver = _make_driver(medical_card_expiry=None)
        warnings = _compute_qualification_warnings(driver)
        assert warnings == []

    def test_warning_appears_in_get_response(self):
        """Qualification warnings appear in GET response. Validates: Req 3.1.4"""
        past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(
            driver_id="drv_expired",
            tenant_id=TENANT_A,
            medical_card_expiry=past,
        ))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/ops/drivers/drv_expired",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "medical_card_expired" in data["qualification_warnings"]

    def test_warning_appears_in_utilization_response(self):
        """Qualification warnings appear in utilization response. Validates: Req 3.2.3"""
        near_future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(
            driver_id="drv_expiring",
            tenant_id=TENANT_A,
            medical_card_expiry=near_future,
        ))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/ops/drivers/utilization",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert "medical_card_expiring_soon" in items[0]["qualification_warnings"]


# ---------------------------------------------------------------------------
# Test: Counter-increment atomicity under concurrent transitions
# ---------------------------------------------------------------------------


class TestCounterAtomicity:
    """Counter increments are atomic under concurrent order transitions.
    Validates: Req 3.2.1"""

    @pytest.mark.asyncio
    async def test_concurrent_increments_produce_correct_final_count(self):
        """10 simultaneous transitions produce correct final counter.
        Validates: Req 3.2.1"""
        from fuel.services.driver_counter_service import DriverCounterService

        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(
            driver_id="drv_concurrent",
            tenant_id=TENANT_A,
            active_order_count=10,
            completed_today=0,
        ))

        counter_service = DriverCounterService(driver_repo=repo)

        # Simulate 10 concurrent delivered transitions
        # Each should decrement active by 1 and increment completed by 1
        async def transition_delivered():
            await counter_service.increment_counters(
                driver_id="drv_concurrent",
                tenant_id=TENANT_A,
                delta_active=-1,
                delta_completed=1,
            )

        # Run 10 concurrent transitions
        await asyncio.gather(*[transition_delivered() for _ in range(10)])

        # Verify final state
        driver = await repo.get(TENANT_A, "drv_concurrent")
        assert driver is not None
        assert driver.active_order_count == 0
        assert driver.completed_today == 10

    @pytest.mark.asyncio
    async def test_concurrent_mixed_transitions(self):
        """Mixed concurrent transitions (assign + deliver) produce correct counts.
        Validates: Req 3.2.1"""
        from fuel.services.driver_counter_service import DriverCounterService

        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(
            driver_id="drv_mixed",
            tenant_id=TENANT_A,
            active_order_count=5,
            completed_today=0,
        ))

        counter_service = DriverCounterService(driver_repo=repo)

        # 5 new assignments (increment active) + 5 deliveries (decrement active, increment completed)
        async def assign():
            await counter_service.increment_counters(
                driver_id="drv_mixed",
                tenant_id=TENANT_A,
                delta_active=1,
            )

        async def deliver():
            await counter_service.increment_counters(
                driver_id="drv_mixed",
                tenant_id=TENANT_A,
                delta_active=-1,
                delta_completed=1,
            )

        tasks = [assign() for _ in range(5)] + [deliver() for _ in range(5)]
        await asyncio.gather(*tasks)

        driver = await repo.get(TENANT_A, "drv_mixed")
        assert driver is not None
        # Started at 5, +5 assigns, -5 delivers = 5
        assert driver.active_order_count == 5
        assert driver.completed_today == 5

    @pytest.mark.asyncio
    async def test_active_count_never_goes_negative(self):
        """active_order_count is clamped at 0 (never negative).
        Validates: Req 3.2.1"""
        from fuel.services.driver_counter_service import DriverCounterService

        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(
            driver_id="drv_clamp",
            tenant_id=TENANT_A,
            active_order_count=2,
            completed_today=0,
        ))

        counter_service = DriverCounterService(driver_repo=repo)

        # Decrement 5 times from a count of 2 — should clamp at 0
        async def decrement():
            await counter_service.increment_counters(
                driver_id="drv_clamp",
                tenant_id=TENANT_A,
                delta_active=-1,
            )

        await asyncio.gather(*[decrement() for _ in range(5)])

        driver = await repo.get(TENANT_A, "drv_clamp")
        assert driver is not None
        assert driver.active_order_count == 0


# ---------------------------------------------------------------------------
# Test: Utilization endpoint response shape
# ---------------------------------------------------------------------------


class TestUtilizationEndpoint:
    """Utilization endpoint returns the expected fields. Validates: Req 3.2.3"""

    def test_utilization_response_shape(self):
        """Utilization response includes all required fields. Validates: Req 3.2.3"""
        now = datetime.now(timezone.utc)
        repo = FakeDriverRepository()
        repo.add_driver(_make_driver(
            driver_id="drv_util",
            tenant_id=TENANT_A,
            active_order_count=3,
            completed_today=7,
            last_seen=now.isoformat(),
        ))

        app = _make_app(repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/ops/drivers/utilization",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["driver_id"] == "drv_util"
        assert item["active_order_count"] == 3
        assert item["completed_today"] == 7
        assert item["last_seen"] is not None
        assert item["current_location"] is not None
        assert "on_duty_minutes_today" in item
        assert "qualification_warnings" in item

    def test_on_duty_minutes_zero_for_off_duty(self):
        """Off-duty drivers have 0 on_duty_minutes_today. Validates: Req 3.2.3"""
        now = datetime.now(timezone.utc)
        driver = _make_driver(
            status="off_duty",
            last_seen=now.isoformat(),
        )
        minutes = _compute_on_duty_minutes_today(driver)
        assert minutes == 0


# ---------------------------------------------------------------------------
# Test: Driver availability check in assign endpoint
# ---------------------------------------------------------------------------


class TestDriverAvailabilityInAssign:
    """Assign endpoint rejects off_duty/inactive drivers. Validates: Req 3.1.5"""

    def test_assign_rejects_off_duty_driver(self):
        """Assigning an off_duty driver returns 409. Validates: Req 3.1.5"""
        from fuel.api.order_endpoints import (
            router as order_router,
            configure_order_endpoints,
        )
        from fuel.order_models import FuelOrder

        # Create a fake order repo
        order_repo = MagicMock()
        order_repo._es = MagicMock()
        order_repo._es.update_document = AsyncMock()
        order_repo._orders_index = "fuel_orders_current"
        order_repo.append_event = AsyncMock()

        # Create a placed order
        now = datetime.now(timezone.utc)
        mock_order = MagicMock()
        mock_order.status = "placed"
        mock_order.assigned_driver_id = None
        mock_order.model_dump = MagicMock(return_value={
            "order_id": "ord_test",
            "status": "placed",
        })
        order_repo.get = AsyncMock(return_value=mock_order)

        # Create driver repo with off_duty driver
        driver_repo = FakeDriverRepository()
        driver_repo.add_driver(_make_driver(
            driver_id="drv_off",
            tenant_id=TENANT_A,
            status="off_duty",
        ))

        # Build app
        from errors.handlers import register_exception_handlers
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(order_router)
        configure_order_endpoints(
            order_intake_pipeline=MagicMock(),
            order_repository=order_repo,
            driver_repository=driver_repo,
        )
        install_test_auth(app)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.patch(
                "/api/orders/ord_test/assign",
                json={"driver_id": "drv_off"},
                headers=_auth_headers(roles=["dispatcher"]),
            )
        assert resp.status_code == 409

    def test_assign_rejects_inactive_driver(self):
        """Assigning an inactive driver returns 409. Validates: Req 3.1.5"""
        from fuel.api.order_endpoints import (
            router as order_router,
            configure_order_endpoints,
        )

        order_repo = MagicMock()
        order_repo._es = MagicMock()
        order_repo._es.update_document = AsyncMock()
        order_repo._orders_index = "fuel_orders_current"
        order_repo.append_event = AsyncMock()

        mock_order = MagicMock()
        mock_order.status = "placed"
        mock_order.assigned_driver_id = None
        order_repo.get = AsyncMock(return_value=mock_order)

        driver_repo = FakeDriverRepository()
        driver_repo.add_driver(_make_driver(
            driver_id="drv_inactive",
            tenant_id=TENANT_A,
            status="inactive",
        ))

        from errors.handlers import register_exception_handlers
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(order_router)
        configure_order_endpoints(
            order_intake_pipeline=MagicMock(),
            order_repository=order_repo,
            driver_repository=driver_repo,
        )
        install_test_auth(app)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.patch(
                "/api/orders/ord_test/assign",
                json={"driver_id": "drv_inactive"},
                headers=_auth_headers(roles=["admin"]),
            )
        assert resp.status_code == 409
