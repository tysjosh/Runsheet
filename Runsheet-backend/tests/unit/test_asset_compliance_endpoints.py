"""Unit tests for the asset compliance-status signal (task 10.2).

Covers the aggregator service
(:class:`AssetComplianceStatusService`) and the
``GET /api/fleet/assets/{asset_id}/compliance`` endpoint that surfaces a
per-asset compliance signal to the Fleet assignment decision surface so an
operator does not dispatch a non-compliant asset.

Validates: Requirements 11.2.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from compliance.api.asset_compliance_endpoints import (
    configure_asset_compliance_api,
    router,
)
from compliance.services.asset_compliance_status_service import (
    AssetComplianceStatusService,
)
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes for the underlying compliance services (tenant-scoped, in-memory)
# ---------------------------------------------------------------------------


class FakeCertService:
    """In-memory fake of AssetCertificationService.list()."""

    def __init__(self) -> None:
        # keyed by (tenant_id, asset_id) -> list of cert docs
        self._certs: Dict[str, List[Dict[str, Any]]] = {}

    def seed(self, tenant_id: str, asset_id: str, docs: List[Dict[str, Any]]) -> None:
        self._certs[f"{tenant_id}::{asset_id}"] = docs

    async def list(
        self,
        tenant_id: str,
        *,
        asset_id: Optional[str] = None,
        certification_type: Optional[str] = None,
        status: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        items = list(self._certs.get(f"{tenant_id}::{asset_id}", []))
        return {"items": items, "next_cursor": None, "limit": limit}


class FakeMeterService:
    """In-memory fake of MeterAuditService.list_meters()."""

    def __init__(self) -> None:
        self._meters: Dict[str, List[Dict[str, Any]]] = {}

    def seed(self, tenant_id: str, truck_id: str, docs: List[Dict[str, Any]]) -> None:
        self._meters[f"{tenant_id}::{truck_id}"] = docs

    async def list_meters(
        self,
        tenant_id: str,
        *,
        truck_id: Optional[str] = None,
        status: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        items = list(self._meters.get(f"{tenant_id}::{truck_id}", []))
        return {"items": items, "next_cursor": None, "limit": limit}


def _iso(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


def _cert(cert_id: str, cert_type: str, *, days: int, status: str = "valid") -> Dict[str, Any]:
    return {
        "cert_id": cert_id,
        "certification_type": cert_type,
        "expiry_date": _iso(days),
        "status": status,
    }


def _meter(meter_id: str, number: str, *, days: int, status: str = "active") -> Dict[str, Any]:
    return {
        "meter_id": meter_id,
        "meter_number": number,
        "calibration_expiry_date": _iso(days),
        "status": status,
    }


def _build_service(certs: FakeCertService, meters: FakeMeterService):
    return AssetComplianceStatusService(
        certification_service=certs, meter_audit_service=meters
    )


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAssetComplianceStatusService:
    async def test_no_records_returns_unknown(self):
        svc = _build_service(FakeCertService(), FakeMeterService())
        summary = await svc.get_asset_compliance_summary("t1", "A-1")
        assert summary.overall_status == "unknown"
        assert summary.has_records is False
        assert summary.items == []

    async def test_all_valid(self):
        certs = FakeCertService()
        certs.seed("t1", "A-1", [_cert("c1", "V_test", days=400)])
        meters = FakeMeterService()
        meters.seed("t1", "A-1", [_meter("m1", "MTR-1", days=400)])
        svc = _build_service(certs, meters)

        summary = await svc.get_asset_compliance_summary("t1", "A-1")
        assert summary.overall_status == "valid"
        assert summary.has_records is True
        assert len(summary.items) == 2

    async def test_expiring_certification(self):
        certs = FakeCertService()
        # 30 days out → within the 60-day cert warning threshold → expiring
        certs.seed("t1", "A-1", [_cert("c1", "K_test", days=30)])
        svc = _build_service(certs, FakeMeterService())

        summary = await svc.get_asset_compliance_summary("t1", "A-1")
        assert summary.overall_status == "expiring"

    async def test_expired_certification_wins_over_expiring_meter(self):
        certs = FakeCertService()
        certs.seed("t1", "A-1", [_cert("c1", "V_test", days=-1)])  # expired
        meters = FakeMeterService()
        meters.seed("t1", "A-1", [_meter("m1", "MTR-1", days=10)])  # expiring
        svc = _build_service(certs, meters)

        summary = await svc.get_asset_compliance_summary("t1", "A-1")
        assert summary.overall_status == "expired"

    async def test_meter_out_of_calibration_is_expired(self):
        meters = FakeMeterService()
        meters.seed("t1", "A-1", [_meter("m1", "MTR-1", days=-5)])
        svc = _build_service(FakeCertService(), meters)

        summary = await svc.get_asset_compliance_summary("t1", "A-1")
        assert summary.overall_status == "expired"
        meter_item = next(i for i in summary.items if i.kind == "meter")
        assert "out of calibration" in (meter_item.detail or "")

    async def test_superseded_certification_skipped(self):
        certs = FakeCertService()
        certs.seed(
            "t1",
            "A-1",
            [_cert("c1", "V_test", days=-100, status="superseded")],
        )
        svc = _build_service(certs, FakeMeterService())

        summary = await svc.get_asset_compliance_summary("t1", "A-1")
        # The only cert was superseded → no contributing records.
        assert summary.overall_status == "unknown"
        assert summary.has_records is False

    async def test_retired_meter_skipped(self):
        meters = FakeMeterService()
        meters.seed("t1", "A-1", [_meter("m1", "MTR-1", days=-100, status="retired")])
        svc = _build_service(FakeCertService(), meters)

        summary = await svc.get_asset_compliance_summary("t1", "A-1")
        assert summary.overall_status == "unknown"

    async def test_doc_status_expired_respected_without_date(self):
        certs = FakeCertService()
        doc = {"cert_id": "c1", "certification_type": "I_test", "status": "expired"}
        certs.seed("t1", "A-1", [doc])
        svc = _build_service(certs, FakeMeterService())

        summary = await svc.get_asset_compliance_summary("t1", "A-1")
        assert summary.overall_status == "expired"

    async def test_tenant_isolation(self):
        certs = FakeCertService()
        certs.seed("t1", "A-1", [_cert("c1", "V_test", days=-1)])
        svc = _build_service(certs, FakeMeterService())

        # A different tenant sees no records for the same asset id.
        summary = await svc.get_asset_compliance_summary("t2", "A-1")
        assert summary.overall_status == "unknown"


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


def _tenant_ctx(tenant_id: str = "t1") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id="u1",
        has_pii_access=False,
        roles=["dispatcher"],
    )


def _build_app(svc: AssetComplianceStatusService, tenant_id: str = "t1") -> TestClient:
    configure_asset_compliance_api(asset_compliance_status_service=svc)
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _handler(request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_dict()})

    app.dependency_overrides[get_tenant_context] = lambda: _tenant_ctx(tenant_id)
    return TestClient(app)


class TestAssetComplianceEndpoint:
    def test_returns_overall_status_and_items(self):
        certs = FakeCertService()
        certs.seed("t1", "A-1", [_cert("c1", "V_test", days=30)])
        meters = FakeMeterService()
        meters.seed("t1", "A-1", [_meter("m1", "MTR-1", days=400)])
        client = _build_app(_build_service(certs, meters))

        resp = client.get("/api/fleet/assets/A-1/compliance")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["asset_id"] == "A-1"
        assert data["overall_status"] == "expiring"
        assert data["has_records"] is True
        assert len(data["items"]) == 2

    def test_unknown_when_no_records(self):
        client = _build_app(_build_service(FakeCertService(), FakeMeterService()))

        resp = client.get("/api/fleet/assets/A-404/compliance")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["overall_status"] == "unknown"
        assert data["has_records"] is False
