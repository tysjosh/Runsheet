"""
Regression tests for B11/S5 (decision D9): ``/fleet/trucks`` and the truck
counts in ``/fleet/summary``.

* "Trucks" means every vehicle: ``asset_type == "vehicle"`` (truck,
  fuel_truck, personnel_vehicle) or a legacy doc with no ``asset_type``.
  Vessels and equipment are not trucks. ``/fleet/trucks`` used to keep only
  ``asset_subtype == "truck"``, so fuel trucks were missing; the summary used
  to count every asset (barges included) as a truck.
* ``activeTrucks`` counts statuses in ``{"active", "in_transit", "on_time",
  "delayed"}``. It used to count only ``on_time``/``delayed``.
* ``averageDelay`` is the mean ``delay_duration_minutes`` over the tenant's
  delayed jobs (``job_metrics_aggregator.delay_metrics``), 0.0 with none. It
  used to be hard-coded to 45.

Both the Elasticsearch-facade branch and the Postgres read-cutover branch are
covered.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

TENANT = "test-tenant"


def _asset(asset_id: str, asset_type, asset_subtype, status: str, **extra) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "truck_id": asset_id,
        "asset_id": asset_id,
        "status": status,
        "tenant_id": TENANT,
        "current_location": {},
        "destination": {},
        "route": {},
        "created_at": f"2025-01-01T00:00:0{len(asset_id) % 10}",
    }
    if asset_type is not None:
        doc["asset_type"] = asset_type
    if asset_subtype is not None:
        doc["asset_subtype"] = asset_subtype
    doc.update(extra)
    return doc


# A mixed fleet: four vehicles (one legacy) plus a barge and a crane.
FLEET: List[Dict[str, Any]] = [
    _asset("FT-1", "vehicle", "fuel_truck", "active"),
    _asset("TR-1", "vehicle", "truck", "in_transit"),
    _asset("PV-1", "vehicle", "personnel_vehicle", "maintenance"),
    _asset("LEGACY-1", None, None, "on_time"),
    _asset("BARGE-1", "vessel", "barge", "active"),
    _asset("CRANE-1", "equipment", "crane", "active"),
]
VEHICLE_IDS = {"FT-1", "TR-1", "PV-1", "LEGACY-1"}

DELAYED_JOBS = [
    {"job_id": "J-1", "delayed": True, "delay_duration_minutes": 10, "job_type": "fuel_delivery",
     "tenant_id": TENANT},
    {"job_id": "J-2", "delayed": True, "delay_duration_minutes": 30, "job_type": "fuel_delivery",
     "tenant_id": TENANT},
]


def _hits(docs):
    return {"hits": {"hits": [{"_source": d} for d in docs], "total": {"value": len(docs)}}}


def _agg_response(docs):
    return {
        "hits": {"total": {"value": len(docs)}, "hits": []},
        "aggregations": {
            "by_type": {"buckets": []},
            "by_subtype": {"buckets": []},
            "active_count": {"doc_count": 0},
            "delayed_count": {"doc_count": 0},
        },
    }


class _FakeEs:
    """Routes ``search_documents`` by index and records every call."""

    def __init__(self, assets, jobs) -> None:
        self.assets = assets
        self.jobs = jobs
        self.calls: List[tuple] = []

    async def search_documents(self, index, query, size=100, **kw):
        self.calls.append((index, query))
        if index == "jobs_current":
            return _hits(self.jobs)
        if "aggs" in query:
            return _agg_response(self.assets)
        return _hits(self.assets)


@pytest.fixture
def make_client():
    from main import app
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    async def _override_tenant():
        return TenantContext(tenant_id=TENANT, user_id="test-user", has_pii_access=False)

    app.dependency_overrides[get_tenant_context] = _override_tenant
    patches: List[Any] = []

    def _make(fake_es: _FakeEs):
        p = patch("data_endpoints.elasticsearch_service", fake_es)
        p.start()
        patches.append(p)
        # Not a context manager: entering it would run main's lifespan.
        return TestClient(app)

    try:
        yield _make
    finally:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(get_tenant_context, None)


def _pg_cutover(assets, jobs, calls: List[tuple]):
    """Patch the read-cutover fetch so both aggregates come from 'Postgres'."""

    async def _fetch(aggregate_type, tenant_id, **kw):
        calls.append((aggregate_type, tenant_id, kw))
        if aggregate_type == "truck":
            return list(assets)
        if aggregate_type == "job":
            return [j for j in jobs if j.get("delayed") is True]
        raise AssertionError(f"unexpected aggregate {aggregate_type}")

    return patch(
        "commerce.services.commerce_persistence_bridge.read_hybrid_fetch_for_aggregation",
        _fetch,
    )


# ---------------------------------------------------------------------------
# /fleet/trucks
# ---------------------------------------------------------------------------


def test_trucks_es_query_selects_every_vehicle(make_client):
    fake = _FakeEs(FLEET, [])
    client = make_client(fake)

    resp = client.get("/api/fleet/trucks")

    assert resp.status_code == 200, resp.text
    index, query = fake.calls[0]
    assert index == "trucks"
    inner = query["query"]["bool"]["must"][0]
    should = inner["bool"]["should"]
    assert {"term": {"asset_type": "vehicle"}} in should
    assert {"bool": {"must_not": {"exists": {"field": "asset_type"}}}} in should
    assert {"term": {"asset_subtype": "truck"}} not in should
    # Whatever the store returns, only vehicles are listed.
    assert {t["id"] for t in resp.json()["data"]} == VEHICLE_IDS


def test_trucks_pg_cutover_lists_every_vehicle(make_client):
    other_tenant = _asset("FT-OTHER", "vehicle", "fuel_truck", "active", tenant_id="tenant-b")
    calls: List[tuple] = []
    client = make_client(_FakeEs([], []))
    with _pg_cutover(FLEET + [other_tenant], [], calls):
        resp = client.get("/api/fleet/trucks")

    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["data"]}
    assert ids == VEHICLE_IDS  # fuel_truck + personnel_vehicle in; barge, crane, tenant B out


# ---------------------------------------------------------------------------
# /fleet/summary
# ---------------------------------------------------------------------------


def _assert_truck_counts(data):
    assert data["totalTrucks"] == 4  # barge and crane are not trucks
    assert data["activeTrucks"] == 3  # active + in_transit + on_time; maintenance excluded
    assert data["onTimeTrucks"] == 1
    assert data["delayedTrucks"] == 0


def test_summary_es_counts_vehicles_and_real_average_delay(make_client):
    fake = _FakeEs(FLEET, DELAYED_JOBS)
    client = make_client(fake)

    resp = client.get("/api/fleet/summary")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    _assert_truck_counts(data)
    assert data["averageDelay"] == pytest.approx(20.0)

    job_calls = [q for i, q in fake.calls if i == "jobs_current"]
    assert len(job_calls) == 1
    job_query = job_calls[0]
    assert {"term": {"delayed": True}} in job_query["query"]["bool"]["must"]
    assert {"term": {"tenant_id": TENANT}} in job_query["query"]["bool"]["filter"]


def test_summary_es_average_delay_is_zero_without_delays(make_client):
    client = make_client(_FakeEs(FLEET, []))

    data = client.get("/api/fleet/summary").json()["data"]

    assert data["averageDelay"] == 0.0


def test_summary_es_active_asset_filter_uses_the_shared_statuses(make_client):
    fake = _FakeEs(FLEET, [])
    client = make_client(fake)

    client.get("/api/fleet/summary")

    agg_query = next(q for i, q in fake.calls if "aggs" in q)
    statuses = agg_query["aggs"]["active_count"]["filter"]["terms"]["status"]
    assert set(statuses) == {"active", "in_transit", "on_time", "delayed"}


def test_summary_pg_cutover_counts_vehicles_and_real_average_delay(make_client):
    calls: List[tuple] = []
    other_job = {"job_id": "J-B", "delayed": True, "delay_duration_minutes": 999,
                 "tenant_id": "tenant-b"}
    client = make_client(_FakeEs([], []))
    with _pg_cutover(FLEET, DELAYED_JOBS + [other_job], calls):
        resp = client.get("/api/fleet/summary")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    _assert_truck_counts(data)
    assert data["averageDelay"] == pytest.approx(20.0)  # tenant B's job ignored
    assert data["totalAssets"] == 6
    assert data["activeAssets"] == 5  # every asset except the maintenance vehicle

    job_calls = [c for c in calls if c[0] == "job"]
    assert job_calls and job_calls[0][1] == TENANT
    assert job_calls[0][2].get("bool_filters") == {"delayed": True}


def test_summary_pg_cutover_average_delay_is_zero_without_delays(make_client):
    client = make_client(_FakeEs([], []))
    with _pg_cutover(FLEET, [], []):
        data = client.get("/api/fleet/summary").json()["data"]

    assert data["averageDelay"] == 0.0
