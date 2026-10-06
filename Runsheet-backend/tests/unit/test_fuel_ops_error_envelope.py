"""Fuel-ops errors use the standard envelope (finding F11, decision D5).

``fuel/api/fuel_ops_endpoints.py`` raised ``HTTPException(detail={...})``,
which nests the code under ``detail`` and carries no ``request_id``. As
``AppException`` (string codes kept, lowercase) they render top-level
``error_code``, ``message`` and ``request_id`` like every other API error.
Also pins the customer-tank PATCH 422 not echoing the stored document, and
that a 500 no longer returns ``str(exc)``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from errors.handlers import register_exception_handlers
from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
from fuel.customer_tank_models import CustomerTankRepository
from fuel.depot_models import DepotRepository
from middleware.request_id import RequestIDMiddleware
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit.test_fuel_ops_customer_tank_endpoints import (
    _FakeESService as _TankES,
    _base_create_payload as _tank_payload,
)
from tests.unit.test_fuel_ops_depot_endpoints import _FakeESService as _DepotES

REQUEST_ID = "req-f11-envelope"


def _app(**configure: Any) -> TestClient:
    configure_fuel_ops_endpoints(**configure)
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id="tenant-1",
        user_id="user-1",
        has_pii_access=False,
        roles=["dispatcher"],
    )
    return TestClient(app, raise_server_exceptions=False)


def _assert_standard(resp, status: int, code: str) -> Dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert "detail" not in body
    assert body["error_code"] == code
    assert isinstance(body["message"], str) and body["message"]
    assert body["request_id"] == REQUEST_ID
    return body


def _tank_client():
    es = _TankES()
    return _app(es_service=es, customer_tank_repository=CustomerTankRepository(es_service=es)), es


def _depot_client():
    es = _DepotES()
    return _app(es_service=es, depot_repository=DepotRepository(es_service=es)), es


def test_depot_delete_missing_is_404_standard_envelope():
    client, _ = _depot_client()
    resp = client.delete("/api/fuel/mvp/depots/nope", headers={"X-Request-ID": REQUEST_ID})
    body = _assert_standard(resp, 404, "depot_not_found")
    assert body["details"]["depot_id"] == "nope"


def test_customer_tank_get_missing_is_404_standard_envelope():
    client, _ = _tank_client()
    resp = client.get(
        "/api/fuel/mvp/customer-tanks/nope", headers={"X-Request-ID": REQUEST_ID}
    )
    body = _assert_standard(resp, 404, "customer_tank_not_found")
    assert body["details"] == {"customer_tank_id": "nope"}


def test_customer_tank_cross_tenant_is_403_standard_envelope():
    client, es = _tank_client()
    es.docs["tank_other"] = {**_tank_payload(customer_tank_id="tank_other"), "tenant_id": "tenant-2"}
    resp = client.patch(
        "/api/fuel/mvp/customer-tanks/tank_other",
        json={"current_level_gallons": 10.0},
        headers={"X-Request-ID": REQUEST_ID},
    )
    body = _assert_standard(resp, 403, "cross_tenant_access_denied")
    assert "tenant-2" not in resp.text
    assert body["details"] == {"customer_tank_id": "tank_other"}


def test_customer_tank_patch_422_does_not_echo_the_stored_document():
    client, es = _tank_client()
    es.docs["tank_001"] = {
        **_tank_payload(zip_code="99999-SECRET", customer_id="cust_secret_owner"),
        "tenant_id": "tenant-1",
    }
    resp = client.patch(
        "/api/fuel/mvp/customer-tanks/tank_001",
        json={"current_level_gallons": 9_999.0},
        headers={"X-Request-ID": REQUEST_ID},
    )
    body = _assert_standard(resp, 422, "validation_error")
    # Field errors only: no stored values anywhere in the body.
    assert "99999-SECRET" not in resp.text
    assert "cust_secret_owner" not in resp.text
    for err in body["details"]["errors"]:
        assert "input" not in err
        assert set(err) <= {"type", "loc", "msg"}


def test_unexpected_500_does_not_return_exception_text(monkeypatch):
    client, es = _tank_client()

    async def _boom(*args, **kwargs):
        raise RuntimeError("secret-dsn://user:pw@db")

    from fuel.services.delivery_destination_service import DeliveryDestinationService

    monkeypatch.setattr(DeliveryDestinationService, "list", _boom)
    resp = client.get(
        "/api/fuel/destinations", headers={"X-Request-ID": REQUEST_ID}
    )
    assert resp.status_code == 500, resp.text
    assert "secret-dsn" not in resp.text
    assert resp.json()["request_id"] == REQUEST_ID


def test_no_http_exception_raise_sites_remain():
    source = (
        Path(__file__).resolve().parents[2] / "fuel" / "api" / "fuel_ops_endpoints.py"
    ).read_text()
    assert re.findall(r"HTTPException\(", source) == []


class TestAppExceptionStringCodes:
    def test_string_code_needs_a_status(self):
        with pytest.raises(ValueError):
            AppException("depot_not_found", "Depot not found")

    def test_string_code_renders_like_an_enum_code(self):
        exc = AppException("depot_not_found", "Depot not found", status_code=404, details={"depot_id": "x"})
        assert exc.to_dict() == {
            "error_code": "depot_not_found",
            "message": "Depot not found",
            "details": {"depot_id": "x"},
        }
        assert "depot_not_found" in repr(exc)
        assert exc.status_code == 404
