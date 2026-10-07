"""HTTP-level tests for ``/api/compliance/meters`` (finding C7, C11).

Built on the real ``MeterAuditService`` over a mocked document store.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compliance.api import meter_endpoints
from compliance.services.meter_audit_service import MeterAuditService
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"
URL = "/api/compliance/meters"
HEADERS = auth_headers(TENANT, roles=["admin"])


def _resp(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"hits": {"hits": [{"_source": s} for s in sources], "total": {"value": len(sources)}}}


def _existing_meter() -> Dict[str, Any]:
    return {
        "meter_id": "meter_QA-1",
        "tenant_id": TENANT,
        "meter_number": "QA-MTR-1",
        "truck_id": "QA-TRUCK-01",
        "calibration_certificate_number": "QA-CAL-1",
        "calibration_date": "2026-01-01",
        "calibration_expiry_date": "2027-01-01",
        "weights_measures_authority": "QA W&M",
        "status": "active",
    }


@pytest.fixture
def es():
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(return_value=_resp([]))
    return es


@pytest.fixture
def client(es):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(meter_endpoints.router)
    install_test_auth(app)
    meter_endpoints.configure_meter_api(meter_service=MeterAuditService(es))
    yield TestClient(app)
    meter_endpoints._meter_service = None


def _body(**overrides) -> Dict[str, Any]:
    body = {
        "meter_number": "QA-MTR-1",
        "truck_id": "QA-TRUCK-01",
        "calibration_certificate_number": "QA-CAL-1",
        "calibration_date": "2026-01-01",
        "calibration_expiry_date": "2027-01-01",
        "weights_measures_authority": "QA W&M",
    }
    body.update(overrides)
    return body


def test_reversed_calibration_dates_are_422(client, es):
    resp = client.post(
        URL,
        json=_body(calibration_date="2026-06-01", calibration_expiry_date="2026-01-01"),
        headers=HEADERS,
    )
    assert resp.status_code == 422, resp.text
    assert "meters.invalid_payload" in resp.text
    es.index_document.assert_not_awaited()


def test_duplicate_meter_number_is_409_envelope(client, es):
    es.search_documents = AsyncMock(return_value=_resp([_existing_meter()]))
    resp = client.post(URL, json=_body(), headers=HEADERS)
    assert resp.status_code == 409, resp.text
    payload = resp.json()
    assert payload["error_code"] == "DUPLICATE_METER_NUMBER"
    assert payload.get("request_id")
    es.index_document.assert_not_awaited()


def test_duplicate_number_with_reversed_dates_is_422(client, es):
    """OI-34: the payload is validated before the duplicate check."""
    es.search_documents = AsyncMock(return_value=_resp([_existing_meter()]))
    resp = client.post(
        URL,
        json=_body(calibration_date="2026-06-01", calibration_expiry_date="2026-01-01"),
        headers=HEADERS,
    )
    assert resp.status_code == 422, resp.text
    assert "meters.invalid_payload" in resp.text
    es.index_document.assert_not_awaited()


def test_valid_meter_is_created(client, es):
    resp = client.post(URL, json=_body(), headers=HEADERS)
    assert resp.status_code == 201, resp.text
    es.index_document.assert_awaited_once()


def test_audit_trail_for_unknown_meter_is_404(client):
    """Finding C11: it answered 200 [] while GET /meters/{id} answered 404."""
    resp = client.get(f"{URL}/meter_QA-nope/audit-trail", headers=HEADERS)
    assert resp.status_code == 404, resp.text
    payload = resp.json()
    assert payload["error_code"] == "RESOURCE_NOT_FOUND"
    assert payload.get("request_id")


def test_audit_trail_for_known_meter_is_200(client, es):
    async def _search(index, query, size=100, **kw):
        if index == "meter_registry":
            return _resp([_existing_meter()])
        return _resp([])

    es.search_documents = AsyncMock(side_effect=_search)
    resp = client.get(f"{URL}/meter_QA-1/audit-trail", headers=HEADERS)
    assert resp.status_code == 200, resp.text
