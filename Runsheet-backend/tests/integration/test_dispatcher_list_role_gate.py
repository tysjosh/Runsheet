"""
Integration test — regression guard for the one removed permission (R3.13).

The driver-mobile-app feature is additive everywhere except here: R3.13 applies
``require_role(tenant, "dispatcher", "admin")`` to the two dispatcher list
surfaces — ``GET /api/orders`` and ``GET /api/scheduling/jobs`` — closing the
verified hole where ``GET /api/orders?driver_id=`` had no role gate at all and a
``driver``-only session could read every order in the tenant.

Because this is the only change in the feature that takes access away from an
existing caller, the guard is explicit and three-cased per endpoint:

1. a ``dispatcher``-role session still passes (200);
2. an ``admin``-role session still passes (200);
3. a ``driver``-only session now receives 403 ``INSUFFICIENT_ROLE``.

Sessions come from the Test_Auth_Path seam (``tests/support/auth_seam.py``), so
the roles under test are carried on the verified ``TenantContext`` exactly as a
real SuperTokens session would carry them.

Validates: Requirements 3.13
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support.auth_seam import auth_headers, install_test_auth

# ---------------------------------------------------------------------------
# Patch the ElasticsearchService singleton BEFORE any scheduling imports, the
# same way the scheduling endpoint tests do.
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from errors.exceptions import AppException  # noqa: E402
from fuel.api.order_endpoints import configure_order_endpoints  # noqa: E402
from fuel.api.order_endpoints import router as order_router  # noqa: E402
from fuel.order_models import FuelOrder  # noqa: E402
from scheduling.api.endpoints import (  # noqa: E402
    configure_scheduling_api,
    router as scheduling_router,
)
from scheduling.services.cargo_service import CargoService  # noqa: E402
from scheduling.services.delay_detection_service import DelayDetectionService  # noqa: E402
from scheduling.services.job_service import JobService  # noqa: E402

TENANT_ID = "t1"
_NOW = datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc)

#: The three sessions under test. ``driver`` is the one that loses access.
DISPATCHER_HEADERS = auth_headers(TENANT_ID, sub="user-dispatcher", roles=["dispatcher"])
ADMIN_HEADERS = auth_headers(TENANT_ID, sub="user-admin", roles=["admin"])
DRIVER_HEADERS = auth_headers(
    TENANT_ID, sub="user-driver", roles=["driver"], driver_id="drv_1"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_order(order_id: str = "ord_abc123") -> FuelOrder:
    return FuelOrder(
        order_id=order_id,
        tenant_id=TENANT_ID,
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
        status="dispatched",
        assigned_driver_id="drv_1",
        source_schema_version="1.0",
        trace_id="trace-001",
        created_at=_NOW,
        updated_at=_NOW,
        last_event_timestamp=_NOW,
    )


class FakeOrderRepository:
    """Minimal order repository serving only the list path under test."""

    def __init__(self, orders: Optional[List[FuelOrder]] = None):
        self._orders: List[FuelOrder] = list(orders or [])

    async def search(self, *, tenant_id: str, **kwargs) -> Dict[str, Any]:
        orders = [o for o in self._orders if o.tenant_id == tenant_id]
        return {"orders": orders, "total": len(orders), "page": 1, "size": 20}


def _make_es_mock() -> MagicMock:
    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    return es


def _make_job_service(es_mock: MagicMock) -> JobService:
    with patch("scheduling.services.job_service.get_settings") as mock_settings:
        settings_obj = MagicMock()
        settings_obj.scheduling_default_eta_hours = 4
        mock_settings.return_value = settings_obj
        return JobService(es_service=es_mock, redis_url=None)


@pytest.fixture()
def client() -> TestClient:
    """App carrying both dispatcher list surfaces plus the Test_Auth_Path seam."""
    configure_order_endpoints(
        order_intake_pipeline=MagicMock(),
        order_repository=FakeOrderRepository([_make_order()]),
        driver_repository=MagicMock(),
    )
    es = _make_es_mock()
    configure_scheduling_api(
        job_service=_make_job_service(es),
        cargo_service=CargoService(es_service=es),
        delay_service=DelayDetectionService(es_service=es, ws_manager=None),
    )

    app = FastAPI()
    app.include_router(order_router)
    app.include_router(scheduling_router)

    @app.exception_handler(AppException)
    async def _handler(request: Request, exc: AppException):  # noqa: ANN202
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_dict()})

    install_test_auth(app)
    return TestClient(app)


def _error_code(resp) -> str:
    body = resp.json()
    detail = body.get("detail", body)
    return detail.get("error_code")


# ---------------------------------------------------------------------------
# GET /api/orders — the endpoint that had no role gate at all
# ---------------------------------------------------------------------------


class TestOrderListRoleGate:
    """R3.13 on ``GET /api/orders``."""

    def test_dispatcher_session_still_passes(self, client: TestClient):
        resp = client.get("/api/orders", headers=DISPATCHER_HEADERS)

        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_admin_session_still_passes(self, client: TestClient):
        resp = client.get("/api/orders", headers=ADMIN_HEADERS)

        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_driver_only_session_is_now_forbidden(self, client: TestClient):
        """The removed permission: a driver session can no longer list orders.

        Including the ``driver_id`` filter that used to let a driver session
        read every order in the tenant — it is rejected before any search runs.
        """
        resp = client.get("/api/orders?driver_id=drv_other", headers=DRIVER_HEADERS)

        assert resp.status_code == 403
        assert _error_code(resp) == "INSUFFICIENT_ROLE"


# ---------------------------------------------------------------------------
# GET /api/scheduling/jobs
# ---------------------------------------------------------------------------


class TestJobListRoleGate:
    """R3.13 on ``GET /api/scheduling/jobs``."""

    def test_dispatcher_session_still_passes(self, client: TestClient):
        resp = client.get("/api/scheduling/jobs", headers=DISPATCHER_HEADERS)

        assert resp.status_code == 200
        assert "pagination" in resp.json()

    def test_admin_session_still_passes(self, client: TestClient):
        resp = client.get("/api/scheduling/jobs", headers=ADMIN_HEADERS)

        assert resp.status_code == 200
        assert "pagination" in resp.json()

    def test_driver_only_session_is_now_forbidden(self, client: TestClient):
        resp = client.get("/api/scheduling/jobs", headers=DRIVER_HEADERS)

        assert resp.status_code == 403
        assert _error_code(resp) == "INSUFFICIENT_ROLE"


# ---------------------------------------------------------------------------
# The rejection must not leak the caller's held roles (R15.14)
# ---------------------------------------------------------------------------


def test_rejection_echoes_only_the_requirement(client: TestClient):
    for path in ("/api/orders", "/api/scheduling/jobs"):
        resp = client.get(path, headers=DRIVER_HEADERS)
        detail = resp.json()["detail"]
        details = detail.get("details") or {}
        assert details.get("required_roles") == ["dispatcher", "admin"]
        assert "driver" not in str(details.get("held_roles", ""))
