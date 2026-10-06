"""Unknown tank ids on the K-factor routes are 404 (finding C11).

``/suggest`` answered 200 ``suggested_kfactor: null`` for a tank that does not
exist; the other per-tank routes were 422 or 200 []. They now share one
not-found answer, in the standard envelope. A store outage stays a 500.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compliance.api import kfactor_endpoints
from compliance.services.kfactor_calibration_service import KFactorCalibrationService
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "demo-tenant"
BASE = "/api/compliance/kfactor/QA-TANK-NOPE"
HEADERS = auth_headers(TENANT, roles=["admin"])

ROUTES = [
    pytest.param("GET", f"{BASE}/suggest", None, id="suggest"),
    pytest.param("GET", f"{BASE}/variance-history", None, id="variance-history"),
    pytest.param("GET", f"{BASE}/variance?delivery_id=QA-DEL-1", None, id="variance"),
    pytest.param("POST", f"{BASE}/approve", {"new_kfactor": 1.5}, id="approve"),
]


@pytest.fixture
def es():
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    return es


@pytest.fixture
def client(es):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(kfactor_endpoints.router)
    install_test_auth(app)
    kfactor_endpoints.configure_kfactor_api(kfactor_service=KFactorCalibrationService(es_service=es))
    yield TestClient(app, raise_server_exceptions=False)
    kfactor_endpoints._kfactor_service = None


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
def test_unknown_tank_is_404(client, method, url, body):
    resp = client.request(method, url, json=body, headers=HEADERS)
    assert resp.status_code == 404, resp.text
    payload = resp.json()
    assert payload["error_code"] == "RESOURCE_NOT_FOUND"
    assert payload["details"]["tank_id"] == "QA-TANK-NOPE"
    assert payload.get("request_id")


def test_store_outage_is_not_a_404(client, es):
    es.search_documents = AsyncMock(side_effect=RuntimeError("store down"))
    resp = client.get(f"{BASE}/suggest", headers=HEADERS)
    assert resp.status_code == 500, resp.text
