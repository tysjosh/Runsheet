"""
Unit tests for the Priority_Cluster REST endpoint added by Task 5.4.

Covers the endpoint mounted under
:data:`fuel.api.fuel_ops_endpoints.mvp_router`:

* ``GET /api/fuel/mvp/priority-clusters`` — returns DBSCAN clusters over
  the tenant's most recent priority list.

Validates: Requirements 3.4.1, 3.4.2, 3.4.3, 3.4.4.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

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
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-1", region: str = "US"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["dispatcher"],
            region=region,
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_es_mock(
    *,
    priority_lists: List[Dict[str, Any]],
    stations: List[Dict[str, Any]],
    tanks: List[Dict[str, Any]],
) -> MagicMock:
    """Build a recording ES mock.

    * ``mvp_delivery_priorities`` → returns the priority-list document
      sorted desc by timestamp (the first entry in ``priority_lists``).
    * ``fuel_stations`` → returns the station docs.
    * ``customer_tanks`` → returns the tank docs.
    """

    es = MagicMock()

    async def _search(index: str, query: Dict[str, Any], size: int) -> Dict[str, Any]:
        if index == "mvp_delivery_priorities":
            hits = [
                {"_source": dict(doc)}
                for doc in priority_lists
                if doc.get("tenant_id") == _tenant_from_query(query)
            ][: size or 1]
            return {
                "hits": {
                    "hits": hits,
                    "total": {"value": len(hits)},
                }
            }
        if index == "fuel_stations":
            return {
                "hits": {
                    "hits": [{"_source": dict(doc)} for doc in stations],
                    "total": {"value": len(stations)},
                }
            }
        if index == "customer_tanks":
            return {
                "hits": {
                    "hits": [{"_source": dict(doc)} for doc in tanks],
                    "total": {"value": len(tanks)},
                }
            }
        return {"hits": {"hits": [], "total": {"value": 0}}}

    es.search_documents = AsyncMock(side_effect=_search)
    return es


def _tenant_from_query(query: Dict[str, Any]) -> str:
    for clause in query.get("query", {}).get("bool", {}).get("must", []):
        term = clause.get("term") or {}
        if "tenant_id" in term:
            return term["tenant_id"]
    return ""


def _priority_list_doc(
    *,
    tenant_id: str = "tenant-1",
    priorities: List[Dict[str, Any]] | None = None,
    run_id: str = "run-1",
    timestamp: datetime | None = None,
) -> Dict[str, Any]:
    return {
        "priority_list_id": "pl-1",
        "priorities": priorities or [],
        "scoring_weights": {},
        "tenant_id": tenant_id,
        "run_id": run_id,
        "timestamp": (
            (timestamp or datetime.now(timezone.utc)).isoformat()
        ),
    }


def _station_doc(
    *, station_id: str, lat: float, lon: float, tenant_id: str = "tenant-1"
) -> Dict[str, Any]:
    return {
        "station_id": station_id,
        "tenant_id": tenant_id,
        "name": f"Station {station_id}",
        "location_lat": lat,
        "location_lon": lon,
        "fuel_type": "DIESEL_2",
    }


def _tank_doc(
    *,
    tank_id: str,
    lat: float,
    lon: float,
    tenant_id: str = "tenant-1",
) -> Dict[str, Any]:
    return {
        "customer_tank_id": tank_id,
        "tenant_id": tenant_id,
        "customer_id": "cust-1",
        "customer_type": "residential",
        "fuel_type": "propane",
        "fuel_product_code": "PROPANE",
        "capacity_gallons": 500.0,
        "current_level_gallons": 100.0,
        "location_lat": lat,
        "location_lon": lon,
        "zip_code": "06010",
        "status": "active",
    }


def _build_app(
    *,
    tenant_id: str = "tenant-1",
    priority_lists: List[Dict[str, Any]] | None = None,
    stations: List[Dict[str, Any]] | None = None,
    tanks: List[Dict[str, Any]] | None = None,
) -> tuple[FastAPI, MagicMock]:
    es = _build_es_mock(
        priority_lists=priority_lists or [],
        stations=stations or [],
        tanks=tanks or [],
    )
    configure_fuel_ops_endpoints(es_service=es)

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPriorityClustersEndpoint:
    def test_returns_empty_when_no_priorities_yet(self):
        """Fresh tenant with no priority_list document gets an empty list."""
        app, _ = _build_app(priority_lists=[])
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priority-clusters")

        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["run_id"] is None
        assert data["eps_miles"] == 3.0
        assert data["min_samples"] == 2

    def test_clusters_nearby_priorities(self):
        """Three priorities within 1 mile cluster into a single cluster (Req 3.4.3)."""
        stations = [
            _station_doc(station_id="s1", lat=40.000, lon=-72.000),
            _station_doc(station_id="s2", lat=40.001, lon=-72.001),
            _station_doc(station_id="s3", lat=40.002, lon=-72.002),
        ]
        priority_list = _priority_list_doc(
            priorities=[
                {
                    "station_id": "s1",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.9,
                    "priority_bucket": "critical",
                    "reasons": [],
                },
                {
                    "station_id": "s2",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.7,
                    "priority_bucket": "high",
                    "reasons": [],
                },
                {
                    "station_id": "s3",
                    "fuel_grade": "HEATING_OIL",
                    "priority_score": 0.5,
                    "priority_bucket": "medium",
                    "reasons": [],
                },
            ],
        )
        app, _ = _build_app(
            priority_lists=[priority_list], stations=stations
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priority-clusters")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        cluster = data["items"][0]
        assert cluster["cluster_id"] == "cluster_0"
        assert cluster["member_count"] == 3
        # Req 3.4.3: highest-priority bucket is "critical".
        assert cluster["highest_priority_bucket"] == "critical"
        # Req 3.4.3: fuel grades present (deduplicated + sorted).
        assert cluster["fuel_grades"] == ["DIESEL_2", "HEATING_OIL"]
        # Centroid is the arithmetic mean of the three points.
        assert cluster["centroid"]["lat"] == pytest.approx(40.001, abs=1e-6)
        assert cluster["centroid"]["lon"] == pytest.approx(-72.001, abs=1e-6)

    def test_far_apart_priorities_produce_noise_excluded_from_response(self):
        """Req 3.4.4: noise points are excluded; only dense clusters surface."""
        stations = [
            _station_doc(station_id="s1", lat=40.0, lon=-72.0),
            _station_doc(station_id="s2", lat=45.0, lon=-80.0),
        ]
        priority_list = _priority_list_doc(
            priorities=[
                {
                    "station_id": "s1",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.9,
                    "priority_bucket": "critical",
                    "reasons": [],
                },
                {
                    "station_id": "s2",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.7,
                    "priority_bucket": "high",
                    "reasons": [],
                },
            ],
        )
        app, _ = _build_app(
            priority_lists=[priority_list], stations=stations
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priority-clusters")

        assert resp.status_code == 200
        data = resp.json()
        # Both points are isolated noise — no dense clusters.
        assert data["total"] == 0
        assert data["items"] == []
        assert data["run_id"] == "run-1"

    def test_customer_tank_priorities_resolve_location_via_customer_tank_id(self):
        """Priorities referencing customer_tank_id use the tanks index."""
        tanks = [
            _tank_doc(tank_id="tank-a", lat=40.000, lon=-72.000),
            _tank_doc(tank_id="tank-b", lat=40.001, lon=-72.001),
        ]
        priority_list = _priority_list_doc(
            priorities=[
                {
                    "customer_tank_id": "tank-a",
                    "station_id": "",
                    "fuel_grade": "PROPANE",
                    "priority_score": 0.8,
                    "priority_bucket": "high",
                    "reasons": [],
                },
                {
                    "customer_tank_id": "tank-b",
                    "station_id": "",
                    "fuel_grade": "PROPANE",
                    "priority_score": 0.75,
                    "priority_bucket": "high",
                    "reasons": [],
                },
            ],
        )
        app, _ = _build_app(priority_lists=[priority_list], tanks=tanks)
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priority-clusters")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["member_count"] == 2
        assert data["items"][0]["fuel_grades"] == ["PROPANE"]

    def test_custom_eps_and_min_samples_are_forwarded(self):
        """Req 3.4.1: caller-supplied eps_miles/min_samples override defaults."""
        stations = [
            _station_doc(station_id="s1", lat=40.000, lon=-72.000),
            _station_doc(station_id="s2", lat=40.100, lon=-72.000),  # ~6.9 miles
        ]
        priority_list = _priority_list_doc(
            priorities=[
                {
                    "station_id": "s1",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.9,
                    "priority_bucket": "critical",
                    "reasons": [],
                },
                {
                    "station_id": "s2",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.85,
                    "priority_bucket": "critical",
                    "reasons": [],
                },
            ],
        )
        app, _ = _build_app(
            priority_lists=[priority_list], stations=stations
        )
        client = TestClient(app)

        # Default eps (3.0) → points 6.9 miles apart are noise; no clusters.
        resp_default = client.get("/api/fuel/mvp/priority-clusters")
        assert resp_default.status_code == 200
        assert resp_default.json()["total"] == 0

        # eps=10 miles → points 6.9 miles apart cluster together.
        resp_wide = client.get(
            "/api/fuel/mvp/priority-clusters?eps_miles=10.0"
        )
        assert resp_wide.status_code == 200
        data = resp_wide.json()
        assert data["eps_miles"] == 10.0
        assert data["total"] == 1

    def test_rejects_non_positive_eps_via_query_validation(self):
        """Pydantic/FastAPI Query validator rejects eps_miles <= 0."""
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/priority-clusters?eps_miles=0")
        assert resp.status_code == 422

    def test_rejects_min_samples_below_one(self):
        app, _ = _build_app()
        client = TestClient(app)
        resp = client.get("/api/fuel/mvp/priority-clusters?min_samples=0")
        assert resp.status_code == 422

    def test_skips_priorities_whose_destination_has_no_location(self):
        """A priority that references an unknown station is skipped, not fatal."""
        stations = [
            _station_doc(station_id="s1", lat=40.000, lon=-72.000),
            _station_doc(station_id="s2", lat=40.001, lon=-72.001),
        ]
        priority_list = _priority_list_doc(
            priorities=[
                {
                    "station_id": "s1",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.9,
                    "priority_bucket": "critical",
                    "reasons": [],
                },
                {
                    "station_id": "missing-station",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.7,
                    "priority_bucket": "high",
                    "reasons": [],
                },
                {
                    "station_id": "s2",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.8,
                    "priority_bucket": "critical",
                    "reasons": [],
                },
            ],
        )
        app, _ = _build_app(
            priority_lists=[priority_list], stations=stations
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priority-clusters")
        assert resp.status_code == 200
        data = resp.json()
        # The cluster formed from s1+s2 remains (member_count=2).
        assert data["total"] == 1
        assert data["items"][0]["member_count"] == 2
