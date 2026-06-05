"""
Unit tests for ``GET /api/fuel/mvp/compartment-trucks``.

This endpoint powers the Truck Compartments tab's truck picker: it lists
every truck that has at least one compartment configured, with a per-truck
compartment count, using a tenant-scoped ``terms`` aggregation on
``truck_id`` over the ``truck_compartments`` index. Without it the tab had
no way to discover which tankers (e.g. TNK-001/TNK-002) actually carry
compartments, so it always opened empty.

Tests run the router against an in-memory ES stub so they never touch the
real Elasticsearch backend.
"""
from __future__ import annotations

import unittest.mock as mock
from typing import Any, Dict, List

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
    """Async ES stub that returns a pre-seeded ``terms`` aggregation.

    Records every query so assertions can verify the tenant filter and the
    aggregation shape. ``buckets`` is the list of
    ``{"key": truck_id, "doc_count": n}`` dicts the endpoint expects under
    ``aggregations.trucks.buckets``.
    """

    def __init__(self) -> None:
        self.buckets: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self.raise_on_search: BaseException | None = None

    def seed_bucket(self, truck_id: str, doc_count: int) -> None:
        self.buckets.append({"key": truck_id, "doc_count": doc_count})

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        self.calls.append({"index": index, "query": query, "size": size})
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return {
            "hits": {"hits": []},
            "aggregations": {"trucks": {"buckets": list(self.buckets)}},
        }


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


class TestListCompartmentTrucksSuccess:
    def test_returns_trucks_sorted_by_id_with_counts(self):
        es = _FakeESService()
        # Seed out of order so the router must sort by truck_id.
        es.seed_bucket("TNK-002", 5)
        es.seed_bucket("TNK-001", 4)
        app, _ = _build_app(es=es)
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/compartment-trucks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["items"] == [
            {"truck_id": "TNK-001", "compartment_count": 4},
            {"truck_id": "TNK-002", "compartment_count": 5},
        ]

    def test_sends_tenant_filter_and_terms_aggregation(self):
        es = _FakeESService()
        app, _ = _build_app(es=es)
        client = TestClient(app)
        client.get("/api/fuel/mvp/compartment-trucks")

        assert len(es.calls) == 1
        query = es.calls[0]["query"]
        must = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": "tenant-1"}} in must
        # size:0 because we only want the aggregation buckets, not hits.
        assert query["size"] == 0
        assert query["aggs"]["trucks"]["terms"]["field"] == "truck_id"

    def test_empty_result_returns_empty_list_not_404(self):
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/compartment-trucks")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0}

    def test_drops_buckets_with_blank_key(self):
        es = _FakeESService()
        es.seed_bucket("TNK-001", 4)
        es.seed_bucket("", 9)  # malformed bucket → dropped
        app, _ = _build_app(es=es)
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/compartment-trucks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["truck_id"] == "TNK-001"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestListCompartmentTrucksErrors:
    def test_es_failure_returns_500(self):
        es = _FakeESService()
        es.raise_on_search = RuntimeError("ES unavailable")
        app, _ = _build_app(es=es)
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/compartment-trucks")
        assert resp.status_code == 500
        body = resp.json()
        detail = body.get("detail") or body
        assert detail["error_code"] == "compartment_trucks_lookup_failed"
