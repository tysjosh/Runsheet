"""Compliance 4xx errors use the standard envelope (C12/C-2).

These handlers raised ``HTTPException(detail={...})``, which nests the code
under ``detail`` and carries no ``request_id``. As ``AppException`` they
render top-level ``error_code`` + ``request_id``. The code strings are
unchanged. Each case drives the real service over a mocked store.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compliance.api import (
    asset_certification_endpoints,
    kfactor_endpoints,
    meter_endpoints,
    terminal_bol_endpoints,
)
from compliance.services.asset_certification_service import AssetCertificationService
from compliance.services.kfactor_calibration_service import KFactorCalibrationService
from compliance.services.meter_audit_service import MeterAuditService
from compliance.services.terminal_bol_edi_parser import create_default_registry
from compliance.services.terminal_bol_ingestion_service import (
    TerminalBOLIngestionService,
)
from errors.handlers import register_exception_handlers
from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"
HEADERS = auth_headers(TENANT, roles=["admin"])
_TANK = {"tank_id": "QA-TANK-1", "tenant_id": TENANT, "kfactor": 1.0}
_TRUCK = {"truck_id": "QA-TRUCK-01", "tenant_id": TENANT, "name": "QA", "status": "active"}


def _es() -> MagicMock:
    async def _search(index, query, size=10, **kw):
        hits = []
        if index == CUSTOMER_TANKS_INDEX:
            hits = [{"_source": _TANK}]
        elif index == "trucks" and "QA-TRUCK-01" in repr(query):
            hits = [{"_source": _TRUCK}]
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}, "aggregations": {}}

    es = MagicMock()
    es.search_documents = AsyncMock(side_effect=_search)
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.get_document = AsyncMock(return_value=None)
    return es


@pytest.fixture
def client():
    from services.ref_loaders import make_asset_loader
    from services.ref_resolver import RefResolver

    es = _es()
    resolver = RefResolver()
    resolver.register("asset", make_asset_loader(es))
    terminal_bol_endpoints.configure_terminal_bol_api(
        bol_service=TerminalBOLIngestionService(
            es_service=es, edi_parser_registry=create_default_registry()
        ),
        es_service=es,
    )
    asset_certification_endpoints.configure_asset_certification_api(
        asset_certification_service=AssetCertificationService(es), ref_resolver=resolver
    )
    meter_endpoints.configure_meter_api(meter_service=MeterAuditService(es))
    kfactor_endpoints.configure_kfactor_api(
        kfactor_service=KFactorCalibrationService(es_service=es)
    )

    app = FastAPI()
    register_exception_handlers(app)
    for mod in (
        terminal_bol_endpoints,
        asset_certification_endpoints,
        meter_endpoints,
        kfactor_endpoints,
    ):
        app.include_router(mod.router)
    install_test_auth(app)
    yield TestClient(app, raise_server_exceptions=False)

    terminal_bol_endpoints._bol_service = None
    terminal_bol_endpoints._es_service = None
    asset_certification_endpoints._asset_cert_service = None
    asset_certification_endpoints._ref_resolver = None
    meter_endpoints._meter_service = None
    kfactor_endpoints._kfactor_service = None


def _assert_envelope(resp, status: int, code: str) -> Dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error_code"] == code
    assert body.get("request_id")
    assert "detail" not in body
    return body


def test_edi_not_a_document_is_422(client):
    resp = client.post(
        "/api/compliance/terminal-bols",
        content=b"not an EDI document",
        headers={**HEADERS, "Content-Type": "text/plain"},
    )
    _assert_envelope(resp, 422, "terminal_bols.invalid_edi")


def test_asset_cert_reversed_dates_is_422(client):
    resp = client.post(
        "/api/compliance/asset-certifications",
        json={
            "asset_id": "QA-TRUCK-01",
            "certification_type": "V_test",
            "certification_date": "2027-01-01",
            "expiry_date": "2026-01-01",
            "inspector_name": "QA Inspector",
            "certificate_number": "QA-CN-1",
        },
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "asset_certifications.invalid_payload")


def test_meter_reversed_dates_is_422(client):
    resp = client.post(
        "/api/compliance/meters",
        json={
            "meter_number": "QA-MTR-1",
            "truck_id": "QA-TRUCK-01",
            "calibration_certificate_number": "QA-CAL-1",
            "calibration_date": "2027-01-01",
            "calibration_expiry_date": "2026-01-01",
            "weights_measures_authority": "QA W&M",
        },
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "meters.invalid_payload")


def test_kfactor_invalid_adjustment_is_422(client):
    # A real tank with no deliveries: fewer than the calibration minimum.
    resp = client.post(
        "/api/compliance/kfactor/QA-TANK-1/approve",
        json={"new_kfactor": 1.5},
        headers=HEADERS,
    )
    _assert_envelope(resp, 422, "kfactor.invalid_adjustment")


def test_kfactor_variance_unknown_delivery_is_404(client):
    resp = client.get(
        "/api/compliance/kfactor/QA-TANK-1/variance?delivery_id=QA-DEL-NOPE",
        headers=HEADERS,
    )
    body = _assert_envelope(resp, 404, "RESOURCE_NOT_FOUND")
    assert body["details"]["delivery_id"] == "QA-DEL-NOPE"
