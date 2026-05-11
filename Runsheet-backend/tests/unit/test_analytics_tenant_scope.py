"""
Regression tests for tenant scoping on the analytics and semantic-search
endpoints on ``data_endpoints.py``.

These tests boot a minimal FastAPI app with the ``data_endpoints.router``
mounted, override ``get_tenant_context`` to return a deterministic tenant,
and install a thin fake ES service that captures every request body.
For each active ``/api/analytics/*`` and ``/api/search`` endpoint we assert the
captured query body contains ``{"term": {"tenant_id": <tenant>}}`` in the
top-level ``bool.filter`` clause, and that swapping the tenant context
changes the embedded tenant_id accordingly. This guards against future
regressions that forget to plumb the tenant id into the ES layer.

Validates: Requirements 9.2, 9.4 (tenant scoping on ES reads).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """In-memory capture of every ES call made by the analytics endpoints.

    Implements the analytics + semantic-search surface in a way that mirrors
    the real ``ElasticsearchService`` (tenant filter in the top-level
    ``bool.filter``) so the captured bodies match the production shape.
    Every call records ``(method, index, body)`` in ``self.calls``.

    We don't bind the real methods at runtime because another test module
    may have already replaced ``services.elasticsearch_service`` with a
    MagicMock before this file is loaded. Re-implementing the shapes here
    keeps this test file self-contained.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int = 100
    ) -> Dict[str, Any]:
        self.calls.append(("search_documents", index, dict(query)))
        return {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "time_series": {"buckets": []},
                "routes": {"buckets": []},
                "causes": {"buckets": []},
                "regions": {"buckets": []},
            },
        }

    # ---- analytics helpers (shape mirrors ElasticsearchService) ----

    async def semantic_search(
        self, tenant_id: str, index: str, text: str, fields: List[str], size: int = 10
    ) -> List[Dict[str, Any]]:
        query = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": text,
                                "fields": fields,
                                "type": "best_fields",
                            }
                        }
                    ],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            }
        }
        resp = await self.search_documents(index, query, size)
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    async def get_current_metrics(self, tenant_id: str) -> Dict[str, Any]:
        query = {
            "query": {
                "bool": {
                    "must": [{"term": {"event_type": "daily_performance"}}],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": 1,
        }
        await self.search_documents("analytics_events", query)
        return {
            "delivery_performance": {"title": "x", "value": "0%", "change": "0", "trend": "up"},
        }

    async def get_route_performance_data(self, tenant_id: str) -> List[Dict[str, Any]]:
        query = {
            "query": {
                "bool": {
                    "must": [{"term": {"event_type": "route_performance"}}],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "aggs": {},
            "size": 0,
        }
        await self.search_documents("analytics_events", query)
        return []

    async def get_delay_causes_data(self, tenant_id: str) -> List[Dict[str, Any]]:
        query = {
            "query": {
                "bool": {
                    "must": [{"term": {"event_type": "delay_cause_analysis"}}],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "aggs": {},
            "size": 0,
        }
        await self.search_documents("analytics_events", query)
        return []

    async def get_regional_performance_data(self, tenant_id: str) -> List[Dict[str, Any]]:
        query = {
            "query": {
                "bool": {
                    "must": [{"term": {"event_type": "regional_performance"}}],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "aggs": {},
            "size": 0,
        }
        await self.search_documents("analytics_events", query)
        return []

    async def get_time_series_data(
        self, tenant_id: str, event_type: str, metric_field: str, time_range: str = "7d"
    ) -> List[Dict[str, Any]]:
        query = {
            "query": {
                "bool": {
                    "must": [{"term": {"event_type": event_type}}],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                }
            },
            "aggs": {},
            "size": 0,
        }
        await self.search_documents("analytics_events", query)
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_tenant_filter(body: Dict[str, Any], tenant_id: str) -> bool:
    """Return True iff ``body`` carries ``{"term": {"tenant_id": tenant_id}}``
    in its top-level ``bool.filter`` clause."""
    bool_clause = body.get("query", {}).get("bool")
    if not bool_clause:
        return False
    for entry in bool_clause.get("filter", []):
        if entry.get("term", {}).get("tenant_id") == tenant_id:
            return True
    return False


def _collect_bodies(fake: _FakeESService) -> List[Dict[str, Any]]:
    return [body for _, _, body in fake.calls]


def _build_app(tenant_id: str) -> Tuple[FastAPI, _FakeESService]:
    """Build a minimal FastAPI app wired to the data_endpoints router with a
    fake ES service and tenant guard override."""
    # Import lazily so the bootstrap-level elasticsearch_service initialisation
    # does not run when this test module is collected in isolation.
    import data_endpoints
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    fake_es = _FakeESService()
    # Route every ES call through the fake without mutating the global.
    data_endpoints.elasticsearch_service = fake_es

    async def _override_tenant() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-a",
            has_pii_access=False,
            roles=["dispatcher"],
        )

    app = FastAPI()
    app.include_router(data_endpoints.router)
    app.dependency_overrides[get_tenant_context] = _override_tenant
    return app, fake_es


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/analytics/metrics",
        "/api/analytics/routes",
    ],
)
def test_analytics_endpoints_emit_tenant_filter(path: str) -> None:
    """Every analytics endpoint must emit an ES query scoped to the caller's tenant."""
    tenant_id = "tenant-a"
    app, fake_es = _build_app(tenant_id=tenant_id)

    with TestClient(app) as client:
        resp = client.get(path)

    assert resp.status_code == 200, resp.text
    bodies = _collect_bodies(fake_es)
    assert bodies, f"{path} did not call elasticsearch_service"
    assert all(
        _find_tenant_filter(body, tenant_id) for body in bodies
    ), f"{path} emitted an ES query without tenant filter: {bodies}"


def test_analytics_endpoints_respect_tenant_switch() -> None:
    """Swapping the tenant context swaps the embedded tenant_id term."""
    # Build each app fresh inside its own ``with`` block so the module-level
    # ``data_endpoints.elasticsearch_service`` pointer is re-assigned to the
    # matching fake immediately before the request runs.
    app_a, fake_a = _build_app(tenant_id="tenant-a")
    with TestClient(app_a) as client:
        assert client.get("/api/analytics/metrics").status_code == 200

    app_b, fake_b = _build_app(tenant_id="tenant-b")
    with TestClient(app_b) as client:
        assert client.get("/api/analytics/metrics").status_code == 200

    assert all(_find_tenant_filter(body, "tenant-a") for body in _collect_bodies(fake_a))
    assert all(_find_tenant_filter(body, "tenant-b") for body in _collect_bodies(fake_b))
    # Guard against leakage: tenant-a's captured bodies never carry tenant-b's id.
    assert not any(_find_tenant_filter(body, "tenant-b") for body in _collect_bodies(fake_a))
    assert not any(_find_tenant_filter(body, "tenant-a") for body in _collect_bodies(fake_b))


def test_static_analytics_mock_routes_are_not_registered() -> None:
    """The retired static/mock analytics routes should stay off the router."""
    import data_endpoints

    paths = {route.path for route in data_endpoints.router.routes}
    assert "/api/analytics/delay-causes" not in paths
    assert "/api/analytics/regional" not in paths
    assert "/api/analytics/time-series" not in paths


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", ["trucks", "support_tickets"])
def test_semantic_search_emits_tenant_filter(index: str) -> None:
    """GET /api/search runs through ``semantic_search`` which must scope its
    multi_match query to the caller's tenant."""
    tenant_id = "tenant-a"
    app, fake_es = _build_app(tenant_id=tenant_id)

    with TestClient(app) as client:
        resp = client.get("/api/search", params={"q": "broken headlights", "index": index})

    assert resp.status_code == 200, resp.text
    bodies = _collect_bodies(fake_es)
    assert bodies, f"/api/search?index={index} did not reach ES"
    body = bodies[0]
    # ``semantic_search`` wraps the multi_match inside a bool with a filter
    # on tenant_id, matching the inject_tenant_filter shape.
    assert _find_tenant_filter(body, tenant_id), (
        f"/api/search?index={index} did not scope query to tenant: {body}"
    )
    # The user's query text must still reach ES.
    must = body["query"]["bool"].get("must", [])
    assert any(
        isinstance(clause, dict)
        and "multi_match" in clause
        and clause["multi_match"].get("query") == "broken headlights"
        for clause in must
    ), f"semantic_search lost the user query: {body}"
