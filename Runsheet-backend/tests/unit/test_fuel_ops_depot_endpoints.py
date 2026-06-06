"""
Unit tests for the Depot CRUD REST endpoints added by Task 4.3.

Covers the endpoints mounted under :data:`fuel.api.fuel_ops_endpoints.mvp_router`:

* ``GET    /api/fuel/mvp/depots``              — tenant-scoped list
* ``POST   /api/fuel/mvp/depots``              — create
* ``PATCH  /api/fuel/mvp/depots/{depot_id}``   — partial update
* ``DELETE /api/fuel/mvp/depots/{depot_id}``   — delete

Each test uses :class:`_FakeESService` — a minimal in-memory stub that
implements the subset of :class:`ElasticsearchService` the repository
relies on. This keeps the endpoint tests decoupled from the real
Elasticsearch backend while still exercising the full
``DepotRepository`` + router wiring (the same pattern used by
``test_fuel_ops_customer_tank_endpoints``).

Validates: Requirements 2.2.2.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
from fuel.depot_models import DepotRepository
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub used by :class:`DepotRepository`.

    Implements ``index_document``, ``search_documents``,
    ``update_document``, and ``delete_document`` over an in-memory dict
    keyed by ``doc_id``. Queries with a ``term`` on ``depot_id`` resolve
    to a single-document lookup; queries with ``bool.must`` on
    ``tenant_id`` and optional equality filters (``status``,
    ``fuel_types_supported``) are honoured so the endpoint-level
    ``list_for_tenant`` filters exercise the real code paths.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}

    # -------- index_document ---------------------------------------------
    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.docs[doc_id] = dict(document)

    # -------- search_documents -------------------------------------------
    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        must = query.get("query", {}).get("bool", {}).get("must", [])
        tenant_id: str | None = None
        equality: Dict[str, Any] = {}
        fuel_type_filter: str | None = None
        id_lookup: str | None = None

        for clause in must:
            term = clause.get("term") if isinstance(clause, dict) else None
            if not term:
                continue
            for field, value in term.items():
                if field == "tenant_id":
                    tenant_id = value
                elif field == "depot_id":
                    id_lookup = value
                elif field == "fuel_types_supported":
                    fuel_type_filter = value
                else:
                    equality[field] = value

        if id_lookup is not None:
            doc = self.docs.get(id_lookup)
            if doc is None:
                return {"hits": {"hits": [], "total": {"value": 0}}}
            return {
                "hits": {
                    "hits": [{"_source": dict(doc)}],
                    "total": {"value": 1},
                }
            }

        matches: List[Dict[str, Any]] = []
        for doc in self.docs.values():
            if tenant_id is not None and doc.get("tenant_id") != tenant_id:
                continue
            if any(doc.get(k) != v for k, v in equality.items()):
                continue
            if fuel_type_filter is not None:
                supported = doc.get("fuel_types_supported") or []
                if fuel_type_filter not in supported:
                    continue
            matches.append({"_source": dict(doc)})

        matches = matches[:size]
        return {"hits": {"hits": matches, "total": {"value": len(matches)}}}

    # -------- update_document --------------------------------------------
    async def update_document(
        self, index: str, doc_id: str, partial: Dict[str, Any]
    ) -> None:
        existing = self.docs.get(doc_id)
        if existing is None:
            raise RuntimeError(f"update_document called for missing {doc_id}")
        existing.update(partial)

    # -------- delete_document --------------------------------------------
    async def delete_document(self, index: str, doc_id: str) -> bool:
        return self.docs.pop(doc_id, None) is not None


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


def _build_app(tenant_id: str = "tenant-1") -> tuple[FastAPI, _FakeESService]:
    es = _FakeESService()
    repo = DepotRepository(es_service=es)
    configure_fuel_ops_endpoints(es_service=es, depot_repository=repo)

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es


def _base_create_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "depot_id": "depot_001",
        "name": "Newark Rack",
        "location_lat": 40.7357,
        "location_lon": -74.1724,
        "address": "1 Fuel Lane, Newark, NJ",
        "timezone": "America/New_York",
        "fuel_types_supported": ["DIESEL_2", "GASOLINE_REG"],
        "status": "active",
    }
    payload.update(overrides)
    return payload


def _seed_depot(
    es: _FakeESService,
    depot_id: str = "depot_001",
    tenant_id: str = "tenant-1",
    **overrides: Any,
) -> None:
    """Insert a well-formed Depot source directly into the fake index.

    Used to exercise read / update / delete paths without going through
    ``POST``. Callers can override any field via ``**overrides``.
    """

    payload = _base_create_payload(depot_id=depot_id, **overrides)
    payload["tenant_id"] = tenant_id
    payload.setdefault("created_at", "2025-01-01T00:00:00+00:00")
    payload.setdefault("updated_at", "2025-01-01T00:00:00+00:00")
    es.docs[depot_id] = payload


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/depots (Req 2.2.2)
# ---------------------------------------------------------------------------


class TestCreateDepot:
    def test_creates_depot_and_stamps_tenant_from_jwt(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)

        body = _base_create_payload()
        resp = client.post("/api/fuel/mvp/depots", json=body)

        assert resp.status_code == 201
        data = resp.json()
        assert data["depot_id"] == "depot_001"
        # Router stamps tenant_id from the verified JWT context; callers do
        # not — and must not need to — send tenant_id in the body.
        assert data["tenant_id"] == "tenant-1"
        assert data["location_lat"] == pytest.approx(40.7357)
        assert data["location_lon"] == pytest.approx(-74.1724)
        assert data["fuel_types_supported"] == ["DIESEL_2", "GASOLINE_REG"]
        # The repository must have persisted the record under its id.
        assert "depot_001" in es.docs
        assert es.docs["depot_001"]["tenant_id"] == "tenant-1"

    def test_mints_id_when_omitted(self):
        app, es = _build_app()
        client = TestClient(app)

        body = _base_create_payload()
        body.pop("depot_id")
        resp = client.post("/api/fuel/mvp/depots", json=body)

        assert resp.status_code == 201
        data = resp.json()
        assert data["depot_id"].startswith("depot_")
        assert data["depot_id"] in es.docs

    def test_canonicalizes_fuel_product_aliases(self):
        """Legacy aliases (LPG, AGO) are normalized to canonical codes."""

        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(fuel_types_supported=["LPG", "AGO"])
        resp = client.post("/api/fuel/mvp/depots", json=body)

        assert resp.status_code == 201
        assert resp.json()["fuel_types_supported"] == ["PROPANE", "DIESEL_2"]

    def test_rejects_unknown_fuel_product(self):
        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(fuel_types_supported=["UNOBTAINIUM"])
        resp = client.post("/api/fuel/mvp/depots", json=body)

        # Pydantic's ``fuel_types_supported`` validator raises
        # :class:`UnknownFuelProductError` which the request-body coercion
        # wraps into a 422 (not a 400, because the failure happens during
        # FastAPI's request validation phase rather than inside the
        # repository call site).
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "field, value",
        [
            ("location_lat", 90.1),
            ("location_lat", -90.1),
            ("location_lon", 180.1),
            ("location_lon", -180.1),
        ],
    )
    def test_rejects_out_of_range_coordinates(self, field, value):
        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(**{field: value})
        resp = client.post("/api/fuel/mvp/depots", json=body)

        assert resp.status_code == 422

    def test_accepts_coordinate_boundaries(self):
        """Exact boundary values (±90, ±180) must be accepted."""

        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(
            depot_id="depot_north",
            location_lat=90.0,
            location_lon=180.0,
        )
        resp = client.post("/api/fuel/mvp/depots", json=body)
        assert resp.status_code == 201
        data = resp.json()
        assert data["location_lat"] == 90.0
        assert data["location_lon"] == 180.0

    def test_rejects_invalid_timezone(self):
        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(timezone="Not/A_Real_TZ")
        resp = client.post("/api/fuel/mvp/depots", json=body)
        # Invalid IANA timezone surfaces from the Depot model validator as
        # a 422 (validation error) since the request body passes
        # type-level validation but fails Pydantic's field validator
        # inside the repository.
        assert resp.status_code == 422

    def test_rejects_extra_fields(self):
        """``extra='forbid'`` on DepotCreateRequest must reject unknown keys."""

        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload()
        body["sneaky"] = "nope"
        resp = client.post("/api/fuel/mvp/depots", json=body)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/depots (Req 2.2.2)
# ---------------------------------------------------------------------------


class TestListDepots:
    def test_lists_only_current_tenants_depots(self):
        """Foreign-tenant records never appear in the response."""

        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)

        _seed_depot(es, depot_id="mine", tenant_id="tenant-1")
        _seed_depot(es, depot_id="other", tenant_id="tenant-2")

        resp = client.get("/api/fuel/mvp/depots")
        assert resp.status_code == 200
        data = resp.json()
        ids = [d["depot_id"] for d in data["items"]]
        assert ids == ["mine"]
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert data["has_next"] is False

    def test_status_filter(self):
        app, es = _build_app()
        client = TestClient(app)

        _seed_depot(es, depot_id="active_one", status="active")
        _seed_depot(es, depot_id="inactive_one", status="inactive")

        resp = client.get(
            "/api/fuel/mvp/depots", params={"status": "inactive"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [d["depot_id"] for d in data["items"]] == ["inactive_one"]

    def test_fuel_type_filter_canonicalizes_alias(self):
        """Filtering by ``LPG`` matches depots that persist ``PROPANE``."""

        app, es = _build_app()
        client = TestClient(app)

        _seed_depot(
            es,
            depot_id="p",
            fuel_types_supported=["PROPANE"],
        )
        _seed_depot(
            es,
            depot_id="d",
            fuel_types_supported=["DIESEL_2"],
        )

        resp = client.get(
            "/api/fuel/mvp/depots", params={"fuel_type": "LPG"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [d["depot_id"] for d in data["items"]] == ["p"]

    def test_fuel_type_filter_unknown_returns_empty(self):
        """An unknown fuel-type filter is a miss, not a 400."""

        app, es = _build_app()
        client = TestClient(app)
        _seed_depot(es)

        resp = client.get(
            "/api/fuel/mvp/depots", params={"fuel_type": "UNOBTAINIUM"}
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_rejects_invalid_status_filter(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/depots", params={"status": "nope"}
        )
        assert resp.status_code == 422

    def test_pagination_has_next(self):
        app, es = _build_app()
        client = TestClient(app)

        # Seed three depots; page_size=2 => page 1 returns 2 items with
        # has_next=True, page 2 returns 1 item with has_next=False.
        for i in range(3):
            _seed_depot(es, depot_id=f"d{i}")

        resp = client.get("/api/fuel/mvp/depots", params={"size": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["page"] == 1
        assert data["page_size"] == 2
        assert data["has_next"] is True

        resp2 = client.get(
            "/api/fuel/mvp/depots", params={"size": 2, "page": 2}
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert len(data2["items"]) == 1
        assert data2["page"] == 2
        assert data2["has_next"] is False


# ---------------------------------------------------------------------------
# PATCH /api/fuel/mvp/depots/{depot_id} (Req 2.2.2)
# ---------------------------------------------------------------------------


class TestUpdateDepot:
    def test_applies_partial_patch(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_depot(es)

        resp = client.patch(
            "/api/fuel/mvp/depots/depot_001",
            json={"name": "Renamed Rack", "status": "inactive"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Renamed Rack"
        assert data["status"] == "inactive"
        # Other fields preserved.
        assert data["address"] == "1 Fuel Lane, Newark, NJ"
        assert data["fuel_types_supported"] == ["DIESEL_2", "GASOLINE_REG"]

    def test_canonicalizes_fuel_types_on_patch(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_depot(es)

        resp = client.patch(
            "/api/fuel/mvp/depots/depot_001",
            json={"fuel_types_supported": ["LPG"]},
        )
        assert resp.status_code == 200
        assert resp.json()["fuel_types_supported"] == ["PROPANE"]

    @pytest.mark.parametrize(
        "field, value",
        [
            ("location_lat", 91.0),
            ("location_lon", -181.0),
        ],
    )
    def test_rejects_out_of_range_coordinates_on_patch(self, field, value):
        app, es = _build_app()
        client = TestClient(app)
        _seed_depot(es)

        resp = client.patch(
            "/api/fuel/mvp/depots/depot_001",
            json={field: value},
        )
        assert resp.status_code == 422

    def test_rejects_invalid_timezone_on_patch(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_depot(es)

        resp = client.patch(
            "/api/fuel/mvp/depots/depot_001",
            json={"timezone": "Not/A_Real_TZ"},
        )
        assert resp.status_code == 422

    def test_rejects_immutable_fields(self):
        """``tenant_id`` and ``depot_id`` are not accepted in the patch body."""

        app, es = _build_app()
        client = TestClient(app)
        _seed_depot(es)

        resp = client.patch(
            "/api/fuel/mvp/depots/depot_001",
            json={"tenant_id": "tenant-evil"},
        )
        assert resp.status_code == 422

    def test_returns_404_for_missing(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.patch(
            "/api/fuel/mvp/depots/does-not-exist",
            json={"status": "inactive"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["detail"]["error_code"] == "depot_not_found"

    def test_returns_403_for_cross_tenant(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_depot(es, depot_id="depot_001", tenant_id="tenant-2")

        resp = client.patch(
            "/api/fuel/mvp/depots/depot_001",
            json={"status": "inactive"},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["error_code"] == "cross_tenant_access_denied"
        # Depot_id is safe to echo; owning tenant is not leaked.
        assert body["detail"]["depot_id"] == "depot_001"

    def test_empty_patch_returns_current_model(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_depot(es)

        resp = client.patch("/api/fuel/mvp/depots/depot_001", json={})
        assert resp.status_code == 200
        assert resp.json()["depot_id"] == "depot_001"

    def test_empty_patch_missing_returns_404(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.patch(
            "/api/fuel/mvp/depots/does-not-exist", json={}
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/fuel/mvp/depots/{depot_id} (Req 2.2.2)
# ---------------------------------------------------------------------------


class TestDeleteDepot:
    def test_deletes_owned_depot(self):
        app, es = _build_app()
        client = TestClient(app)
        _seed_depot(es)

        resp = client.delete("/api/fuel/mvp/depots/depot_001")
        assert resp.status_code == 204
        # Body is empty for 204 per the HTTP spec.
        assert resp.content == b""
        assert "depot_001" not in es.docs

    def test_returns_404_for_missing(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.delete("/api/fuel/mvp/depots/does-not-exist")
        assert resp.status_code == 404
        body = resp.json()
        assert body["detail"]["error_code"] == "depot_not_found"

    def test_returns_403_for_cross_tenant(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_depot(es, depot_id="depot_001", tenant_id="tenant-2")

        resp = client.delete("/api/fuel/mvp/depots/depot_001")
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["error_code"] == "cross_tenant_access_denied"
        # Depot must remain in the store — cross-tenant delete is a no-op.
        assert "depot_001" in es.docs


# ---------------------------------------------------------------------------
# configure_fuel_ops_endpoints wiring (Req 2.2.2)
# ---------------------------------------------------------------------------


class TestConfigureWiring:
    def test_auto_constructs_depot_repository_from_es_service(self):
        """When ``depot_repository`` is omitted, the configure helper must
        auto-construct one from ``es_service`` so callers don't have to
        wire every collaborator by hand."""

        es = _FakeESService()
        # Omit depot_repository entirely.
        configure_fuel_ops_endpoints(es_service=es)

        app = FastAPI()
        app.include_router(router)
        app.include_router(mvp_router)
        app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory()
        client = TestClient(app)

        body = _base_create_payload()
        resp = client.post("/api/fuel/mvp/depots", json=body)
        assert resp.status_code == 201
        assert "depot_001" in es.docs


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/depots/{depot_id} (cross-module-entity-linkage Req 10.2, 10.3)
# ---------------------------------------------------------------------------


def _seed_asset(
    es: _FakeESService,
    asset_id: str,
    *,
    tenant_id: str = "tenant-1",
    assigned_depot_id: str | None = None,
    asset_name: str = "Tanker",
    asset_type: str = "vehicle",
    status: str = "active",
) -> None:
    """Insert an asset/truck source into the shared fake index.

    The endpoint enumerates assets via the ``assets`` alias; the fake stub
    ignores the index argument, so seeding into the same dict is sufficient to
    exercise the ``assigned_depot_id`` term filter.
    """

    doc: Dict[str, Any] = {
        "truck_id": asset_id,
        "asset_id": asset_id,
        "asset_name": asset_name,
        "asset_type": asset_type,
        "status": status,
        "tenant_id": tenant_id,
    }
    if assigned_depot_id is not None:
        doc["assigned_depot_id"] = assigned_depot_id
    es.docs[asset_id] = doc


class TestGetDepot:
    def test_returns_depot_and_round_trips_is_default_true(self):
        """A depot read echoes the canonical ``is_default`` flag (Req 10.3)."""

        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_depot(es, depot_id="depot_001", is_default=True)

        resp = client.get("/api/fuel/mvp/depots/depot_001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["depot"]["depot_id"] == "depot_001"
        assert data["depot"]["is_default"] is True
        # No expand requested → assets omitted (None), not an empty list.
        assert data["assigned_assets"] is None

    def test_round_trips_is_default_false(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_depot(es, depot_id="depot_002", is_default=False)

        resp = client.get("/api/fuel/mvp/depots/depot_002")
        assert resp.status_code == 200
        assert resp.json()["depot"]["is_default"] is False

    def test_expand_assets_enumerates_assigned_assets(self):
        """``?expand=assets`` lists assets whose assigned_depot_id matches (Req 10.2)."""

        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_depot(es, depot_id="depot_001")
        _seed_asset(es, "AST-1", assigned_depot_id="depot_001", asset_name="Tanker 1")
        _seed_asset(es, "AST-2", assigned_depot_id="depot_001", asset_name="Tanker 2")
        # An asset assigned elsewhere must not appear.
        _seed_asset(es, "AST-3", assigned_depot_id="depot_999")
        # An unassigned asset must not appear.
        _seed_asset(es, "AST-4", assigned_depot_id=None)

        resp = client.get("/api/fuel/mvp/depots/depot_001", params={"expand": "assets"})
        assert resp.status_code == 200
        data = resp.json()
        ids = sorted(a["asset_id"] for a in data["assigned_assets"])
        assert ids == ["AST-1", "AST-2"]
        first = next(a for a in data["assigned_assets"] if a["asset_id"] == "AST-1")
        assert first["name"] == "Tanker 1"
        assert first["asset_type"] == "vehicle"
        assert first["status"] == "active"

    def test_expand_assets_empty_when_none_assigned(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_depot(es, depot_id="depot_001")

        resp = client.get("/api/fuel/mvp/depots/depot_001", params={"expand": "assets"})
        assert resp.status_code == 200
        assert resp.json()["assigned_assets"] == []

    def test_asset_enumeration_is_tenant_scoped(self):
        """Another tenant's asset never appears in the enumeration (Req 5.3)."""

        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_depot(es, depot_id="depot_001", tenant_id="tenant-1")
        _seed_asset(
            es, "AST-MINE", tenant_id="tenant-1", assigned_depot_id="depot_001"
        )
        # Foreign-tenant asset referencing the same depot id must be excluded.
        _seed_asset(
            es, "AST-OTHER", tenant_id="tenant-2", assigned_depot_id="depot_001"
        )

        resp = client.get("/api/fuel/mvp/depots/depot_001", params={"expand": "assets"})
        assert resp.status_code == 200
        ids = [a["asset_id"] for a in resp.json()["assigned_assets"]]
        assert ids == ["AST-MINE"]

    def test_missing_depot_returns_404(self):
        app, _ = _build_app(tenant_id="tenant-1")
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/depots/does_not_exist")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "depot_not_found"

    def test_cross_tenant_depot_is_404(self):
        """Fetching another tenant's depot is suppressed to a 404, not 403."""

        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        _seed_depot(es, depot_id="depot_other", tenant_id="tenant-2")

        resp = client.get("/api/fuel/mvp/depots/depot_other")
        assert resp.status_code == 404
