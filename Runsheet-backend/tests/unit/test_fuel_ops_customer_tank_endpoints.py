"""
Unit tests for the Customer_Tank CRUD REST endpoints added by Task 3.6.

Covers the endpoints mounted under :data:`fuel.api.fuel_ops_endpoints.mvp_router`:

* ``GET    /api/fuel/mvp/customer-tanks``            — tenant-scoped list
* ``GET    /api/fuel/mvp/customer-tanks/{id}``       — single tank fetch
* ``POST   /api/fuel/mvp/customer-tanks``            — create
* ``PATCH  /api/fuel/mvp/customer-tanks/{id}``       — partial update

Each test uses :class:`_FakeESService` — a minimal in-memory stub that
implements the subset of :class:`ElasticsearchService` the repository
relies on. This keeps the endpoint tests decoupled from the real
Elasticsearch backend while still exercising the full repository +
router wiring (the same pattern used by ``test_customer_tank_models``).

Validates: Requirements 1.6.2, 1.6.3.
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
from fuel.customer_tank_models import CustomerTankRepository
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fake ES service
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub used by :class:`CustomerTankRepository`.

    Implements ``index_document``, ``search_documents``,
    ``update_document``, and ``delete_document`` over an in-memory dict
    keyed by ``doc_id``. Queries with a ``term`` on ``customer_tank_id``
    resolve to a single-document lookup; queries with ``bool.must`` on
    ``tenant_id`` and optional equality filters are honoured so the
    endpoint-level ``list_for_tenant`` filters exercise the real code
    paths.
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
        id_lookup: str | None = None

        for clause in must:
            term = clause.get("term") if isinstance(clause, dict) else None
            if not term:
                continue
            for field, value in term.items():
                if field == "tenant_id":
                    tenant_id = value
                elif field == "customer_tank_id":
                    id_lookup = value
                else:
                    equality[field] = value

        if id_lookup is not None:
            doc = self.docs.get(id_lookup)
            if doc is None:
                return {"hits": {"hits": [], "total": {"value": 0}}}
            return {"hits": {"hits": [{"_source": dict(doc)}], "total": {"value": 1}}}

        matches: List[Dict[str, Any]] = []
        for doc in self.docs.values():
            if tenant_id is not None and doc.get("tenant_id") != tenant_id:
                continue
            if any(doc.get(k) != v for k, v in equality.items()):
                continue
            matches.append({"_source": dict(doc)})

        # Honour the requested ``size`` like the real ES service does.
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
    repo = CustomerTankRepository(es_service=es)
    configure_fuel_ops_endpoints(es_service=es, customer_tank_repository=repo)

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es


def _base_create_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "customer_tank_id": "tank_001",
        "customer_id": "cust_001",
        "customer_type": "residential",
        "fuel_type": "propane",
        "fuel_product_code": "PROPANE",
        "capacity_gallons": 500.0,
        "current_level_gallons": 250.0,
        "location_lat": 40.7128,
        "location_lon": -74.0060,
        "zip_code": "10001",
        "status": "active",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# POST /api/fuel/mvp/customer-tanks (Req 1.6.3)
# ---------------------------------------------------------------------------


class TestCreateCustomerTank:
    def test_creates_tank_and_stamps_tenant_from_jwt(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)

        body = _base_create_payload()
        # Client does not — and must not need to — send tenant_id.
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)

        assert resp.status_code == 201
        data = resp.json()
        assert data["customer_tank_id"] == "tank_001"
        assert data["tenant_id"] == "tenant-1"
        assert data["fuel_product_code"] == "PROPANE"
        # Repository must have persisted the record under its id.
        assert "tank_001" in es.docs
        assert es.docs["tank_001"]["tenant_id"] == "tenant-1"

    def test_mints_id_when_omitted(self):
        app, es = _build_app()
        client = TestClient(app)

        body = _base_create_payload()
        body.pop("customer_tank_id")
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)

        assert resp.status_code == 201
        data = resp.json()
        assert data["customer_tank_id"].startswith("tank_")
        assert data["customer_tank_id"] in es.docs

    def test_canonicalizes_legacy_alias(self):
        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(fuel_product_code="LPG")
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)

        assert resp.status_code == 201
        assert resp.json()["fuel_product_code"] == "PROPANE"

    def test_rejects_unknown_product_code(self):
        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(fuel_product_code="UNOBTAINIUM")
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)

        # The repository raises UnknownFuelProductError before Pydantic's
        # validator runs, and the router maps that to a 400 with a
        # structured ``unknown_product_code`` payload so clients can
        # distinguish "bad product" from generic validation errors.
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["error_code"] == "unknown_product_code"
        assert body["detail"]["fuel_product_code"] == "UNOBTAINIUM"

    def test_rejects_level_exceeding_capacity(self):
        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(
            capacity_gallons=100.0, current_level_gallons=200.0
        )
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)
        assert resp.status_code == 422

    def test_rejects_invalid_coordinates(self):
        app, _ = _build_app()
        client = TestClient(app)

        body = _base_create_payload(location_lat=91.0)
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)
        # FastAPI's Query/Body constraint validation raises 422.
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/customer-tanks (Req 1.6.2)
# ---------------------------------------------------------------------------


class TestListCustomerTanks:
    def test_lists_only_current_tenants_tanks(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)

        # Seed one owned and one foreign tank directly so we bypass the
        # guarded repository's cross-tenant stamping.
        es.docs["mine"] = {
            **_base_create_payload(customer_tank_id="mine"),
            "tenant_id": "tenant-1",
        }
        es.docs["other"] = {
            **_base_create_payload(customer_tank_id="other"),
            "tenant_id": "tenant-2",
        }

        resp = client.get("/api/fuel/mvp/customer-tanks")
        assert resp.status_code == 200
        data = resp.json()
        ids = [t["customer_tank_id"] for t in data["items"]]
        assert ids == ["mine"]
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["has_next"] is False

    def test_customer_type_filter(self):
        app, es = _build_app()
        client = TestClient(app)
        es.docs["res"] = {
            **_base_create_payload(customer_tank_id="res", customer_type="residential"),
            "tenant_id": "tenant-1",
        }
        es.docs["com"] = {
            **_base_create_payload(customer_tank_id="com", customer_type="commercial"),
            "tenant_id": "tenant-1",
        }

        resp = client.get(
            "/api/fuel/mvp/customer-tanks",
            params={"customer_type": "commercial"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [t["customer_tank_id"] for t in data["items"]] == ["com"]

    def test_fuel_type_filter(self):
        app, es = _build_app()
        client = TestClient(app)
        es.docs["p"] = {
            **_base_create_payload(
                customer_tank_id="p", fuel_type="propane", fuel_product_code="PROPANE"
            ),
            "tenant_id": "tenant-1",
        }
        es.docs["h"] = {
            **_base_create_payload(
                customer_tank_id="h",
                fuel_type="heating_oil",
                fuel_product_code="HEATING_OIL",
            ),
            "tenant_id": "tenant-1",
        }

        resp = client.get(
            "/api/fuel/mvp/customer-tanks",
            params={"fuel_type": "heating_oil"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [t["customer_tank_id"] for t in data["items"]] == ["h"]

    def test_rejects_invalid_customer_type(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/customer-tanks",
            params={"customer_type": "nope"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/fuel/mvp/customer-tanks/{id} (Req 1.6.2)
# ---------------------------------------------------------------------------


class TestGetCustomerTank:
    def test_returns_owned_tank(self):
        app, es = _build_app()
        client = TestClient(app)
        es.docs["tank_001"] = {
            **_base_create_payload(),
            "tenant_id": "tenant-1",
        }

        resp = client.get("/api/fuel/mvp/customer-tanks/tank_001")
        assert resp.status_code == 200
        assert resp.json()["customer_tank_id"] == "tank_001"

    def test_returns_404_for_missing(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/customer-tanks/does-not-exist")
        assert resp.status_code == 404
        body = resp.json()
        assert body["detail"]["error_code"] == "customer_tank_not_found"

    def test_returns_404_for_cross_tenant(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        es.docs["tank_001"] = {
            **_base_create_payload(),
            "tenant_id": "tenant-2",
        }

        resp = client.get("/api/fuel/mvp/customer-tanks/tank_001")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/fuel/mvp/customer-tanks/{id} (Req 1.6.3)
# ---------------------------------------------------------------------------


class TestUpdateCustomerTank:
    def test_applies_partial_patch(self):
        app, es = _build_app()
        client = TestClient(app)
        es.docs["tank_001"] = {
            **_base_create_payload(),
            "tenant_id": "tenant-1",
        }

        resp = client.patch(
            "/api/fuel/mvp/customer-tanks/tank_001",
            json={"current_level_gallons": 123.0, "status": "maintenance"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_level_gallons"] == 123.0
        assert data["status"] == "maintenance"
        # Other fields preserved.
        assert data["capacity_gallons"] == 500.0
        assert data["customer_type"] == "residential"

    def test_canonicalizes_fuel_product_code_on_patch(self):
        app, es = _build_app()
        client = TestClient(app)
        es.docs["tank_001"] = {
            **_base_create_payload(),
            "tenant_id": "tenant-1",
        }

        resp = client.patch(
            "/api/fuel/mvp/customer-tanks/tank_001",
            json={"fuel_product_code": "LPG"},
        )
        assert resp.status_code == 200
        assert resp.json()["fuel_product_code"] == "PROPANE"

    def test_returns_404_for_missing(self):
        app, _ = _build_app()
        client = TestClient(app)

        resp = client.patch(
            "/api/fuel/mvp/customer-tanks/does-not-exist",
            json={"current_level_gallons": 1.0},
        )
        assert resp.status_code == 404

    def test_returns_403_for_cross_tenant(self):
        app, es = _build_app(tenant_id="tenant-1")
        client = TestClient(app)
        es.docs["tank_001"] = {
            **_base_create_payload(),
            "tenant_id": "tenant-2",
        }

        resp = client.patch(
            "/api/fuel/mvp/customer-tanks/tank_001",
            json={"current_level_gallons": 1.0},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["error_code"] == "cross_tenant_access_denied"

    def test_empty_patch_returns_current_model(self):
        app, es = _build_app()
        client = TestClient(app)
        es.docs["tank_001"] = {
            **_base_create_payload(),
            "tenant_id": "tenant-1",
        }

        resp = client.patch(
            "/api/fuel/mvp/customer-tanks/tank_001", json={}
        )
        assert resp.status_code == 200
        assert resp.json()["customer_tank_id"] == "tank_001"

    def test_rejects_level_exceeding_capacity_on_patch(self):
        app, es = _build_app()
        client = TestClient(app)
        es.docs["tank_001"] = {
            **_base_create_payload(),
            "tenant_id": "tenant-1",
        }

        resp = client.patch(
            "/api/fuel/mvp/customer-tanks/tank_001",
            json={"current_level_gallons": 9_999.0},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Cross-module entity linkage (Task 6) — customer ref validation + resolver read
# (Req 7.1, 7.2, 7.3, 13.1)
# ---------------------------------------------------------------------------


def _build_app_with_resolver(
    resolver: Any, tenant_id: str = "tenant-1"
) -> tuple[FastAPI, _FakeESService]:
    """Build an app whose customer-tank endpoints use an injected resolver.

    Registers the shared exception handlers so a write-time
    ``validation_error`` (raised by ``RefResolver.validate_ref``) surfaces as
    an HTTP 400 rather than an unhandled 500.
    """
    from errors.handlers import register_exception_handlers

    es = _FakeESService()
    repo = CustomerTankRepository(es_service=es)
    configure_fuel_ops_endpoints(
        es_service=es, customer_tank_repository=repo, ref_resolver=resolver
    )

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es


def _resolver_with(customers: Dict[str, set], orders: Dict[str, set]):
    """A RefResolver whose customer/order loaders resolve seeded ids per tenant.

    ``customers`` / ``orders`` map ``tenant_id -> {id, ...}``. An id present for
    the caller's tenant resolves to a small summary; anything else resolves to
    ``None`` (surfaced as ``unresolved`` / rejected on write).
    """
    from services.ref_resolver import RefResolver

    resolver = RefResolver()

    async def _customer_loader(tenant_id: str, entity_id: str):
        if entity_id in customers.get(tenant_id, set()):
            return {"customer_id": entity_id, "display_name": f"Cust {entity_id}"}
        return None

    async def _order_loader(tenant_id: str, entity_id: str):
        if entity_id in orders.get(tenant_id, set()):
            return {"order_id": entity_id, "status": "delivered"}
        return None

    resolver.register("customer", _customer_loader)
    resolver.register("order", _order_loader)
    return resolver


class TestCustomerTankCustomerRefValidation:
    """Write-time validation of ``customer_id`` as a reference (Req 7.1)."""

    def test_create_rejects_nonexistent_customer(self):
        resolver = _resolver_with(customers={"tenant-1": {"cust_ok"}}, orders={})
        app, _ = _build_app_with_resolver(resolver)
        client = TestClient(app)

        body = _base_create_payload(customer_id="cust_missing")
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)

        assert resp.status_code == 400
        detail = resp.json()
        # The structured validation_error carries a stable reason.
        assert "customer_not_found" in str(detail)

    def test_create_accepts_existing_customer(self):
        resolver = _resolver_with(customers={"tenant-1": {"cust_001"}}, orders={})
        app, _ = _build_app_with_resolver(resolver)
        client = TestClient(app)

        body = _base_create_payload(customer_id="cust_001")
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)

        assert resp.status_code == 201
        assert resp.json()["customer_id"] == "cust_001"

    def test_create_persists_last_refill_order_id(self):
        resolver = _resolver_with(customers={"tenant-1": {"cust_001"}}, orders={})
        app, _ = _build_app_with_resolver(resolver)
        client = TestClient(app)

        body = _base_create_payload(
            customer_id="cust_001", last_refill_order_id="ORD-9"
        )
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)

        assert resp.status_code == 201
        assert resp.json()["last_refill_order_id"] == "ORD-9"

    def test_patch_rejects_nonexistent_customer(self):
        resolver = _resolver_with(customers={"tenant-1": {"cust_001"}}, orders={})
        app, es = _build_app_with_resolver(resolver)
        client = TestClient(app)
        es.docs["tank_001"] = {
            **_base_create_payload(customer_id="cust_001"),
            "tenant_id": "tenant-1",
        }

        resp = client.patch(
            "/api/fuel/mvp/customer-tanks/tank_001",
            json={"customer_id": "cust_missing"},
        )
        assert resp.status_code == 400
        assert "customer_not_found" in str(resp.json())

    def test_create_skips_validation_when_no_customer_loader(self):
        """A partially-wired resolver (no ``customer`` loader) stays additive."""
        from services.ref_resolver import RefResolver

        app, _ = _build_app_with_resolver(RefResolver())
        client = TestClient(app)

        body = _base_create_payload(customer_id="anything")
        resp = client.post("/api/fuel/mvp/customer-tanks", json=body)
        assert resp.status_code == 201


class TestCustomerTankResolverRead:
    """``GET /customer-tanks/{id}?expand=customer,last_refill_order`` (Req 7.2/7.3/5.4)."""

    def _seed_tank(self, es: _FakeESService, **overrides: Any) -> None:
        es.docs["tank_001"] = {
            **_base_create_payload(**overrides),
            "tenant_id": "tenant-1",
        }

    def test_no_expand_returns_unchanged_contract(self):
        resolver = _resolver_with(customers={"tenant-1": {"cust_001"}}, orders={})
        app, es = _build_app_with_resolver(resolver)
        self._seed_tank(es, customer_id="cust_001")
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/customer-tanks/tank_001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["customer_tank_id"] == "tank_001"
        assert "links" not in body

    def test_expand_resolves_customer_and_order(self):
        resolver = _resolver_with(
            customers={"tenant-1": {"cust_001"}}, orders={"tenant-1": {"ORD-9"}}
        )
        app, es = _build_app_with_resolver(resolver)
        self._seed_tank(es, customer_id="cust_001", last_refill_order_id="ORD-9")
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/customer-tanks/tank_001",
            params={"expand": "customer,last_refill_order"},
        )
        assert resp.status_code == 200
        links = resp.json()["links"]
        assert links["customer"]["status"] == "resolved"
        assert links["customer"]["id"] == "cust_001"
        assert links["last_refill_order"]["status"] == "resolved"
        assert links["last_refill_order"]["id"] == "ORD-9"

    def test_expand_marks_unresolved_not_dropped(self):
        # last_refill_order_id points at an order that does not resolve.
        resolver = _resolver_with(customers={"tenant-1": {"cust_001"}}, orders={})
        app, es = _build_app_with_resolver(resolver)
        self._seed_tank(es, customer_id="cust_001", last_refill_order_id="GHOST")
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/customer-tanks/tank_001",
            params={"expand": "customer,last_refill_order"},
        )
        assert resp.status_code == 200
        links = resp.json()["links"]
        assert links["last_refill_order"]["status"] == "unresolved"
        assert links["last_refill_order"]["id"] == "GHOST"

    def test_expand_marks_absent_order_empty(self):
        resolver = _resolver_with(customers={"tenant-1": {"cust_001"}}, orders={})
        app, es = _build_app_with_resolver(resolver)
        self._seed_tank(es, customer_id="cust_001")  # no last_refill_order_id
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/customer-tanks/tank_001",
            params={"expand": "last_refill_order"},
        )
        assert resp.status_code == 200
        links = resp.json()["links"]
        assert links["last_refill_order"]["status"] == "empty"
