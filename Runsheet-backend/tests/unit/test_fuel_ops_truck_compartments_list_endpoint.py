"""
Unit tests for ``GET /api/fuel/mvp/trucks/{truck_id}/compartments``
(Task 11.9 / Req 7.1.1–7.1.3).

The endpoint surfaces the lifecycle state triple the Compartment_Loading_Agent
writes so the truck-detail UI can render a state badge
(``clean`` | ``loaded`` | ``needs_cleaning``) alongside the static compartment
configuration. These tests exercise the router against an in-memory ES stub so
they never touch the real Elasticsearch backend.

Validates: Requirement 7.1.4.
"""
from __future__ import annotations

import unittest.mock as mock
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub backed by a list of pre-seeded hits.

    Only :meth:`search_documents` is implemented because the list
    endpoint is read-only. The fake echoes whatever hits the test seeds
    and records every query so assertions can verify tenant / truck
    filtering shape.
    """

    def __init__(self) -> None:
        self.hits: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self.raise_on_search: BaseException | None = None

    def seed(self, source: Dict[str, Any]) -> None:
        self.hits.append({"_source": source})

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        self.calls.append({"index": index, "query": query, "size": size})
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return {"hits": {"hits": list(self.hits)}}


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-1"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["dispatcher"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    *,
    tenant_id: str = "tenant-1",
    es: _FakeESService | None = None,
) -> tuple[FastAPI, _FakeESService]:
    es = es or _FakeESService()

    configure_fuel_ops_endpoints(
        es_service=es,
        destination_service=mock.MagicMock(list=mock.AsyncMock(return_value=[])),
        customer_tank_repository=mock.MagicMock(),
        depot_repository=mock.MagicMock(),
        terminal_repository=mock.MagicMock(),
        compartment_state_repository=mock.MagicMock(),
        cleaning_event_service=mock.MagicMock(),
        file_storage_service=mock.MagicMock(),
    )

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestListTruckCompartmentsSuccess:
    def test_returns_items_sorted_by_position_index(self):
        es = _FakeESService()
        # Seed deliberately out of order so the router must sort.
        es.seed(
            {
                "compartment_id": "TRUCK-1_c2",
                "truck_id": "TRUCK-1",
                "tenant_id": "tenant-1",
                "capacity_liters": 5000.0,
                "allowed_grades": ["DIESEL_2", "GASOLINE_REG"],
                "position_index": 2,
                "state": "needs_cleaning",
                "last_loaded_product": "HEATING_OIL",
                "last_loaded_at": "2024-06-01T10:00:00Z",
                "last_cleaned_at": None,
            }
        )
        es.seed(
            {
                "compartment_id": "TRUCK-1_c1",
                "truck_id": "TRUCK-1",
                "tenant_id": "tenant-1",
                "capacity_liters": 4000.0,
                "allowed_grades": ["PROPANE"],
                "position_index": 0,
                "state": "clean",
                "last_loaded_product": None,
                "last_loaded_at": None,
                "last_cleaned_at": "2024-05-20T09:00:00Z",
            }
        )
        app, _ = _build_app(es=es)
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/trucks/TRUCK-1/compartments")
        assert resp.status_code == 200
        body = resp.json()
        assert body["truck_id"] == "TRUCK-1"
        assert body["total"] == 2
        # Sorted by position_index.
        assert [c["compartment_id"] for c in body["items"]] == [
            "TRUCK-1_c1",
            "TRUCK-1_c2",
        ]
        first = body["items"][0]
        assert first["state"] == "clean"
        assert first["allowed_grades"] == ["PROPANE"]
        assert first["last_cleaned_at"] == "2024-05-20T09:00:00Z"
        second = body["items"][1]
        assert second["state"] == "needs_cleaning"
        assert second["last_loaded_product"] == "HEATING_OIL"

    def test_sends_tenant_and_truck_filters(self):
        es = _FakeESService()
        app, _ = _build_app(es=es)
        client = TestClient(app)
        client.get("/api/fuel/mvp/trucks/TRUCK-42/compartments")

        assert len(es.calls) == 1
        query = es.calls[0]["query"]
        must = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": "tenant-1"}} in must
        assert {"term": {"truck_id": "TRUCK-42"}} in must

    def test_empty_result_returns_empty_list_not_404(self):
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/trucks/UNKNOWN-TRUCK/compartments")
        assert resp.status_code == 200
        assert resp.json() == {
            "truck_id": "UNKNOWN-TRUCK",
            "items": [],
            "total": 0,
        }

    def test_defaults_missing_state_fields_to_clean(self):
        """Legacy pre-Task-6.1 docs lack the state triple; the router
        must coerce them into the ``state='clean'`` default rather
        than dropping the row."""

        es = _FakeESService()
        es.seed(
            {
                "compartment_id": "LEGACY_c1",
                "truck_id": "TRUCK-7",
                "tenant_id": "tenant-1",
                "capacity_liters": 3000.0,
                "allowed_grades": ["DIESEL_2"],
                "position_index": 0,
            }
        )
        app, _ = _build_app(es=es)
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/trucks/TRUCK-7/compartments")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["state"] == "clean"
        assert items[0]["last_loaded_product"] is None


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestListTruckCompartmentsTenantIsolation:
    def test_drops_cross_tenant_rows_defensively(self):
        es = _FakeESService()
        # Even if a mis-indexed doc slipped past the ES filter, the
        # router's per-row re-check must drop it.
        es.seed(
            {
                "compartment_id": "TRUCK-1_c1",
                "truck_id": "TRUCK-1",
                "tenant_id": "tenant-1",
                "capacity_liters": 5000.0,
                "allowed_grades": ["DIESEL_2"],
                "position_index": 0,
                "state": "clean",
            }
        )
        es.seed(
            {
                "compartment_id": "TRUCK-1_c2",
                "truck_id": "TRUCK-1",
                "tenant_id": "other-tenant",
                "capacity_liters": 5000.0,
                "allowed_grades": ["DIESEL_2"],
                "position_index": 1,
                "state": "loaded",
            }
        )
        app, _ = _build_app(es=es)
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/trucks/TRUCK-1/compartments")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["compartment_id"] == "TRUCK-1_c1"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestListTruckCompartmentsErrors:
    def test_blank_truck_id_returns_400(self):
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/trucks/%20%20/compartments")
        assert resp.status_code == 400
        body = resp.json()
        detail = body.get("detail") or body
        assert detail["error_code"] == "invalid_truck_id"

    def test_es_failure_returns_500(self):
        es = _FakeESService()
        es.raise_on_search = RuntimeError("ES unavailable")
        app, _ = _build_app(es=es)
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/trucks/TRUCK-1/compartments")
        assert resp.status_code == 500
        body = resp.json()
        detail = body.get("detail") or body
        assert detail["error_code"] == "truck_compartments_lookup_failed"

    def test_malformed_row_is_dropped_not_500(self):
        """A single malformed row must not fail the whole request — the
        router logs it and keeps the remaining valid rows."""

        es = _FakeESService()
        # Valid row.
        es.seed(
            {
                "compartment_id": "TRUCK-1_c1",
                "truck_id": "TRUCK-1",
                "tenant_id": "tenant-1",
                "capacity_liters": 5000.0,
                "allowed_grades": ["DIESEL_2"],
                "position_index": 0,
                "state": "clean",
            }
        )
        # Malformed row: capacity is a non-numeric string the row-level
        # validator cannot coerce. The router must drop and keep going.
        es.seed(
            {
                "compartment_id": "TRUCK-1_c2",
                "truck_id": "TRUCK-1",
                "tenant_id": "tenant-1",
                "capacity_liters": "not-a-number",
                "allowed_grades": ["DIESEL_2"],
                "position_index": 1,
                "state": "clean",
            }
        )
        app, _ = _build_app(es=es)
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/trucks/TRUCK-1/compartments")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["compartment_id"] == "TRUCK-1_c1"
