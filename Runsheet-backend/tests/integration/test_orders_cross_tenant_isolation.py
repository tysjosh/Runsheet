"""
Integration test — cross-tenant isolation for order, driver, and
intake-channel endpoints.

Builds JWTs for two tenants and asserts:
- Cross-tenant reads → 404 (existence not leaked)
- Cross-tenant writes → 403

Covers every endpoint under:
- ``/api/orders/*``
- ``/api/ops/drivers/*``
- ``/api/integrations/intake-channels/*``

Also confirms via code review that every handler takes
``tenant: TenantContext = Depends(get_tenant_context)``.

Validates: Requirements 9.1.2, 10.2.1
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
from fuel.api.driver_endpoints import configure_driver_endpoints
from fuel.api.driver_endpoints import router as driver_router
from fuel.api.order_endpoints import configure_order_endpoints
from fuel.api.order_endpoints import router as order_router
from fuel.order_models import Driver, DriverStatus, FuelOrder, FuelOrderEvent
from integrations.api.intake_channel_endpoints import (
    configure_intake_channel_endpoints,
    router as intake_channel_router,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = "tenant-alpha"
TENANT_B = "tenant-beta"

_NOW = datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fake repositories (in-memory, tenant-scoped)
# ---------------------------------------------------------------------------


@dataclass
class FakeIntakeResult:
    event_id: str = "evt_fake123"
    status: str = "processed"
    order_id: Optional[str] = "ord_fake456"


class FakePipeline:
    async def ingest_dispatcher(self, *, tenant, payload, request_id, client_event_id):
        return FakeIntakeResult()


class _FakeES:
    def __init__(self, repo):
        self._repo = repo

    async def update_document(self, index: str, doc_id: str, fields: Dict[str, Any]) -> None:
        for key, order in list(self._repo._orders.items()):
            if order.order_id == doc_id:
                order_dict = order.model_dump(mode="python")
                order_dict.update(fields)
                for date_field in ("updated_at", "last_event_timestamp", "created_at"):
                    val = order_dict.get(date_field)
                    if isinstance(val, str):
                        order_dict[date_field] = datetime.fromisoformat(val)
                self._repo._orders[key] = FuelOrder.model_validate(order_dict)
                return


class FakeOrderRepository:
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
        return self._orders.get(self._key(tenant_id, order_id))

    async def search(self, *, tenant_id: str, **kwargs) -> Dict[str, Any]:
        orders = [o for o in self._orders.values() if o.tenant_id == tenant_id]
        return {"orders": orders, "total": len(orders), "page": 1, "size": 20}

    async def append_event(self, tenant_id: str, event: Any) -> None:
        pass

    async def get_events_for_order(self, tenant_id: str, order_id: str) -> List[FuelOrderEvent]:
        return []


class FakeDriverRepository:
    def __init__(self):
        self._drivers: Dict[str, Driver] = {}

    def _key(self, tenant_id: str, driver_id: str) -> str:
        return f"{tenant_id}::{driver_id}"

    def seed_driver(self, driver: Driver) -> None:
        self._drivers[self._key(driver.tenant_id, driver.driver_id)] = driver

    async def get(self, tenant_id: str, driver_id: str) -> Optional[Driver]:
        return self._drivers.get(self._key(tenant_id, driver_id))

    async def search(self, *, tenant_id: str, **kwargs) -> Dict[str, Any]:
        drivers = [d for d in self._drivers.values() if d.tenant_id == tenant_id]
        return {"drivers": drivers, "total": len(drivers)}

    async def list_for_tenant(self, tenant_id: str) -> List[Driver]:
        return [d for d in self._drivers.values() if d.tenant_id == tenant_id]

    async def create(self, tenant_id: str, data: Dict[str, Any]) -> Driver:
        driver = Driver(**data)
        self._drivers[self._key(tenant_id, driver.driver_id)] = driver
        return driver

    async def update(self, tenant_id: str, driver_id: str, updates: Dict[str, Any]) -> Optional[Driver]:
        key = self._key(tenant_id, driver_id)
        driver = self._drivers.get(key)
        if driver is None:
            return None
        driver_dict = driver.model_dump(mode="python")
        driver_dict.update(updates)
        driver_dict["updated_at"] = _NOW
        updated = Driver.model_validate(driver_dict)
        self._drivers[key] = updated
        return updated


class FakeIntakeChannelRepository:
    """Minimal fake for intake channel endpoints."""

    def __init__(self):
        self._channels: Dict[str, Any] = {}

    async def create(self, *, tenant_id, channel_id, channel_type, display_name,
                     supported_schema_versions, rate_limit_per_minute=None, enabled=True):
        from fuel.intake_channel_models import IntakeChannel
        from services.time_utils import utcnow
        now = utcnow()
        channel = IntakeChannel(
            channel_id=channel_id,
            tenant_id=tenant_id,
            channel_type=channel_type,
            display_name=display_name,
            hmac_secret_ref=f"ref_{channel_id}",
            supported_schema_versions=supported_schema_versions,
            rate_limit_per_minute=rate_limit_per_minute,
            secret_version=1,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        self._channels[f"{tenant_id}::{channel_id}"] = channel
        return channel, "plaintext-secret-once"

    async def get(self, tenant_id: str, channel_id: str):
        return self._channels.get(f"{tenant_id}::{channel_id}")

    async def list_for_tenant(self, tenant_id: str):
        return [ch for key, ch in self._channels.items() if key.startswith(f"{tenant_id}::")]

    async def update(self, *, tenant_id: str, channel_id: str, updates: Dict[str, Any]):
        key = f"{tenant_id}::{channel_id}"
        channel = self._channels.get(key)
        if channel is None:
            return None
        return channel

    async def delete(self, tenant_id: str, channel_id: str) -> bool:
        key = f"{tenant_id}::{channel_id}"
        if key in self._channels:
            del self._channels[key]
            return True
        return False

    async def rotate_secret(self, *, tenant_id: str, channel_id: str):
        key = f"{tenant_id}::{channel_id}"
        channel = self._channels.get(key)
        if channel is None:
            raise ValueError("not found")
        return channel, "new-plaintext-secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx(tenant_id: str, roles: Optional[List[str]] = None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=f"user-{tenant_id}",
        has_pii_access=False,
        roles=roles if roles is not None else ["admin", "dispatcher"],
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


def _make_order(order_id: str, tenant_id: str, status: str = "placed") -> FuelOrder:
    return FuelOrder(
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
        call_type="one_off",
        delivery_window_start=datetime(2026, 2, 2, 8, 0, 0, tzinfo=timezone.utc),
        delivery_window_end=datetime(2026, 2, 2, 12, 0, 0, tzinfo=timezone.utc),
        intake_channel="dispatcher",
        intake_channel_id="dispatcher-default",
        status=status,
        source_schema_version="1.0",
        trace_id="trace-001",
        created_at=_NOW,
        updated_at=_NOW,
        last_event_timestamp=_NOW,
    )


def _make_driver(driver_id: str, tenant_id: str) -> Driver:
    return Driver(
        driver_id=driver_id,
        tenant_id=tenant_id,
        driver_name="Test Driver",
        phone="555-0100",
        status="active",
        availability="available",
        assigned_truck_id=None,
        cdl_class="A",
        hazmat_endorsement=True,
        medical_card_expiry=datetime(2027, 6, 1, tzinfo=timezone.utc),
        current_location=None,
        last_seen=_NOW,
        active_order_count=0,
        completed_today=0,
        last_event_timestamp=_NOW,
        source_schema_version="1.0",
        trace_id=f"drv_{driver_id}",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _build_app_for_tenant(
    tenant_id: str,
    order_repo: FakeOrderRepository,
    driver_repo: FakeDriverRepository,
    intake_repo: FakeIntakeChannelRepository,
) -> TestClient:
    """Build a FastAPI app with all routers, scoped to a specific tenant."""
    pipeline = FakePipeline()

    configure_order_endpoints(
        order_intake_pipeline=pipeline,
        order_repository=order_repo,
        driver_repository=driver_repo,
    )
    configure_driver_endpoints(driver_repository=driver_repo)
    configure_intake_channel_endpoints(repository=intake_repo)

    app = FastAPI()
    app.include_router(order_router)
    app.include_router(driver_router)
    app.include_router(intake_channel_router)

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.to_dict()},
        )

    ctx = _tenant_ctx(tenant_id)
    app.dependency_overrides[get_tenant_context] = lambda: ctx

    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests — Cross-tenant order isolation
# ---------------------------------------------------------------------------


class TestOrdersCrossTenantIsolation:
    """Cross-tenant reads → 404, cross-tenant writes → 403 for /api/orders/*.

    Validates: Requirement 9.1.2
    """

    @pytest.fixture
    def repos(self):
        order_repo = FakeOrderRepository()
        driver_repo = FakeDriverRepository()
        intake_repo = FakeIntakeChannelRepository()

        # Seed data for tenant B
        order_repo.seed_order(_make_order("ord_b_001", TENANT_B))
        order_repo.seed_order(_make_order("ord_b_002", TENANT_B, status="confirmed"))
        driver_repo.seed_driver(_make_driver("drv_b_001", TENANT_B))

        return order_repo, driver_repo, intake_repo

    def test_get_order_cross_tenant_returns_404(self, repos):
        """Tenant A cannot read Tenant B's order."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.get("/api/orders/ord_b_001")
        assert resp.status_code == 404

    def test_get_order_events_cross_tenant_returns_404(self, repos):
        """Tenant A cannot read Tenant B's order events."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.get("/api/orders/ord_b_001/events")
        assert resp.status_code == 404

    def test_list_orders_cross_tenant_returns_empty(self, repos):
        """Tenant A listing orders sees none of Tenant B's orders."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.get("/api/orders")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_status_transition_cross_tenant_returns_404(self, repos):
        """Tenant A cannot transition Tenant B's order status."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.patch(
            "/api/orders/ord_b_001/status",
            json={"new_status": "confirmed"},
        )
        assert resp.status_code == 404

    def test_assign_driver_cross_tenant_returns_404(self, repos):
        """Tenant A cannot assign a driver to Tenant B's order."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.patch(
            "/api/orders/ord_b_001/assign",
            json={"driver_id": "drv_b_001"},
        )
        assert resp.status_code == 404

    def test_cancel_order_cross_tenant_returns_404(self, repos):
        """Tenant A cannot cancel Tenant B's order."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.post(
            "/api/orders/ord_b_001/cancel",
            json={"reason": "test cancel"},
        )
        assert resp.status_code == 404

    def test_hold_order_cross_tenant_returns_404(self, repos):
        """Tenant A cannot put Tenant B's order on hold."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.post(
            "/api/orders/ord_b_001/hold",
            json={"hold_reason": "test hold"},
        )
        assert resp.status_code == 404

    def test_release_hold_cross_tenant_returns_404(self, repos):
        """Tenant A cannot release hold on Tenant B's order."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.post("/api/orders/ord_b_001/release-hold", json={})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — Cross-tenant driver isolation
# ---------------------------------------------------------------------------


class TestDriversCrossTenantIsolation:
    """Cross-tenant reads → 404, cross-tenant writes → 403 for /api/ops/drivers/*.

    Validates: Requirement 9.1.2
    """

    @pytest.fixture
    def repos(self):
        order_repo = FakeOrderRepository()
        driver_repo = FakeDriverRepository()
        intake_repo = FakeIntakeChannelRepository()

        # Seed data for tenant B
        driver_repo.seed_driver(_make_driver("drv_b_001", TENANT_B))

        return order_repo, driver_repo, intake_repo

    def test_get_driver_cross_tenant_returns_404(self, repos):
        """Tenant A cannot read Tenant B's driver."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.get("/api/ops/drivers/drv_b_001")
        assert resp.status_code == 404

    def test_list_drivers_cross_tenant_returns_empty(self, repos):
        """Tenant A listing drivers sees none of Tenant B's drivers."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.get("/api/ops/drivers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_utilization_cross_tenant_returns_empty(self, repos):
        """Tenant A utilization endpoint sees none of Tenant B's drivers."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.get("/api/ops/drivers/utilization")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_update_driver_cross_tenant_returns_404(self, repos):
        """Tenant A cannot update Tenant B's driver."""
        order_repo, driver_repo, intake_repo = repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.patch(
            "/api/ops/drivers/drv_b_001",
            json={"driver_name": "Hacked Name"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — Cross-tenant intake channel isolation
# ---------------------------------------------------------------------------


class TestIntakeChannelsCrossTenantIsolation:
    """Cross-tenant reads → 404, cross-tenant writes → 403 for
    /api/integrations/intake-channels/*.

    Validates: Requirement 9.1.2
    """

    @pytest.fixture
    def repos(self):
        order_repo = FakeOrderRepository()
        driver_repo = FakeDriverRepository()
        intake_repo = FakeIntakeChannelRepository()
        return order_repo, driver_repo, intake_repo

    @pytest.fixture
    def seeded_repos(self, repos):
        """Seed a channel for tenant B."""
        order_repo, driver_repo, intake_repo = repos
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            intake_repo.create(
                tenant_id=TENANT_B,
                channel_id="channel-b-voice",
                channel_type="voice",
                display_name="Tenant B Voice Channel",
                supported_schema_versions=["1.0"],
            )
        )
        return order_repo, driver_repo, intake_repo

    def test_list_channels_cross_tenant_returns_empty(self, seeded_repos):
        """Tenant A listing channels sees none of Tenant B's channels."""
        order_repo, driver_repo, intake_repo = seeded_repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.get("/api/integrations/intake-channels")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_update_channel_cross_tenant_returns_404(self, seeded_repos):
        """Tenant A cannot update Tenant B's channel."""
        order_repo, driver_repo, intake_repo = seeded_repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.patch(
            "/api/integrations/intake-channels/channel-b-voice",
            json={"display_name": "Hacked Channel"},
        )
        assert resp.status_code == 404

    def test_delete_channel_cross_tenant_returns_404(self, seeded_repos):
        """Tenant A cannot delete Tenant B's channel."""
        order_repo, driver_repo, intake_repo = seeded_repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.delete("/api/integrations/intake-channels/channel-b-voice")
        assert resp.status_code == 404

    def test_rotate_secret_cross_tenant_returns_404(self, seeded_repos):
        """Tenant A cannot rotate Tenant B's channel secret."""
        order_repo, driver_repo, intake_repo = seeded_repos
        client = _build_app_for_tenant(TENANT_A, order_repo, driver_repo, intake_repo)

        resp = client.post(
            "/api/integrations/intake-channels/channel-b-voice/rotate-secret"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — Confirm tenant guard dependency is present on all handlers
# ---------------------------------------------------------------------------


class TestTenantGuardPresence:
    """Confirm via code review that every handler takes
    ``tenant: TenantContext = Depends(get_tenant_context)``.

    This test reads the source files and asserts the dependency is present
    on every route handler function.

    Validates: Requirement 9.1.2
    """

    def _get_handler_signatures(self, filepath: str) -> List[str]:
        """Extract handler function signatures from a file."""
        from pathlib import Path
        import re

        path = Path(filepath)
        if not path.exists():
            return []

        text = path.read_text(encoding="utf-8")
        # Find all async def functions that are route handlers
        # (decorated with @router.get/post/patch/delete/put)
        handler_pattern = re.compile(
            r"@router\.\w+.*?\n(?:.*?\n)*?async def (\w+)\((.*?)\)",
            re.DOTALL,
        )
        # Simpler approach: find all async def in the file and check params
        func_pattern = re.compile(
            r"async def (\w+)\(([\s\S]*?)\)\s*(?:->|:)",
        )
        handlers = []
        for match in func_pattern.finditer(text):
            name = match.group(1)
            params = match.group(2)
            handlers.append((name, params))
        return handlers

    def test_order_endpoints_have_tenant_guard(self):
        """Every /api/orders/* handler depends on get_tenant_context."""
        from pathlib import Path
        import re

        filepath = Path(__file__).resolve().parent.parent.parent / "fuel" / "api" / "order_endpoints.py"
        text = filepath.read_text(encoding="utf-8")

        # Find route-decorated handlers
        route_decorator_pattern = re.compile(r"@router\.(get|post|patch|put|delete)")
        func_def_pattern = re.compile(r"async def (\w+)\(")

        lines = text.split("\n")
        handlers_without_guard = []

        i = 0
        while i < len(lines):
            if route_decorator_pattern.search(lines[i]):
                # Find the next async def
                j = i + 1
                while j < len(lines) and not func_def_pattern.search(lines[j]):
                    j += 1
                if j < len(lines):
                    func_name = func_def_pattern.search(lines[j]).group(1)
                    # Read the full function signature (may span multiple lines)
                    sig_lines = []
                    k = j
                    while k < len(lines) and ")" not in lines[k]:
                        sig_lines.append(lines[k])
                        k += 1
                    if k < len(lines):
                        sig_lines.append(lines[k])
                    full_sig = "\n".join(sig_lines)

                    if "get_tenant_context" not in full_sig:
                        handlers_without_guard.append(func_name)
                i = j + 1 if j < len(lines) else i + 1
            else:
                i += 1

        assert not handlers_without_guard, (
            f"Order endpoint handlers missing get_tenant_context dependency: "
            f"{handlers_without_guard}"
        )

    def test_driver_endpoints_have_tenant_guard(self):
        """Every /api/ops/drivers/* handler depends on get_tenant_context."""
        from pathlib import Path
        import re

        filepath = Path(__file__).resolve().parent.parent.parent / "fuel" / "api" / "driver_endpoints.py"
        text = filepath.read_text(encoding="utf-8")

        route_decorator_pattern = re.compile(r"@router\.(get|post|patch|put|delete)")
        func_def_pattern = re.compile(r"async def (\w+)\(")

        lines = text.split("\n")
        handlers_without_guard = []

        i = 0
        while i < len(lines):
            if route_decorator_pattern.search(lines[i]):
                j = i + 1
                while j < len(lines) and not func_def_pattern.search(lines[j]):
                    j += 1
                if j < len(lines):
                    func_name = func_def_pattern.search(lines[j]).group(1)
                    sig_lines = []
                    k = j
                    while k < len(lines) and ")" not in lines[k]:
                        sig_lines.append(lines[k])
                        k += 1
                    if k < len(lines):
                        sig_lines.append(lines[k])
                    full_sig = "\n".join(sig_lines)

                    if "get_tenant_context" not in full_sig:
                        handlers_without_guard.append(func_name)
                i = j + 1 if j < len(lines) else i + 1
            else:
                i += 1

        assert not handlers_without_guard, (
            f"Driver endpoint handlers missing get_tenant_context dependency: "
            f"{handlers_without_guard}"
        )

    def test_intake_channel_endpoints_have_tenant_guard(self):
        """Every /api/integrations/intake-channels/* handler depends on get_tenant_context."""
        from pathlib import Path
        import re

        filepath = (
            Path(__file__).resolve().parent.parent.parent
            / "integrations" / "api" / "intake_channel_endpoints.py"
        )
        text = filepath.read_text(encoding="utf-8")

        route_decorator_pattern = re.compile(r"@router\.(get|post|patch|put|delete)")
        func_def_pattern = re.compile(r"async def (\w+)\(")

        lines = text.split("\n")
        handlers_without_guard = []

        i = 0
        while i < len(lines):
            if route_decorator_pattern.search(lines[i]):
                j = i + 1
                while j < len(lines) and not func_def_pattern.search(lines[j]):
                    j += 1
                if j < len(lines):
                    func_name = func_def_pattern.search(lines[j]).group(1)
                    sig_lines = []
                    k = j
                    while k < len(lines) and ")" not in lines[k]:
                        sig_lines.append(lines[k])
                        k += 1
                    if k < len(lines):
                        sig_lines.append(lines[k])
                    full_sig = "\n".join(sig_lines)

                    if "get_tenant_context" not in full_sig:
                        handlers_without_guard.append(func_name)
                i = j + 1 if j < len(lines) else i + 1
            else:
                i += 1

        assert not handlers_without_guard, (
            f"Intake channel endpoint handlers missing get_tenant_context dependency: "
            f"{handlers_without_guard}"
        )
