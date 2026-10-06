"""``POST /api/compliance/asset-certifications`` for an existing truck (C3).

Uses the production asset loader over a mocked document store holding one
truck, so the write-time subject validation reads the same index it does in
production.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compliance.api import asset_certification_endpoints as ep
from errors.handlers import register_exception_handlers
from services.ref_loaders import make_asset_loader
from services.ref_resolver import RefResolver
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"
URL = "/api/compliance/asset-certifications"


def _es_with_truck() -> MagicMock:
    truck = {"truck_id": "QA-TRUCK-01", "tenant_id": TENANT, "name": "QA Truck", "status": "active"}

    async def _search(index, query, size=100, **kw):
        # Match the way the store would: only the ``trucks`` index, only this id.
        hits = [{"_source": truck}] if index == "trucks" and "QA-TRUCK-01" in repr(query) else []
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}}

    es = MagicMock()
    es.search_documents = AsyncMock(side_effect=_search)
    return es


@pytest.fixture
def client():
    resolver = RefResolver()
    resolver.register("asset", make_asset_loader(_es_with_truck()))
    svc = MagicMock()
    svc.create = AsyncMock(side_effect=lambda tenant_id, **kw: {"cert_id": "QA-CERT-1", **kw})
    ep.configure_asset_certification_api(asset_certification_service=svc, ref_resolver=resolver)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(ep.router)
    install_test_auth(app)
    yield TestClient(app)
    ep._asset_cert_service = None
    ep._ref_resolver = None


def _body(asset_id: str) -> dict:
    return {
        "asset_id": asset_id,
        "certification_type": "V_test",
        "certification_date": "2026-01-01",
        "expiry_date": "2027-01-01",
        "inspector_name": "QA Inspector",
        "certificate_number": "QA-CN-1",
    }


def test_cert_for_an_existing_truck_is_created(client):
    resp = client.post(URL, json=_body("QA-TRUCK-01"), headers=auth_headers(TENANT, roles=["admin"]))
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["asset_id"] == "QA-TRUCK-01"


def test_cert_for_an_unknown_asset_is_still_rejected(client):
    resp = client.post(URL, json=_body("QA-NOPE"), headers=auth_headers(TENANT, roles=["admin"]))
    assert resp.status_code == 400, resp.text
    assert resp.json()["details"]["reason"] == "asset_not_found"


@pytest.fixture
def real_service_client():
    """The real service over a mocked ES, so the model validators run."""
    from compliance.services.asset_certification_service import AssetCertificationService

    resolver = RefResolver()
    es = _es_with_truck()
    es.index_document = AsyncMock(return_value=None)
    resolver.register("asset", make_asset_loader(es))
    ep.configure_asset_certification_api(
        asset_certification_service=AssetCertificationService(es), ref_resolver=resolver
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(ep.router)
    install_test_auth(app)
    yield TestClient(app), es
    ep._asset_cert_service = None
    ep._ref_resolver = None


def _error_code(body: dict):
    if "error_code" in body:
        return body["error_code"]
    detail = body.get("detail")
    return detail.get("error_code") if isinstance(detail, dict) else None


def test_expiry_before_certification_date_is_422(real_service_client):
    client, es = real_service_client
    body = {**_body("QA-TRUCK-01"), "certification_date": "2027-01-01", "expiry_date": "2026-01-01"}

    resp = client.post(URL, json=body, headers=auth_headers(TENANT, roles=["admin"]))

    assert resp.status_code == 422, resp.text
    assert _error_code(resp.json()) == "asset_certifications.invalid_payload"
    es.index_document.assert_not_called()
