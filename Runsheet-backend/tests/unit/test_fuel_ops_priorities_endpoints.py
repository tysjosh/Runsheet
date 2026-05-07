"""
Unit tests for Task 5.6 of the fuel-ops-hardening spec:

* ``GET /api/fuel/mvp/priorities`` — migrated from the legacy
  :mod:`Agents.support.mvp_endpoints` router to
  :mod:`fuel.api.fuel_ops_endpoints` so it shares the JWT-backed tenant
  context used by the rest of the fuel-ops surface. The endpoint now
  accepts a ``safe_to_delay_bucket`` query filter and returns the
  Capability-3 extensions (safe_to_delay_days, business_impact_score,
  business_impact_reasons, cluster_id, cluster_size) verbatim.

* ``GET /api/fuel/mvp/combinable-groups`` — a new endpoint that serves
  the ``mvp_combinable_groups`` index with ``run_id``, ``fuel_grade``,
  and ``min_members`` filters, scoped to the caller's tenant via the
  :class:`CombinableGroupRepository`.

The tests use fake in-memory ES services so they exercise the full
router + repository wiring without depending on the real Elasticsearch
backend.

Validates: Requirements 3.1.4, 3.2.4.
"""
from __future__ import annotations

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
from fuel.combinable_group_models import (
    CombinableGroup,
    CombinableGroupMember,
    CombinableGroupRepository,
)
from fuel.services.fuel_ops_es_mappings import MVP_COMBINABLE_GROUPS_INDEX
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeES:
    """Tiny async ES stub used by the priorities + combinable-groups endpoints.

    Supports the subset of :class:`ElasticsearchService` the endpoints
    actually call: ``search_documents`` and ``index_document``. Queries
    honour ``tenant_id`` term clauses, nested ``safe_to_delay_bucket``
    filters, and top-level equality filters on ``run_id`` and
    ``fuel_grades`` array containment so the tests can exercise the
    filter paths without depending on a real ES cluster.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.search_calls: List[Dict[str, Any]] = []

    async def index_document(
        self, index: str, doc_id: str, doc: Dict[str, Any]
    ) -> None:
        self.docs[doc_id] = dict(doc)

    async def delete_document(self, index: str, doc_id: str) -> bool:
        return self.docs.pop(doc_id, None) is not None

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        self.search_calls.append({"index": index, "query": query, "size": size})
        must = query.get("query", {}).get("bool", {}).get("must", [])

        def matches(doc: Dict[str, Any]) -> bool:
            for clause in must:
                if "term" in clause:
                    for field, expected in clause["term"].items():
                        actual = doc.get(field)
                        if isinstance(actual, list):
                            if expected not in actual:
                                return False
                        elif actual != expected:
                            return False
                elif "nested" in clause:
                    path = clause["nested"]["path"]
                    inner = clause["nested"]["query"].get("term", {})
                    nested_entries = doc.get(path) or []
                    field_to_check = next(iter(inner))
                    # nested path is ``priorities.safe_to_delay_bucket`` etc.
                    nested_field = field_to_check.split(".", 1)[1]
                    expected_value = inner[field_to_check]
                    if not any(
                        entry.get(nested_field) == expected_value
                        for entry in nested_entries
                        if isinstance(entry, dict)
                    ):
                        return False
            return True

        matched = [doc for doc in self.docs.values() if matches(doc)]
        # Apply from/size pagination so the legacy mvp_delivery_priorities
        # list respects ``page * size`` offsets.
        start = query.get("from", 0)
        end = start + size
        page = matched[start:end]
        return {
            "hits": {
                "hits": [{"_source": dict(d)} for d in page],
                "total": {"value": len(matched)},
            }
        }


def _tenant_ctx_factory(tenant_id: str = "tenant-A"):
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


def _build_app(tenant_id: str = "tenant-A") -> tuple[FastAPI, _FakeES]:
    es = _FakeES()
    combinable_repo = CombinableGroupRepository(es_service=es)
    configure_fuel_ops_endpoints(
        es_service=es,
        combinable_group_repository=combinable_repo,
    )
    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(tenant_id)
    return app, es


def _priority_list_doc(
    *,
    tenant_id: str = "tenant-A",
    run_id: str = "run-1",
    priority_list_id: str = "pl-1",
    bucket_entries: Dict[str, int] | None = None,
    timestamp: str | None = None,
) -> Dict[str, Any]:
    """Build an ``mvp_delivery_priorities`` document for the tests.

    ``bucket_entries`` maps safe_to_delay_bucket → count so a single
    document can carry a mix of bucket values (matching the real agent's
    output shape).
    """

    bucket_entries = bucket_entries or {"none": 1}
    priorities: List[Dict[str, Any]] = []
    for bucket, count in bucket_entries.items():
        for i in range(count):
            priorities.append(
                {
                    "station_id": f"station-{bucket}-{i}",
                    "fuel_grade": "DIESEL_2",
                    "priority_score": 0.5,
                    "priority_bucket": "medium",
                    "reasons": [],
                    "safe_to_delay_days": 2,
                    "safe_to_delay_bucket": bucket,
                    "business_impact_score": 0.42,
                    "business_impact_reasons": ["dominant_component:annual_revenue_usd"],
                    "cluster_id": f"cluster-{i}",
                    "cluster_size": 3,
                }
            )
    return {
        "priority_list_id": priority_list_id,
        "priorities": priorities,
        "scoring_weights": {},
        "tenant_id": tenant_id,
        "run_id": run_id,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/priorities — migrated endpoint (Req 3.1.4)
# ---------------------------------------------------------------------------


class TestListPrioritiesEndpoint:
    def test_returns_paginated_priorities_for_tenant(self):
        app, es = _build_app(tenant_id="tenant-A")
        doc = _priority_list_doc(tenant_id="tenant-A", priority_list_id="pl-1")
        es.docs["pl-1"] = doc
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priorities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["has_next"] is False
        assert len(data["items"]) == 1
        item = data["items"][0]
        # Capability-3 extensions surface on each priority entry.
        entry = item["priorities"][0]
        for field in (
            "safe_to_delay_days",
            "safe_to_delay_bucket",
            "business_impact_score",
            "business_impact_reasons",
            "cluster_id",
            "cluster_size",
        ):
            assert field in entry

    def test_safe_to_delay_bucket_filter_narrows_matches(self):
        """Req 3.1.4 — safe_to_delay_bucket query filter must narrow
        results to priority lists that contain an entry in the bucket.
        """
        app, es = _build_app(tenant_id="tenant-A")
        es.docs["pl-1"] = _priority_list_doc(
            tenant_id="tenant-A",
            priority_list_id="pl-1",
            run_id="run-1",
            bucket_entries={"short": 2, "medium": 1},
        )
        es.docs["pl-2"] = _priority_list_doc(
            tenant_id="tenant-A",
            priority_list_id="pl-2",
            run_id="run-2",
            bucket_entries={"long": 3},
        )
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/priorities", params={"safe_to_delay_bucket": "short"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["priority_list_id"] == "pl-1"

    def test_safe_to_delay_bucket_rejects_unknown_bucket(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/priorities", params={"safe_to_delay_bucket": "forever"}
        )
        # FastAPI returns 422 for Literal-typed query params out of range.
        assert resp.status_code == 422

    def test_run_id_filter_lands_in_query(self):
        app, es = _build_app()
        es.docs["pl-1"] = _priority_list_doc(run_id="run-1", priority_list_id="pl-1")
        es.docs["pl-2"] = _priority_list_doc(run_id="run-2", priority_list_id="pl-2")
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/priorities", params={"run_id": "run-2"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["run_id"] == "run-2"

    def test_tenant_isolation(self):
        app, es = _build_app(tenant_id="tenant-A")
        es.docs["pl-1"] = _priority_list_doc(tenant_id="tenant-A")
        es.docs["pl-2"] = _priority_list_doc(
            tenant_id="tenant-B", priority_list_id="pl-2"
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priorities")
        assert resp.status_code == 200
        data = resp.json()
        # Only tenant-A's list should be returned.
        assert data["total"] == 1
        assert data["items"][0]["priority_list_id"] == "pl-1"

    def test_returns_empty_envelope_when_no_priorities(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priorities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/combinable-groups — new endpoint (Req 3.2.4)
# ---------------------------------------------------------------------------


def _make_group(
    *,
    group_id: str,
    tenant_id: str = "tenant-A",
    run_id: str = "run-1",
    member_ids: List[str] | None = None,
    fuel_grade: str = "DIESEL_2",
    centroid: Dict[str, float] | None = None,
) -> CombinableGroup:
    """Build a valid :class:`CombinableGroup` for persistence tests."""

    member_ids = member_ids or ["s1", "s2"]
    members = [
        CombinableGroupMember(
            destination_type="station",
            destination_id=mid,
            station_id=mid,
            fuel_grade=fuel_grade,
            product_code=fuel_grade,
            estimated_gallons=100.0,
            location={"lat": 40.0 + i * 0.001, "lon": -72.0 + i * 0.001},
        )
        for i, mid in enumerate(member_ids)
    ]
    return CombinableGroup(
        group_id=group_id,
        tenant_id=tenant_id,
        run_id=run_id,
        members=members,
        fuel_grades=[fuel_grade],
        estimated_combined_gallons=sum(m.estimated_gallons for m in members),
        centroid=centroid or {"lat": 40.0005, "lon": -71.9995},
    )


class TestListCombinableGroupsEndpoint:
    @pytest.mark.asyncio
    async def test_returns_paginated_groups_for_tenant(self):
        app, es = _build_app(tenant_id="tenant-A")
        repo = CombinableGroupRepository(es_service=es)
        await repo.persist_groups(
            "tenant-A",
            [
                _make_group(group_id="g1"),
                _make_group(group_id="g2", member_ids=["s3", "s4"]),
            ],
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/combinable-groups")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert data["has_next"] is False
        ids = {item["group_id"] for item in data["items"]}
        assert ids == {"g1", "g2"}

    @pytest.mark.asyncio
    async def test_filters_by_run_id(self):
        app, es = _build_app(tenant_id="tenant-A")
        repo = CombinableGroupRepository(es_service=es)
        await repo.persist_groups(
            "tenant-A",
            [
                _make_group(group_id="g1", run_id="run-A"),
                _make_group(group_id="g2", run_id="run-B"),
            ],
        )
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/combinable-groups", params={"run_id": "run-B"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["group_id"] == "g2"

    @pytest.mark.asyncio
    async def test_filters_by_fuel_grade_with_alias_canonicalization(self):
        app, es = _build_app(tenant_id="tenant-A")
        repo = CombinableGroupRepository(es_service=es)
        await repo.persist_groups(
            "tenant-A",
            [
                _make_group(group_id="g1", fuel_grade="DIESEL_2"),
                _make_group(group_id="g2", fuel_grade="PROPANE"),
            ],
        )
        client = TestClient(app)

        # Legacy alias "AGO" → canonical "DIESEL_2".
        resp = client.get(
            "/api/fuel/mvp/combinable-groups", params={"fuel_grade": "AGO"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["group_id"] == "g1"

    @pytest.mark.asyncio
    async def test_filters_by_min_members(self):
        app, es = _build_app(tenant_id="tenant-A")
        repo = CombinableGroupRepository(es_service=es)
        await repo.persist_groups(
            "tenant-A",
            [
                _make_group(group_id="small", member_ids=["s1", "s2"]),
                _make_group(
                    group_id="large", member_ids=["s3", "s4", "s5", "s6"]
                ),
            ],
        )
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/combinable-groups", params={"min_members": 3}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["group_id"] == "large"

    @pytest.mark.asyncio
    async def test_unknown_fuel_grade_returns_empty_without_hitting_es(self):
        app, es = _build_app(tenant_id="tenant-A")
        repo = CombinableGroupRepository(es_service=es)
        await repo.persist_groups("tenant-A", [_make_group(group_id="g1")])
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/combinable-groups",
            params={"fuel_grade": "NOT_A_REAL_PRODUCT"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_tenant_isolation(self):
        app, es = _build_app(tenant_id="tenant-A")
        repo = CombinableGroupRepository(es_service=es)
        await repo.persist_groups("tenant-A", [_make_group(group_id="gA")])
        await repo.persist_groups(
            "tenant-B",
            [_make_group(group_id="gB", tenant_id="tenant-B")],
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/combinable-groups")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["group_id"] == "gA"

    @pytest.mark.asyncio
    async def test_rejects_invalid_min_members(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/combinable-groups", params={"min_members": 1}
        )
        # min_members has Query(ge=2) so 1 is rejected by FastAPI.
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_uses_canonical_index_for_search(self):
        app, es = _build_app(tenant_id="tenant-A")
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/combinable-groups")
        assert resp.status_code == 200
        assert es.search_calls[-1]["index"] == MVP_COMBINABLE_GROUPS_INDEX

    @pytest.mark.asyncio
    async def test_pagination_reports_has_next(self):
        app, es = _build_app(tenant_id="tenant-A")
        repo = CombinableGroupRepository(es_service=es)
        groups = [
            _make_group(group_id=f"g{i}", member_ids=[f"s{i}a", f"s{i}b"])
            for i in range(3)
        ]
        await repo.persist_groups("tenant-A", groups)
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/combinable-groups", params={"size": 2, "page": 1}
        )
        assert resp.status_code == 200
        data = resp.json()
        # Window fetched = page*size+1 = 3, so has_next is True.
        assert data["has_next"] is True
        assert len(data["items"]) == 2
