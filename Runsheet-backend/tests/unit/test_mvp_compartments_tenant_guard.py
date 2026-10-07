"""PUT /api/fuel/mvp/compartments/{truck_id} refuses a cross-tenant overwrite (OI-07).

Compartment ids are ``{truck_id}_{compartment_id}`` and global across tenants.
The handler used to replace whatever document sat at that id. It now pre-reads
every target, and a document owned by another tenant gets a generic 409 with
nothing written.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT_A = "tenant-A"
TENANT_B = "tenant-B"


def _body(*compartment_ids):
    return {
        "compartments": [
            {
                "compartment_id": cid,
                "capacity_liters": 10_000.0,
                "allowed_grades": ["DIESEL_2"],
                "position_index": i,
            }
            for i, cid in enumerate(compartment_ids)
        ]
    }


@pytest.fixture
def harness(monkeypatch):
    from Agents.support import mvp_endpoints
    from commerce.services import commerce_persistence_bridge

    store = {}
    es = MagicMock()
    es.get_document = AsyncMock(side_effect=lambda _idx, doc_id: store.get(doc_id))
    es.index_document = AsyncMock()
    mirror = AsyncMock()
    monkeypatch.setattr(mvp_endpoints, "_es_service", es)
    monkeypatch.setattr(mvp_endpoints, "_fleet_registration_service", None)
    monkeypatch.setattr(
        commerce_persistence_bridge, "mirror_current_state_upsert", mirror
    )

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(mvp_endpoints.router)
    install_test_auth(app)
    return store, es, mirror, app


def _client(app, tenant):
    return TestClient(app, headers=auth_headers(tenant), raise_server_exceptions=False)


def test_cross_tenant_put_returns_409_and_writes_nothing(harness):
    store, es, mirror, app = harness
    # c2 is free, c1 belongs to tenant A: nothing may be written, not even c2.
    store["TRUCK-1_c1"] = {"truck_id": "TRUCK-1", "compartment_id": "c1", "tenant_id": TENANT_A}

    resp = _client(app, TENANT_B).put("/api/fuel/mvp/compartments/TRUCK-1", json=_body("c2", "c1"))

    assert resp.status_code == 409, resp.text
    assert "Truck compartments are already in use" in resp.text
    assert TENANT_A not in resp.text  # never names the owner
    es.index_document.assert_not_awaited()
    mirror.assert_not_awaited()


def test_same_tenant_reput_carries_state_over(harness):
    store, es, mirror, app = harness
    store["TRUCK-1_c1"] = {
        "truck_id": "TRUCK-1",
        "compartment_id": "c1",
        "tenant_id": TENANT_A,
        "state": "loaded",
        "last_loaded_product": "DIESEL_2",
    }

    resp = _client(app, TENANT_A).put("/api/fuel/mvp/compartments/TRUCK-1", json=_body("c1"))

    assert resp.status_code == 200, resp.text
    es.index_document.assert_awaited_once()
    _idx, doc_id, doc = es.index_document.await_args.args
    assert doc_id == "TRUCK-1_c1"
    assert doc["tenant_id"] == TENANT_A
    assert doc["state"] == "loaded"
    assert doc["last_loaded_product"] == "DIESEL_2"
    mirror.assert_awaited_once()


def test_fresh_truck_is_configured(harness):
    _store, es, mirror, app = harness

    resp = _client(app, TENANT_B).put("/api/fuel/mvp/compartments/TRUCK-9", json=_body("c1", "c2"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["compartments_configured"] == 2
    assert es.index_document.await_count == 2
    assert mirror.await_count == 2
